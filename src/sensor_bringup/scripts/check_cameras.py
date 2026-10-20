"""Check that all configured Orbbec RGB topics publish a frame."""

import sys
import time

import rclpy
from rclpy.node import Node
from rclpy.signals import SignalHandlerOptions
from rclpy.qos import HistoryPolicy, QoSProfile, ReliabilityPolicy
from sensor_msgs.msg import Image

from camera_config import load_config, topic_map


class HealthCheck(Node):
    def __init__(self, topics):
        super().__init__("camera_health_check")
        self.frames = {name: 0 for name in topics}
        qos = QoSProfile(depth=10, history=HistoryPolicy.KEEP_LAST,
                         reliability=ReliabilityPolicy.BEST_EFFORT)
        for name, topic in topics.items():
            self.create_subscription(Image, topic, lambda _msg, key=name: self._received(key), qos)

    def _received(self, name):
        self.frames[name] += 1


def check_topics(topics, timeout):
    if not topics:
        return False
    rclpy.init(signal_handler_options=SignalHandlerOptions.NO)
    node = HealthCheck(topics)
    started = time.monotonic()
    try:
        while time.monotonic() - started < timeout:
            rclpy.spin_once(node, timeout_sec=0.1)
            if all(count > 0 for count in node.frames.values()):
                for name, count in node.frames.items():
                    node.get_logger().info(f"{name}: OK ({count} frames)")
                return True
        for name, count in node.frames.items():
            node.get_logger().info(f"{name}: {'OK' if count else 'SILENT'} ({count} frames)")
        return False
    finally:
        node.destroy_node()
        rclpy.shutdown()


def _load_config():
    return load_config()


def _enabled_topics(config, mode="color"):
    return topic_map(config, mode)


def main():
    timeout = float(sys.argv[1]) if len(sys.argv) > 1 else 15.0
    topics = _enabled_topics(_load_config())
    if not topics:
        print("RESULT: no enabled cameras", file=sys.stderr)
        return 1
    ok = check_topics(topics, timeout)
    print(f"RESULT: {'all cameras publishing' if ok else 'one or more cameras silent'}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
