// Standalone yaml-cpp lifetime/performance reproducer; no ROS or sensor input.
// Run each mode in a separate process so allocator history does not cross modes.
// g++ -O2 -std=c++17 tests/reproduce_yaml_status.cpp -lyaml-cpp -o /tmp/yaml_status
// /tmp/yaml_status shared src/event_recorder/config/recorder.yaml
// /tmp/yaml_status clone  src/event_recorder/config/recorder.yaml
#include <yaml-cpp/yaml.h>
#include <chrono>
#include <fstream>
#include <iostream>
#include <stdexcept>
#include <string>
#include <unistd.h>

static long rss_bytes() {
  long total = 0, resident = 0;
  std::ifstream statm("/proc/self/statm");
  if (!(statm >> total >> resident)) throw std::runtime_error("Cannot read RSS");
  return resident * sysconf(_SC_PAGESIZE);
}

int main(int argc, char ** argv) {
  if (argc != 3 || (std::string(argv[1]) != "shared" && std::string(argv[1]) != "clone")) {
    std::cerr << "Usage: yaml_status shared|clone recorder.yaml\n";
    return 2;
  }
  const bool clone = std::string(argv[1]) == "clone";
  // Detach from the input document; only this configuration lives across snapshots.
  const YAML::Node configuration = YAML::Clone(
    YAML::LoadFile(argv[2])["event_recorder"]["ros__parameters"]);
  const std::string expected_configuration = YAML::Dump(configuration);
  std::cout << "{\"mode\":\"" << argv[1] << "\",\"batch_size\":500,\"initial_rss_bytes\":"
            << rss_bytes() << ",\"batches\":[" << std::flush;
  std::string serialized;
  for (int batch = 0; batch < 10; ++batch) {
    const auto begin = std::chrono::steady_clock::now();
    for (int i = 0; i < 500; ++i) {
      YAML::Node status;
      status["state"] = "READY";
      status["receive_steady_ns"] = batch * 500 + i;
      status["history_bytes"] = 268435456;
      status["input_queue_bytes"] = 0;
      status["writer_queue_bytes"] = 0;
      // The only difference between the two experimental arms.
      if (clone) status["configuration"] = YAML::Clone(configuration);
      else status["configuration"] = configuration;
      YAML::Node rules;
      for (const auto & rule : configuration["trigger_list"]) {
        YAML::Node topics(YAML::NodeType::Sequence);
        topics.push_back("/example/data");
        rules[rule.as<std::string>()] = topics;
      }
      status["rules"] = rules;
      serialized = YAML::Dump(status);
    }
    const double seconds = std::chrono::duration<double>(
      std::chrono::steady_clock::now() - begin).count();
    // Compare data on the wire, rather than Node identity. No snapshot survives a batch.
    if (YAML::Dump(YAML::Load(serialized)["configuration"]) != expected_configuration)
      throw std::runtime_error("Configuration content changed");
    if (batch) std::cout << ',';
    std::cout << "{\"updates\":" << (batch + 1) * 500 << ",\"seconds\":" << seconds
              << ",\"rss_bytes\":" << rss_bytes() << '}' << std::flush;
  }
  if (clone) {
    YAML::Node snapshot;
    snapshot["configuration"] = YAML::Clone(configuration);
    snapshot["configuration"]["pre_trigger_seconds"] = 123;
    if (YAML::Dump(configuration) != expected_configuration)
      throw std::runtime_error("Snapshot aliases startup configuration");
  }
  std::cout << "],\"configuration_preserved\":true}\n";
}
