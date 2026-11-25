#include <rclcpp/rclcpp.hpp>
#include <rclcpp/serialization.hpp>
#include <rosbag2_cpp/writer.hpp>
#include <rosbag2_storage/serialized_bag_message.hpp>
#include <std_msgs/msg/string.hpp>
#include <std_srvs/srv/trigger.hpp>
#include <tf2_msgs/msg/tf_message.hpp>
#include <yaml-cpp/yaml.h>
#include <atomic>
#include <cmath>
#include <iostream>
#include <regex>
#include <future>
#include <condition_variable>
#include <deque>
#include <filesystem>
#include <fstream>
#include <map>
#include <set>
#include <thread>

// Explicit string tags preserve identifiers such as "on" for YAML 1.1 readers.
static std::string dump_yaml(const YAML::Node & node) {
  YAML::Emitter emitter;
  emitter << node;
  if (!emitter.good()) throw std::runtime_error("YAML serialization failed");
  return emitter.c_str();
}
static YAML::Node yaml_string(const std::string & value) {
  YAML::Node result(value); result.SetTag("tag:yaml.org,2002:str"); return result;
}
using Ns = int64_t;
static Ns steady_now() {
  return std::chrono::duration_cast<std::chrono::nanoseconds>(
    std::chrono::steady_clock::now().time_since_epoch()).count();
}
struct Message {
  uint64_t seq{};
  Ns steady{}, ros{};
  std::string topic, type;
  std::shared_ptr<rclcpp::SerializedMessage> data;
  bool context{};
  size_t bytes() const { return data ? data->capacity() + 256 : 256; }
};
struct TriggerDecision {
  bool accepted{false};
  std::string event_id, reason{"manager_error"};
};
struct Input {
  enum Kind { DATA, TRIGGER, TICK, STOP } kind;
  Message msg;
  std::string id, reason, rule;
  std::shared_ptr<std::promise<TriggerDecision>> completion;
};
struct Event {
  std::string id, rule;
  Ns start{}, end{};
  YAML::Node meta;
  std::set<std::string> errors;
};
struct Job {
  enum Kind { OPEN, WRITE, CLOSE } kind;
  std::string id;
  Message msg;
  YAML::Node meta;
};
class Recorder : public rclcpp::Node {
 public:
  Recorder() : Node("event_recorder") {
    auto seconds = [this](const char * name, double value) {
      double v = declare_parameter<double>(name, value);
      if (!std::isfinite(v) || v <= 0 || v > 86400) throw std::runtime_error(name);
      return static_cast<Ns>(v * 1e9);
    };
    pre_ = seconds("pre_trigger_seconds", 5); post_ = seconds("post_trigger_seconds", 10);
    retention_ = seconds("history_retention_seconds", 8);
    cooldown_ = seconds("trigger_cooldown_seconds", 3);
    maximum_ = seconds("max_event_seconds", 300); dedup_ = seconds("dedup_seconds", 60);
    if (retention_ < pre_ || maximum_ < pre_ + post_) throw std::runtime_error("invalid window limits");
    auto budget = [this](const char * name, int64_t value) {
      auto v = declare_parameter<int64_t>(name, value);
      if (v <= 0) throw std::runtime_error(name);
      return static_cast<size_t>(v);
    };
    history_limit_ = budget("history_bytes", 268435456);
    input_limit_ = budget("input_bytes", 67108864);
    writer_limit_ = budget("writer_bytes", 536870912);
    context_limit_ = budget("context_bytes", 16777216);
    items_ = budget("queue_items", 100000);
    pending_limit_ = budget("max_pending_events", 8);
    free_limit_ = budget("minimum_free_bytes", 1073741824);
    output_ = declare_parameter<std::string>("output_directory", "event_bags");
    storage_ = declare_parameter<std::string>("storage_id", "sqlite3");
    std::filesystem::create_directories(output_);
    auto topics = declare_parameter<std::vector<std::string>>("topics", {"/imu/data"});
    auto types = declare_parameter<std::vector<std::string>>("types", {"sensor_msgs/msg/Imu"});
    auto rel = declare_parameter<std::vector<std::string>>("reliability", std::vector<std::string>{});
    auto dur = declare_parameter<std::vector<std::string>>("durability", std::vector<std::string>{});
    auto ct = declare_parameter<std::vector<std::string>>("context_topics", std::vector<std::string>{});
    auto cy = declare_parameter<std::vector<std::string>>("context_types", std::vector<std::string>{});
    if (topics.size() != types.size() || ct.size() != cy.size() ||
        (!rel.empty() && rel.size() != topics.size()) ||
        (!dur.empty() && dur.size() != topics.size())) throw std::runtime_error("topic configuration lengths differ");
    for (size_t i = 0; i < topics.size(); ++i)
      subscribe(topics[i], types[i], false, rel.empty() ? "best_effort" : rel[i], dur.empty() ? "volatile" : dur[i]);
    for (size_t i = 0; i < ct.size(); ++i) subscribe(ct[i], cy[i], true, "reliable", "transient_local");
    auto names = declare_parameter<std::vector<std::string>>("trigger_list", std::vector<std::string>{});
    named_triggers_ = !names.empty();
    if (names.empty()) {
      for (const auto & pair : metadata_) rules_["default"].insert(pair.first);
      names.push_back("default");
    } else {
      for (const auto & name : names) {
        if (!std::regex_match(name, std::regex("[A-Za-z][A-Za-z0-9_]*")) || rules_.count(name))
          throw std::runtime_error("invalid or duplicate trigger name: " + name);
        auto selected = declare_parameter<std::vector<std::string>>(
          "triggers." + name + ".topics", std::vector<std::string>{});
        if (selected.empty()) throw std::runtime_error("trigger has no topics: " + name);
        auto & rule = rules_[name];
        for (const auto & topic : selected) {
          if (!metadata_.count(topic)) throw std::runtime_error("unknown topic in trigger " + name + ": " + topic);
          if (!rule.insert(topic).second) throw std::runtime_error("duplicate topic in trigger " + name);
        }
      }
    }
    default_rule_ = declare_parameter<std::string>("default_trigger", names.front());
    if (!rules_.count(default_rule_)) throw std::runtime_error("default_trigger is not in trigger_list");
    trigger_ = create_subscription<std_msgs::msg::String>("~/trigger", 100,
      [this](std_msgs::msg::String::ConstSharedPtr m) {
        if (!named_triggers_) {
          auto p = m->data.find('|');
          enqueue(Input::TRIGGER, {}, p == std::string::npos ? "" : m->data.substr(0, p),
            p == std::string::npos ? m->data : m->data.substr(p + 1), default_rule_);
          return;
        }
        // Named mode: rule|trigger_id|reason, or just rule for an anonymous trigger.
        auto first = m->data.find('|');
        auto second = first == std::string::npos ? first : m->data.find('|', first + 1);
        if (first != std::string::npos && second == std::string::npos) {
          reject("invalid_format"); return;
        }
        enqueue(Input::TRIGGER, {}, second == std::string::npos ? "" : m->data.substr(first + 1, second - first - 1),
          second == std::string::npos ? "" : m->data.substr(second + 1), m->data.substr(0, first));
      });
    // These services reply only after the ordered event manager has made its decision.
    service_group_ = create_callback_group(rclcpp::CallbackGroupType::MutuallyExclusive);
    auto make_service = [this](const std::string & path, const std::string & rule) {
      return create_service<std_srvs::srv::Trigger>(path,
        [this, rule](std::shared_ptr<std_srvs::srv::Trigger::Request>,
                     std::shared_ptr<std_srvs::srv::Trigger::Response> response) {
          auto completion = std::make_shared<std::promise<TriggerDecision>>();
          auto result = completion->get_future();
          TriggerDecision decision{false, "", "input_queue_full"};
          if (enqueue(Input::TRIGGER, {}, "", "service", rule, completion)) decision = result.get();
          response->success = decision.accepted;
          YAML::Node details;
          details["accepted"] = decision.accepted; details["rule"] = yaml_string(rule);
          details["event_id"] = decision.event_id; details["reason"] = decision.reason;
          response->message = dump_yaml(details);
        }, rmw_qos_profile_services_default, service_group_);
    };
    service_ = make_service("~/trigger", default_rule_);
    for (const auto & rule : rules_)
      rule_services_.push_back(make_service("~/triggers/" + rule.first, rule.first));
    status_publisher_ = create_publisher<std_msgs::msg::String>(
      "~/status", rclcpp::QoS(1).reliable().transient_local());
    // Capture the effective startup configuration; runtime parameter edits do not hot-reload it.
    for (const auto & parameter : get_parameters(list_parameters({}, 100).names)) {
      auto name = parameter.get_name();
      switch (parameter.get_type()) {
        case rclcpp::ParameterType::PARAMETER_BOOL: configuration_[name] = parameter.as_bool(); break;
        case rclcpp::ParameterType::PARAMETER_INTEGER: configuration_[name] = parameter.as_int(); break;
        case rclcpp::ParameterType::PARAMETER_DOUBLE: configuration_[name] = parameter.as_double(); break;
        case rclcpp::ParameterType::PARAMETER_STRING: configuration_[name] = yaml_string(parameter.as_string()); break;
        case rclcpp::ParameterType::PARAMETER_STRING_ARRAY:
          configuration_[name] = YAML::Node(YAML::NodeType::Sequence);
          for (const auto & value : parameter.as_string_array()) configuration_[name].push_back(yaml_string(value));
          break;
        default: break;
      }
    }
    timer_ = create_wall_timer(std::chrono::milliseconds(20), [this] {enqueue(Input::TICK);});
    started_ = steady_now();
    run_id_ = "event_" + std::to_string(now().nanoseconds()) + "_" + std::to_string(started_);
    writer_ = std::thread([this] {write_loop();});
    manager_ = std::thread([this] {manage_loop();});
    RCLCPP_INFO(get_logger(), "Ready: %s, trigger ~/trigger, pre %.1fs post %.1fs",
      storage_.c_str(), pre_/1e9, post_/1e9);
  }
  ~Recorder() override { stop(); }
  void stop() {
    { std::lock_guard<std::mutex> lock(input_mutex_);
      if (stopped_) return;
      stopped_ = true;
      Input in{Input::STOP, {}, "", "", "", {}}; in.msg.steady = steady_now(); input_.push_back(in);
    }
    input_cv_.notify_one();
    if (manager_.joinable()) manager_.join();
    { std::lock_guard<std::mutex> lock(write_mutex_); writer_stop_ = true; }
    write_cv_.notify_one();
    if (writer_.joinable()) writer_.join();
  }
 private:
  void reject(const std::string & reason) {
    ++trigger_drops_;
    RCLCPP_WARN(get_logger(), "Trigger rejected: %s", reason.c_str());
  }
  bool selected(const std::string & topic) const {
    return rules_.at(active_->rule).count(topic) != 0;
  }
  bool lost_context() const {
    for (const auto & topic : rules_.at(active_->rule)) if (context_lost_topics_.count(topic)) return true;
    return false;
  }
  void subscribe(const std::string & topic, const std::string & type, bool context,
                 const std::string & reliability, const std::string & durability) {
    if (topic.empty() || topic[0] != '/' || metadata_.count(topic)) throw std::runtime_error("duplicate or non-absolute topic");
    rclcpp::QoS qos(100);
    if (reliability == "reliable") qos.reliable();
    else if (reliability == "best_effort") qos.best_effort();
    else throw std::runtime_error("invalid reliability");
    if (durability == "transient_local") qos.transient_local();
    else if (durability != "volatile") throw std::runtime_error("invalid durability");
    rosbag2_storage::TopicMetadata tm;
    tm.name = topic; tm.type = type; tm.serialization_format = "cdr";
    // rosbag2 uses these profiles when choosing playback publisher QoS.
    YAML::Node profile;
    profile["history"] = 1; profile["depth"] = 100;
    profile["reliability"] = reliability == "reliable" ? 1 : 2;
    profile["durability"] = durability == "transient_local" ? 1 : 2;
    for (const auto * key : {"deadline", "lifespan", "liveliness_lease_duration"}) {
      profile[key]["sec"] = 9223372036LL; profile[key]["nsec"] = 854775807;
    }
    profile["liveliness"] = 1; profile["avoid_ros_namespace_conventions"] = false;
    YAML::Node profiles; profiles.push_back(profile); tm.offered_qos_profiles = dump_yaml(profiles);
    metadata_[topic] = tm;
    subscriptions_.push_back(create_generic_subscription(topic, type, qos,
      [this, topic, type, context](std::shared_ptr<rclcpp::SerializedMessage> data) {
        Message m; m.topic = topic; m.type = type; m.data = data; m.context = context;
        enqueue(Input::DATA, m);
      }));
  }
  bool enqueue(Input::Kind kind, Message m = {}, std::string id = "", std::string reason = "", std::string rule = "",
               std::shared_ptr<std::promise<TriggerDecision>> completion = {}) {
    std::lock_guard<std::mutex> lock(input_mutex_);
    if (stopped_) return false;
    m.steady = steady_now(); m.ros = now().nanoseconds(); m.seq = ++sequence_;
    size_t bytes = m.bytes() + id.size() + reason.size() + rule.size();
    if (input_.size() >= items_ || input_bytes_ + bytes > input_limit_) {
      if (kind == Input::DATA) { last_input_drops_[m.topic] = m.steady; ++input_drops_; }
      if (kind == Input::TRIGGER) {
        ++trigger_drops_; RCLCPP_ERROR(get_logger(), "Trigger rejected: input queue full");
      }
      return false;
    }
    input_bytes_ += bytes;
    input_.push_back({kind, m, std::move(id), std::move(reason), std::move(rule), std::move(completion)}); input_cv_.notify_one(); return true;
  }
  bool job(Job j) {
    std::lock_guard<std::mutex> lock(write_mutex_);
    if (j.kind == Job::WRITE) {
      if (write_bytes_ + j.msg.bytes() > writer_limit_ || write_data_items_ >= items_) return false;
      write_bytes_ += j.msg.bytes(); ++write_data_items_;
    }
    jobs_.push_back(std::move(j)); write_cv_.notify_one(); return true;
  }
  void send(const Message & m, bool context = false) {
    if (!selected(m.topic)) return;
    Message copy = m; copy.context = context;
    if (!job({Job::WRITE, active_->id, copy, {}})) {
      active_->errors.insert("writer_queue_overflow");
      auto n = active_->meta["writer_dropped_messages"];
      active_->meta["writer_dropped_messages"] = n ? n.as<uint64_t>() + 1 : 1;
    }
  }
  void context_store(const Message & m) {
    auto store = [this](const std::string & key, Message value) {
      auto it = context_.find(key);
      if (it != context_.end()) {context_bytes_ -= it->second.bytes(); context_.erase(it);}
      if (context_bytes_ + value.bytes() > context_limit_ || context_.size() >= items_) {
        context_lost_topics_.insert(value.topic); return;
      }
      context_bytes_ += value.bytes(); context_[key] = std::move(value);
    };
    if (m.type == "tf2_msgs/msg/TFMessage") {
      tf2_msgs::msg::TFMessage tf;
      rclcpp::Serialization<tf2_msgs::msg::TFMessage> serializer;
      serializer.deserialize_message(m.data.get(), &tf);
      for (auto & transform : tf.transforms) {
        tf2_msgs::msg::TFMessage one; one.transforms.push_back(transform);
        Message copy = m; copy.data = std::make_shared<rclcpp::SerializedMessage>();
        serializer.serialize_message(&one, copy.data.get());
        store(m.topic + "/" + transform.child_frame_id, copy);
      }
    } else store(m.topic, m);
  }
  void prune(Ns t) {
    while (!history_.empty() && (history_.front().steady < t - retention_ ||
           history_bytes_ > history_limit_ || history_.size() > items_)) {
      const auto & m = history_.front();
      if (m.steady >= t - retention_) last_history_drops_[m.topic] = m.steady;
      if (m.context) context_store(m); // baseline immediately before retained history
      history_bytes_ -= m.bytes(); history_.pop_front();
    }
  }
  void finish(const std::string & reason = "") {
    if (!active_) return;
    if (!reason.empty()) active_->errors.insert(reason);
    {
      std::lock_guard<std::mutex> lock(input_mutex_);
      for (const auto & topic : rules_.at(active_->rule)) {
        if (last_input_drops_[topic] >= active_->start) active_->errors.insert("input_queue_overflow");
      }
    }
    active_->meta["input_drops_total"] = input_drops_.load();
    active_->meta["trigger_rejections_total"] = trigger_drops_.load();
    active_->meta["window_end_steady_ns"] = active_->end;
    active_->meta["errors"] = YAML::Node(YAML::NodeType::Sequence);
    for (const auto & error : active_->errors) active_->meta["errors"].push_back(error);
    active_->meta["data_complete"] = active_->errors.empty();
    ready_after_ = active_->end + cooldown_;
    active_->meta["next_trigger_not_before_steady_ns"] = ready_after_;
    job({Job::CLOSE, active_->id, {}, YAML::Clone(active_->meta)});
    active_.reset();
  }
  TriggerDecision trigger(const Input & in) {
    auto t = in.msg.steady;
    auto declined = [this](const std::string & reason) {
      reject(reason); return TriggerDecision{false, "", reason};
    };
    if (!rules_.count(in.rule)) {return declined("unknown_rule: " + in.rule);}
    if (active_) {return declined("busy");}
    if (t < ready_after_) {return declined("cooldown");}
    if (pending_.load() != 0) {return declined("finalizing");}
    for (auto it = seen_.begin(); it != seen_.end();) {
      if (it->second < t - dedup_) {seen_bytes_ -= it->first.size() + 64; it = seen_.erase(it);}
      else ++it;
    }
    // A rejected trigger is never inserted into the dedup cache and is never deferred.
    std::string key = in.rule + "\n" + in.id;
    if (!in.id.empty()) {
      if (seen_.count(key)) {return declined("duplicate_id");}
      if (seen_.size() >= items_ || seen_bytes_ + key.size() + 64 > input_limit_) {
        return declined("dedup_capacity");
      }
      seen_[key] = t; seen_bytes_ += key.size() + 64;
    }
    {
      ++pending_;
      active_ = std::make_unique<Event>();
      active_->rule = in.rule;
      active_->id = run_id_ + "_" + in.rule + "_" + std::to_string(++events_);
      active_->start = t - pre_; active_->end = t + post_;
      auto & meta = active_->meta;
      meta["config_version"] = 2;
      meta["trigger_rule"] = yaml_string(in.rule);
      meta["trigger_policy"] = "single_active_reject_busy";
      meta["trigger_cooldown_seconds"] = cooldown_/1e9;
      meta["event_id"] = active_->id; meta["software_version"] = "0.3.0";
      meta["time_basis"] = "receive_steady"; meta["bag_timestamp_basis"] = "receive_ros";
      meta["window_start_steady_ns"] = active_->start;
      meta["pre_trigger_seconds"] = pre_/1e9; meta["post_trigger_seconds"] = post_/1e9;
      meta["history_retention_seconds"] = retention_/1e9;
      meta["max_event_seconds"] = maximum_/1e9; meta["storage_id"] = storage_;
      meta["history_bytes"] = history_limit_; meta["input_bytes"] = input_limit_;
      meta["writer_bytes"] = writer_limit_; meta["context_bytes"] = context_limit_;
      meta["minimum_free_bytes"] = free_limit_; meta["queue_items"] = items_;
      meta["max_pending_events"] = pending_limit_; meta["dedup_seconds"] = dedup_/1e9;
      for (const auto & pair : metadata_) {
        if (!selected(pair.first)) continue;
        meta["topics"][pair.first]["type"] = pair.second.type;
        meta["topics"][pair.first]["qos"] = YAML::Load(pair.second.offered_qos_profiles);
      }
      bool enough = started_ <= active_->start;
      for (const auto & topic : rules_.at(in.rule))
        if (last_history_drops_[topic] >= active_->start) enough = false;
      meta["prehistory_available"] = enough;
      if (!enough) active_->errors.insert("insufficient_prehistory");
      if (lost_context()) active_->errors.insert("context_cache_overflow");
      job({Job::OPEN, active_->id, {}, YAML::Clone(meta)});
      // Build context as of window start, then replay updates in the strict window.
      auto saved = context_; auto saved_bytes = context_bytes_;
      for (const auto & m : history_) if (m.context && m.steady < active_->start) context_store(m);
      for (const auto & pair : context_) send(pair.second, true);
      context_ = std::move(saved); context_bytes_ = saved_bytes;
      if (lost_context()) active_->errors.insert("context_cache_overflow");
      for (const auto & m : history_) if (m.steady >= active_->start) send(m);
      RCLCPP_INFO(get_logger(), "CAPTURING %s", active_->id.c_str());
    }
    YAML::Node tr; tr["rule"] = yaml_string(in.rule); tr["id"] = yaml_string(in.id); tr["reason"] = yaml_string(in.reason);
    tr["receive_steady_ns"] = t; tr["receive_ros_ns"] = in.msg.ros;
    active_->meta["triggers"].push_back(tr);
    return {true, active_->id, "accepted"};
  }
  void manage_loop() {
    for (;;) {
      Input in;
      { std::unique_lock<std::mutex> lock(input_mutex_);
        input_cv_.wait(lock, [this] {return !input_.empty();});
        in = std::move(input_.front()); input_.pop_front();
        if (in.kind != Input::STOP) input_bytes_ -= in.msg.bytes() + in.id.size() + in.reason.size() + in.rule.size();
      }
      TriggerDecision decision;
      try {
        if (active_ && in.msg.steady > active_->end) finish();
        if (in.kind == Input::STOP) {finish("shutdown_truncated"); break;}
        prune(in.msg.steady);
        if (last_steady_ && std::abs((in.msg.ros - last_ros_) - (in.msg.steady - last_steady_)) > 100000000) {
          if (active_) active_->errors.insert("ros_clock_jump");
          last_jump_ = in.msg.steady;
        }
        last_steady_ = in.msg.steady; last_ros_ = in.msg.ros;
        if (in.kind == Input::DATA) {
          if (active_) send(in.msg);
          history_.push_back(in.msg); history_bytes_ += in.msg.bytes(); prune(in.msg.steady);
        } else if (in.kind == Input::TRIGGER) decision = trigger(in);
        if (active_ && last_jump_ >= active_->start) active_->errors.insert("ros_clock_jump");
      } catch (const std::exception & e) {
        RCLCPP_ERROR(get_logger(), "Manager: %s", e.what()); finish("manager_error");
      }
      if (in.completion) in.completion->set_value(decision);
      publish_status(in.kind == Input::TRIGGER);
    }
  }
  void publish_status(bool force = false) {
    Ns t = steady_now();
    if (!force && t - last_status_ < 200000000) return;
    last_status_ = t;
    if (!rclcpp::ok()) return;
    try {
      YAML::Node status;
      status["run_id"] = run_id_; status["receive_steady_ns"] = t;
      status["state"] = active_ ? "CAPTURING" : pending_.load() ? "FINALIZING" :
        t < ready_after_ ? "COOLDOWN" : "READY";
      status["active_event_id"] = active_ ? active_->id : "";
      status["active_rule"] = yaml_string(active_ ? active_->rule : "");
      status["remaining_seconds"] = active_ ? std::max<Ns>(0, active_->end - t)/1e9 : 0.0;
      status["cooldown_remaining_seconds"] = std::max<Ns>(0, ready_after_ - t)/1e9;
      status["history_bytes"] = history_bytes_; status["history_limit_bytes"] = history_limit_;
      status["history_span_seconds"] = history_.empty() ? 0.0 : (t - history_.front().steady)/1e9;
      status["prehistory_ready"] = t - started_ >= pre_;
      status["input_drops"] = input_drops_.load(); status["trigger_rejections"] = trigger_drops_.load();
      status["output_directory"] = std::filesystem::absolute(output_).string();
      status["storage_id"] = storage_; status["default_trigger"] = yaml_string(default_rule_);
      status["configuration"] = configuration_;
      YAML::Node rule_list(YAML::NodeType::Map);
      for (const auto & rule : rules_) {
        YAML::Node topics(YAML::NodeType::Sequence);
        for (const auto & topic : rule.second) topics.push_back(topic);
        rule_list.force_insert(yaml_string(rule.first), topics);
      }
      status["rules"] = rule_list;
      {
        std::lock_guard<std::mutex> lock(input_mutex_);
        status["input_queue_bytes"] = input_bytes_; status["input_queue_items"] = input_.size();
      }
      {
        std::lock_guard<std::mutex> lock(write_mutex_);
        status["writer_queue_bytes"] = write_bytes_; status["writer_queue_items"] = jobs_.size();
      }
      {
        std::lock_guard<std::mutex> lock(result_mutex_);
        if (!last_result_.empty()) status["last_event"] = YAML::Load(last_result_);
      }
      std_msgs::msg::String msg; msg.data = dump_yaml(status); status_publisher_->publish(msg);
    } catch (const std::exception & e) {
      RCLCPP_WARN(get_logger(), "Status publish failed: %s", e.what());
    }
  }
  void save_meta(const std::string & id, const YAML::Node & meta) {
    auto path = std::filesystem::path(output_) / (id + ".event.yaml");
    std::ofstream out(path.string() + ".tmp");
    out.exceptions(std::ios::badbit | std::ios::failbit); out << dump_yaml(meta); out.close();
    std::filesystem::rename(path.string() + ".tmp", path);
  }
  void write_loop() {
    std::unique_ptr<rosbag2_cpp::Writer> writer;
    YAML::Node stats;
    std::string error;
    for (;;) {
      Job j;
      { std::unique_lock<std::mutex> lock(write_mutex_);
        write_cv_.wait(lock, [this] {return writer_stop_ || !jobs_.empty();});
        if (jobs_.empty()) break;
        j = std::move(jobs_.front()); jobs_.pop_front();
        if (j.kind == Job::WRITE) {write_bytes_ -= j.msg.bytes(); --write_data_items_;}
      }
      try {
        if (j.kind == Job::OPEN) {
          stats = YAML::Node(); error.clear();
          YAML::Node initial; initial["event_id"] = j.id; initial["status"] = "CAPTURING";
          save_meta(j.id, initial);
          if (std::filesystem::space(output_).available < free_limit_) throw std::runtime_error("minimum free disk space violated");
          writer = std::make_unique<rosbag2_cpp::Writer>();
          rosbag2_storage::StorageOptions options;
          options.uri = (std::filesystem::path(output_) / j.id).string(); options.storage_id = storage_;
          options.max_cache_size = 0;
          writer->open(options, {"cdr", "cdr"});
          for (const auto & pair : j.meta["topics"])
            writer->create_topic(metadata_.at(pair.first.as<std::string>()));
        } else if (j.kind == Job::WRITE && error.empty()) {
          if (std::filesystem::space(output_).available < free_limit_) throw std::runtime_error("minimum free disk space violated");
          auto bag = std::make_shared<rosbag2_storage::SerializedBagMessage>();
          // Alias payload ownership: Writer must not consume the ring's SerializedMessage.
          bag->serialized_data = std::shared_ptr<rcutils_uint8_array_t>(j.msg.data,
            &j.msg.data->get_rcl_serialized_message());
          bag->topic_name = j.msg.topic; bag->time_stamp = j.msg.ros;
          writer->write(bag);
          auto topic = stats[j.msg.topic];
          topic["message_count"] = topic["message_count"] ? topic["message_count"].as<uint64_t>() + 1 : 1;
          if (j.msg.context) {
            YAML::Node ctx; ctx["sequence_id"] = j.msg.seq;
            ctx["original_receive_ros_ns"] = j.msg.ros; ctx["original_receive_steady_ns"] = j.msg.steady;
            ctx["injection"] = "original_timestamp_before_window";
            topic["context"].push_back(ctx);
          } else {
            if (!topic["first_receive_steady_ns"]) {
              topic["first_receive_steady_ns"] = j.msg.steady; topic["first_sequence_id"] = j.msg.seq;
            }
            topic["last_receive_steady_ns"] = j.msg.steady; topic["last_sequence_id"] = j.msg.seq;
          }
        }
      } catch (const std::exception & e) {
        error = e.what(); RCLCPP_ERROR(get_logger(), "Writer %s: %s", j.id.c_str(), e.what());
      }
      if (j.kind == Job::CLOSE) {
        try {if (writer) writer->close();}
        catch (const std::exception & e) {error = e.what();}
        writer.reset();
        j.meta["written_topics"] = stats;
        if (!error.empty()) {j.meta["errors"].push_back(error); j.meta["data_complete"] = false;}
        j.meta["status"] = !error.empty() ? "FAILED" : j.meta["data_complete"].as<bool>() ? "COMPLETED" : "PARTIAL";
        try {save_meta(j.id, j.meta);}
        catch (const std::exception & e) {
          j.meta["status"] = "FAILED"; j.meta["data_complete"] = false;
          j.meta["errors"].push_back(std::string("metadata_save_failed: ") + e.what());
          RCLCPP_ERROR(get_logger(), "Metadata save failed: %s", e.what());
        }
        {
          YAML::Node summary;
          for (const auto * field : {"event_id", "trigger_rule", "status", "data_complete", "errors"})
            summary[field] = j.meta[field];
          std::lock_guard<std::mutex> lock(result_mutex_); last_result_ = dump_yaml(summary);
        }
        RCLCPP_INFO(get_logger(), "%s %s", j.meta["status"].as<std::string>().c_str(), j.id.c_str());
        --pending_;
      }
    }
  }
  Ns pre_, post_, retention_, maximum_, dedup_, cooldown_, ready_after_{}, started_{}, last_jump_{};
  Ns last_steady_{}, last_ros_{}, last_status_{};
  YAML::Node configuration_;
  std::mutex result_mutex_;
  std::string last_result_;
  size_t history_limit_, input_limit_, writer_limit_, context_limit_, items_, pending_limit_, free_limit_;
  size_t seen_bytes_{}, input_bytes_{}, history_bytes_{}, write_bytes_{}, context_bytes_{}, write_data_items_{};
  uint64_t sequence_{}, events_{};
  std::map<std::string, Ns> last_input_drops_, last_history_drops_;
  std::map<std::string, std::set<std::string>> rules_;
  std::set<std::string> context_lost_topics_;
  std::atomic<uint64_t> input_drops_{0}, trigger_drops_{0};
  std::atomic<size_t> pending_{0};
  bool stopped_{}, writer_stop_{}, named_triggers_{};
  std::string output_, storage_, run_id_, default_rule_;
  std::mutex input_mutex_, write_mutex_;
  std::condition_variable input_cv_, write_cv_;
  std::deque<Input> input_;
  std::deque<Message> history_;
  std::deque<Job> jobs_;
  std::map<std::string, Message> context_;
  std::map<std::string, Ns> seen_;
  std::map<std::string, rosbag2_storage::TopicMetadata> metadata_;
  std::unique_ptr<Event> active_;
  std::thread manager_, writer_;
  std::vector<rclcpp::GenericSubscription::SharedPtr> subscriptions_;
  rclcpp::Subscription<std_msgs::msg::String>::SharedPtr trigger_;
  rclcpp::CallbackGroup::SharedPtr service_group_;
  rclcpp::Service<std_srvs::srv::Trigger>::SharedPtr service_;
  std::vector<rclcpp::Service<std_srvs::srv::Trigger>::SharedPtr> rule_services_;
  rclcpp::Publisher<std_msgs::msg::String>::SharedPtr status_publisher_;
  rclcpp::TimerBase::SharedPtr timer_;
};
int main(int argc, char ** argv) {
  rclcpp::init(argc, argv);
  try {
    auto node = std::make_shared<Recorder>();
    // A service waiting for its ordered decision must not block data subscription callbacks.
    rclcpp::executors::MultiThreadedExecutor executor(rclcpp::ExecutorOptions(), 2);
    executor.add_node(node); executor.spin(); node->stop();
  }
  catch (const std::exception & e) {std::cerr << e.what() << '\n'; rclcpp::shutdown(); return 1;}
  rclcpp::shutdown(); return 0;
}
