#!/usr/bin/python3
"""Publish one named recorder trigger after cumulative odometry travel exceeds a limit."""
import json
import re
import uuid

import rclpy
from rclpy.node import Node
from rclpy.qos import QoSProfile, ReliabilityPolicy, DurabilityPolicy
from nav_msgs.msg import Odometry
from std_msgs.msg import String
from std_srvs.srv import Trigger

from distance_accumulator import DistanceAccumulator


class OdomDistanceTrigger(Node):
    def __init__(self):
        super().__init__('odom_distance_trigger')
        self.odom_topic = self.declare_parameter('odom_topic', '/odom').value
        self.trigger_topic = self.declare_parameter('trigger_topic', '/event_recorder/trigger').value
        self.robot_type = self.declare_parameter('robot_type', 'bunker_mini').value
        threshold = self.declare_parameter('threshold_m', 20.0).value
        reliability = self.declare_parameter('qos_reliability', 'reliable').value
        if not re.fullmatch('[A-Za-z][A-Za-z0-9_]*', self.robot_type):
            raise ValueError('invalid robot_type')
        if reliability not in ('reliable', 'best_effort'):
            raise ValueError('qos_reliability must be reliable or best_effort')
        self.accumulator = DistanceAccumulator(threshold)
        self.request_id = 'odom_distance_' + uuid.uuid4().hex
        self.trigger_published = False
        self.crossing = None
        self.delivery_error = ''
        self.publisher = self.create_publisher(String, self.trigger_topic, 10)
        self.status_publisher = self.create_publisher(String, '~/status', QoSProfile(
            depth=1, reliability=ReliabilityPolicy.RELIABLE, durability=DurabilityPolicy.TRANSIENT_LOCAL))
        self.create_subscription(Odometry, self.odom_topic, self.on_odometry, QoSProfile(
            depth=1000, reliability=ReliabilityPolicy.RELIABLE if reliability == 'reliable' else ReliabilityPolicy.BEST_EFFORT))
        self.create_service(Trigger, '~/reset', self.on_reset)
        self.create_timer(0.5, self.publish_status)
        self.get_logger().info(f'ARMED: {self.odom_topic}, cumulative XY distance > {threshold:.3f} m '
                               f'→ {self.robot_type}; one trigger per start/reset')

    def on_reset(self, request, response):
        self.accumulator.reset()
        self.request_id = 'odom_distance_' + uuid.uuid4().hex
        self.trigger_published = False
        self.crossing = None
        self.delivery_error = ''
        response.success = True
        response.message = 'Reset; next valid odometry sample defines the new starting point'
        self.get_logger().info(response.message)
        self.publish_status()
        return response

    def on_odometry(self, msg):
        p = msg.pose.pose.position
        previous_fault = self.accumulator.fault
        details = self.accumulator.observe(p.x, p.y, msg.header.frame_id, msg.child_frame_id,
            msg.header.stamp.sec * 1_000_000_000 + msg.header.stamp.nanosec)
        if self.accumulator.fault and not previous_fault:
            self.get_logger().error(self.accumulator.fault)
            self.publish_status()
        if details is None:
            return
        details['odom_topic'] = self.odom_topic
        self.crossing = details
        # No delayed retries: a missing recorder must not silently shift the event window.
        if self.publisher.get_subscription_count() == 0:
            self.delivery_error = 'no_trigger_subscriber_at_crossing; start recorder and reset/replay'
            self.get_logger().error(self.delivery_error)
        else:
            reason = json.dumps(details, separators=(',', ':'), allow_nan=False)
            self.publisher.publish(String(data=f'{self.robot_type}|{self.request_id}|{reason}'))
            self.trigger_published = True
            self.get_logger().info(f'TRIGGER_PUBLISHED {self.request_id}: {details["distance_m"]:.6f} m '
                                   '(publication is not confirmation of recorder acceptance)')
        self.publish_status()

    def publish_status(self):
        a = self.accumulator
        state = 'FAULT' if a.fault else 'UNDELIVERED' if self.delivery_error else 'TRIGGER_PUBLISHED' if self.trigger_published else 'ARMED'
        status = {
            'state': state, 'robot_type': self.robot_type, 'request_id': self.request_id,
            'odom_topic': self.odom_topic, 'threshold_m': a.threshold_m,
            'distance_m': a.distance_m, 'samples': a.samples,
            'invalid_samples': a.invalid_samples, 'duplicate_samples': a.duplicate_samples,
            'source_stamp_ns': a.last_stamp_ns, 'trigger_published': self.trigger_published,
            'error': a.fault or self.delivery_error, 'crossing': self.crossing,
        }
        self.status_publisher.publish(String(data=json.dumps(status, allow_nan=False)))


def main():
    rclpy.init()
    node = None
    try:
        node = OdomDistanceTrigger()
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        if node is not None:
            node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()


if __name__ == '__main__':
    main()
