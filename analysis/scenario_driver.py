#!/usr/bin/env python3
"""Play the scripted command sequence on /cmd_vel using simulation time.

Publishes the commanded Twist at 20 Hz and the current segment name on /scenario/segment
whenever it changes. Run inside the container with use_sim_time so segments follow the
simulator clock, not the wall clock.
"""
from __future__ import annotations

import os
import sys

import rclpy
from geometry_msgs.msg import Twist
from rclpy.node import Node
from rclpy.parameter import Parameter
from std_msgs.msg import String

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from scenario import SCENARIO_VERSION, segment_at, total_duration  # noqa: E402

PUBLISH_HZ = 20.0
STOP_TAIL_SECONDS = 1.0


class ScenarioDriver(Node):
    def __init__(self) -> None:
        super().__init__("scenario_driver", parameter_overrides=[Parameter("use_sim_time", value=True)])
        self.cmd_pub = self.create_publisher(Twist, "/cmd_vel", 10)
        self.seg_pub = self.create_publisher(String, "/scenario/segment", 10)
        self.t0: float | None = None
        self.current: str | None = None
        self.done = False
        self.create_timer(1.0 / PUBLISH_HZ, self.step)
        self.get_logger().info(f"scenario v{SCENARIO_VERSION}, {total_duration():.0f} s")

    def now_s(self) -> float:
        return self.get_clock().now().nanoseconds * 1e-9

    def publish_segment(self, name: str) -> None:
        if name != self.current:
            self.current = name
            self.seg_pub.publish(String(data=name))
            self.get_logger().info(f"segment {name}")

    def step(self) -> None:
        now = self.now_s()
        if now <= 0.0:  # no /clock received yet
            return
        if self.t0 is None:
            self.t0 = now
        t = now - self.t0
        seg = segment_at(t)
        cmd = Twist()
        if seg is not None:
            cmd.linear.x, cmd.linear.y, cmd.angular.z = seg.vx, seg.vy, seg.wz
            self.publish_segment(seg.name)
        else:
            self.publish_segment("end")
            if t >= total_duration() + STOP_TAIL_SECONDS:
                self.done = True
        self.cmd_pub.publish(cmd)


def main() -> int:
    rclpy.init()
    node = ScenarioDriver()
    while rclpy.ok() and not node.done:
        rclpy.spin_once(node, timeout_sec=0.1)
    node.destroy_node()
    rclpy.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
