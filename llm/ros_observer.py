"""Container-side observation only; ROS imports stay outside host-side tests."""
from __future__ import annotations

import argparse
from dataclasses import dataclass, replace
import json
import math
import sqlite3
import subprocess
import time
from pathlib import Path


def bag_topic_counts(path: Path) -> dict[str, int]:
    counts = {}
    for db in path.glob('*.db3'):
        try:
            with sqlite3.connect(f'{db.as_uri()}?mode=ro', uri=True, timeout=.5) as connection:
                rows = connection.execute('SELECT topics.name, COUNT(messages.topic_id) FROM topics '
                                          'LEFT JOIN messages ON topics.id = messages.topic_id GROUP BY topics.id')
                for topic, count in rows:
                    counts[topic] = counts.get(topic, 0) + count
        except sqlite3.Error:
            # The recorder may still be creating its schema on the next poll.
            continue
    return counts


def quaternion_pose(t, x, y, z, qx, qy, qz, qw):
    norm = math.hypot(qx, qy, qz, qw)
    if not math.isfinite(norm) or norm < 1e-12:
        return {'t': t, 'x': x, 'y': y, 'z': z, 'yaw': float('nan'), 'tilt': float('nan')}
    qx, qy, qz, qw = (value / norm for value in (qx, qy, qz, qw))
    yaw = math.atan2(2 * (qw * qz + qx * qy), 1 - 2 * (qy * qy + qz * qz))
    tilt = math.degrees(math.acos(max(-1, min(1, 1 - 2 * (qx * qx + qy * qy)))))
    return {'t': t, 'x': x, 'y': y, 'z': z, 'yaw': yaw, 'tilt': tilt}


def preflight() -> dict:
    """Check the same ROS runtime as observation, without requiring a simulator."""
    try:
        import rclpy
        from nav_msgs.msg import Odometry
        from rclpy.qos import qos_profile_sensor_data
        from rosgraph_msgs.msg import Clock
        from llm.recording import standing_start

        rclpy.init()
        try:
            node = rclpy.create_node('language_observer_preflight')
            node.destroy_node()
        finally:
            rclpy.shutdown()
        return {'status': 'ready'}
    except Exception as exc:
        return {'status': 'observer_preflight_failed',
                'failure_reason': f'ROS observer startup failed ({type(exc).__name__}); source ROS and workspace setup and use /usr/bin/python3 with ROS PYTHONPATH preserved.'}


MAX_POSE_SAMPLES = 2048
MAX_WALL_AGE_S = .5
MAX_CLOCK_ODOM_LAG_S = .25
WINDOW_KEEP_S = 3.2


def _finite_json(value):
    if isinstance(value, dict):
        return {key: _finite_json(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_finite_json(item) for item in value]
    return None if isinstance(value, float) and not math.isfinite(value) else value


@dataclass(frozen=True)
class ObservationState:
    """Bounded samples from one simulation epoch, with independent wall freshness."""

    samples: tuple = ()
    last_pose: dict | None = None
    last_accepted_t: float | None = None
    clock_t: float | None = None
    clock_first_t: float | None = None
    clock_received_at: float | None = None
    clock_advanced_at: float | None = None
    pose_received_at: float | None = None
    stream_issue: str | None = None
    frame_id: str | None = None
    child_frame_id: str | None = None

    def with_clock(self, t, *, received_at):
        if not isinstance(t, (int, float)) or not math.isfinite(t) or t < 0:
            return replace(self, clock_t=None, samples=(), last_accepted_t=None,
                           clock_received_at=received_at, stream_issue='invalid_clock')
        reset = self.clock_t is not None and t < self.clock_t
        initial = self.clock_t is None
        changes = {'clock_t': t, 'clock_received_at': received_at}
        if initial or reset or t > self.clock_t:
            changes = {**changes, 'clock_advanced_at': received_at}
        if initial or reset:
            changes = {**changes, 'samples': (), 'last_accepted_t': None,
                       'clock_first_t': t, 'stream_issue': 'clock_reset' if reset else None}
        return replace(self, **changes)

    def with_pose(self, pose, *, received_at, frame_id=None, child_frame_id=None):
        from llm.recording import valid_pose
        frame_id = self.frame_id if frame_id is None else frame_id
        child_frame_id = self.child_frame_id if child_frame_id is None else child_frame_id
        changes = {'last_pose': dict(pose), 'pose_received_at': received_at,
                   'frame_id': frame_id, 'child_frame_id': child_frame_id}
        reason = None
        if not valid_pose(pose):
            reason = 'invalid_sample'
        elif self.clock_t is None:
            reason = 'clock_missing'
        elif abs(self.clock_t - pose['t']) > MAX_CLOCK_ODOM_LAG_S:
            reason = 'simulation_time_mismatch'
        elif self.last_accepted_t is not None and pose['t'] <= self.last_accepted_t:
            reason = 'odom_not_advancing'
        if reason:
            return replace(self, **changes, samples=(), stream_issue=reason)
        previous_frames = (self.frame_id, self.child_frame_id)
        frames_changed = any(previous_frames) and previous_frames != (frame_id, child_frame_id)
        gap = self.last_accepted_t is not None and pose['t'] - self.last_accepted_t > .5
        issue = ('frame_changed' if frames_changed else 'invalid_sample' if gap
                 else self.stream_issue if self.stream_issue == 'clock_reset' else None)
        previous = () if gap or frames_changed else self.samples
        samples = tuple(s for s in previous if s['t'] >= pose['t'] - WINDOW_KEEP_S)
        return replace(self, **changes, samples=(*samples, dict(pose))[-MAX_POSE_SAMPLES:],
                       last_accepted_t=pose['t'], stream_issue=issue)

    def _stream_reason(self, now):
        if self.last_pose is None:
            return 'pose_missing'
        if self.clock_t is None:
            return 'invalid_clock' if self.stream_issue == 'invalid_clock' else 'clock_missing'
        if now - self.clock_received_at > MAX_WALL_AGE_S:
            return 'clock_stale'
        if now - self.clock_advanced_at > MAX_WALL_AGE_S:
            return 'clock_frozen'
        if now - self.pose_received_at > MAX_WALL_AGE_S:
            return 'odom_stale'
        if self.stream_issue not in (None, 'clock_reset'):
            return self.stream_issue
        if abs(self.clock_t - self.last_pose['t']) > MAX_CLOCK_ODOM_LAG_S:
            return 'simulation_time_mismatch'
        return None

    def result(self, mode, *, now):
        from llm.recording import check_standing_start
        diagnostics = check_standing_start(self.samples)
        reason = self._stream_reason(now)
        if reason is None:
            if mode == 'prepare':
                reason = diagnostics['reason']
                if reason in ('pose_missing', 'insufficient_stable_window') and self.stream_issue == 'clock_reset':
                    reason = 'clock_reset'
            elif (len(self.samples) < 2 or diagnostics['duration_s'] < 1
                  or self.clock_t - self.clock_first_t < 1):
                reason = 'insufficient_simulation_progress'
        pose_t = self.last_pose.get('t') if self.last_pose else None
        lag = self.clock_t - pose_t if self.clock_t is not None and isinstance(pose_t, (int, float)) else None
        diagnostics = {
            **diagnostics, 'ready': reason is None, 'reason': reason,
            'clock_t': self.clock_t,
            'clock_age_s': None if self.clock_received_at is None else now - self.clock_received_at,
            'clock_advance_age_s': None if self.clock_advanced_at is None else now - self.clock_advanced_at,
            'odom_age_s': None if self.pose_received_at is None else now - self.pose_received_at,
            'clock_odom_lag_s': lag,
            'freshness_tolerances': {'max_wall_age_s': MAX_WALL_AGE_S,
                                     'max_clock_odom_lag_s': MAX_CLOCK_ODOM_LAG_S},
            'frames': {'frame_id': self.frame_id, 'child_frame_id': self.child_frame_id},
        }
        status = 'ready' if reason is None else 'invalid_start_pose' if mode == 'prepare' else 'clock_or_pose_missing'
        return _finite_json({'status': status, 'pose': self.last_pose,
                             'failure_reason': reason, 'diagnostics': diagnostics})


def observe(mode: str, timeout: float):
    import rclpy
    from nav_msgs.msg import Odometry
    from rclpy.qos import qos_profile_sensor_data
    from rosgraph_msgs.msg import Clock

    rclpy.init()
    node = rclpy.create_node('language_run_observer')
    state = ObservationState()

    def clock(message):
        nonlocal state
        state = state.with_clock(message.clock.sec + message.clock.nanosec / 1e9,
                                 received_at=time.monotonic())

    def odom(message):
        nonlocal state
        p, q = message.pose.pose.position, message.pose.pose.orientation
        stamp = message.header.stamp
        pose = quaternion_pose(stamp.sec + stamp.nanosec / 1e9,
                               p.x, p.y, p.z, q.x, q.y, q.z, q.w)
        state = state.with_pose(pose, received_at=time.monotonic(),
                                frame_id=getattr(message.header, 'frame_id', None),
                                child_frame_id=getattr(message, 'child_frame_id', None))

    node.create_subscription(Clock, '/clock', clock, qos_profile_sensor_data)
    node.create_subscription(Odometry, '/odom_ground_truth', odom, qos_profile_sensor_data)
    deadline = time.monotonic() + timeout
    try:
        while rclpy.ok() and time.monotonic() < deadline:
            rclpy.spin_once(node, timeout_sec=.1)
            result = state.result(mode, now=time.monotonic())
            if result['status'] == 'ready':
                return result
        return state.result(mode, now=time.monotonic())
    finally:
        node.destroy_node()
        rclpy.shutdown()


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('mode', choices=('preflight', 'prepare', 'tail', 'bag-ready', 'metadata'))
    p.add_argument('--output', type=Path)
    p.add_argument('--bag', type=Path)
    p.add_argument('--timeout', type=float, default=30)
    args = p.parse_args(argv)
    if args.output is None and args.mode != 'preflight':
        p.error('--output is required for observation and metadata')
    if args.mode == 'preflight':
        result = preflight()
    elif args.mode == 'metadata':
        result = {}
        for key, command in (
            ('base_commit', ['git', '-C', '/home/ubuntu/ws/src/unitree-go2-ros2', 'rev-parse', 'HEAD']),
            ('rosbridge_version', ['dpkg-query', '-W', '-f=${Version}', 'ros-humble-rosbridge-suite']),
        ):
            try:
                result[key] = subprocess.check_output(command, text=True, stderr=subprocess.DEVNULL, timeout=5).strip()
            except (subprocess.SubprocessError, OSError):
                result[key] = None
        result['status'] = 'ready'
    elif args.mode == 'bag-ready':
        from llm.recording import recording_ready
        deadline = time.monotonic() + args.timeout
        result = {'status': 'recording_not_started', 'topic_counts': {}}
        while time.monotonic() < deadline:
            counts = bag_topic_counts(args.bag.resolve())
            result = {'status': 'ready' if recording_ready(counts) else 'recording_not_started',
                      'topic_counts': counts}
            if result['status'] == 'ready':
                break
            time.sleep(.2)
    else:
        result = observe(args.mode, args.timeout)
    serialized = json.dumps(result, indent=2) + '\n'
    if args.output is not None:
        args.output.write_text(serialized)
    else:
        print(serialized, end='')
    return 0 if result['status'] == 'ready' else 1


if __name__ == '__main__':
    raise SystemExit(main())
