"""Validate every MCP input before any transmission; fail closed on bad results."""
from __future__ import annotations

import math

ALLOWED_TOOLS = ('publish_for_durations', 'get_topics', 'get_topic_type', 'get_message_details')
TOPICS = {'/cmd_vel': 'geometry_msgs/msg/Twist', '/odom_ground_truth': 'nav_msgs/msg/Odometry', '/clock': 'rosgraph_msgs/msg/Clock'}


class SafetyError(ValueError):
    """A tool request or response cannot safely continue the instruction."""

    def __init__(self, message, reason='argument_error'):
        super().__init__(message)
        self.reason = reason


def zero_twist() -> dict:
    return {'linear': dict.fromkeys(('x', 'y', 'z'), 0.0), 'angular': dict.fromkeys(('x', 'y', 'z'), 0.0)}


def number(value) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or abs(value) > 1e100 or not math.isfinite(value):
        raise SafetyError('有限の数値だけを指定してください。')
    return float(value)


def velocity(msg: dict) -> tuple[float, float, float]:
    if not isinstance(msg, dict) or set(msg) != {'linear', 'angular'}:
        raise SafetyError('Twist の項目が不正です。')
    for axis in ('linear', 'angular'):
        if not isinstance(msg[axis], dict) or set(msg[axis]) != {'x', 'y', 'z'}:
            raise SafetyError('Twist の軸が不正です。')
        for v in msg[axis].values():
            number(v)
    vx, vy, wz = msg['linear']['x'], msg['linear']['y'], msg['angular']['z']
    if any(msg[a][b] != 0 for a, b in [('linear', 'z'), ('angular', 'x'), ('angular', 'y')]):
        raise SafetyError('許可されない軸です。')
    if abs(vx) > .5 or abs(vy) > .3 or abs(wz) > .5 or sum(v != 0 for v in (vx, vy, wz)) > 1:
        raise SafetyError('速度の上限または単一軸の条件を超えています。')
    return vx, vy, wz


def validate_call(name: str, args: dict, used_s: float = 0) -> float:
    if name not in ALLOWED_TOOLS or not isinstance(args, dict):
        raise SafetyError('許可されていないツールです。')
    if name == 'get_topics' and not args:
        return used_s
    if name == 'get_topic_type' and set(args) == {'topic'} and isinstance(args['topic'], str) and args['topic'] in TOPICS:
        return used_s
    if name == 'get_message_details' and set(args) == {'message_type'} and isinstance(args['message_type'], str) and args['message_type'] in TOPICS.values():
        return used_s
    fields = {'topic', 'msg_type', 'messages', 'durations', 'rate_hz'}
    if name != 'publish_for_durations' or set(args) != fields:
        raise SafetyError('ツールの引数が不正です。')
    if args['topic'] != '/cmd_vel' or args['msg_type'] != TOPICS['/cmd_vel'] or number(args['rate_hz']) != 10:
        raise SafetyError('送信先・型・10 Hz を確認してください。')
    messages, durations = args['messages'], args['durations']
    if not isinstance(messages, list) or not isinstance(durations, list) or not messages or len(messages) != len(durations):
        raise SafetyError('動作列と時間列は同じ長さの空でない配列が必要です。')
    vectors = [velocity(m) for m in messages]
    seconds = [number(d) for d in durations]
    if any(not 0 < d <= 10 for d in seconds) or used_s + sum(seconds) > 40:
        raise SafetyError('区間10秒・合計40秒の上限を超えています。')
    if any(vectors[-1]) or seconds[-1] < 1:
        raise SafetyError('末尾に1秒以上の停止が必要です。')
    for i, v in enumerate(vectors[:-1]):
        if any(v) and any(vectors[i + 1]):
            raise SafetyError('移動区間の間に停止が必要です。')
        if not any(v) and seconds[i] < .5:
            raise SafetyError('区間の停止は0.5秒以上必要です。')
    return used_s + sum(seconds)


def check_result(result: dict, args: dict | None = None) -> None:
    if not isinstance(result, dict) or result.get('error') or result.get('errors') or result.get('success') is False:
        raise SafetyError('通信切断またはMCPの失敗です。追加移動を中止します。', 'communication_failure')
    if args is not None:
        count = result.get('published_count')
        # The upstream loop is wall-clock scheduled. Allow at most one rounding
        # publish per segment; counts alone cannot establish the actual cadence.
        expected = sum(math.ceil(d * 10) for d in args['durations'])
        if result.get('success') is not True or type(count) is not int or not expected - len(args['durations']) <= count <= expected + len(args['durations']):
            raise SafetyError('送信数が期待値と一致しません。通信記録を確認してください。', 'command_interruption')
