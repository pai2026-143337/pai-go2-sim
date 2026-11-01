"""Pure preparation checks, shared by the recorder and local tests."""
from __future__ import annotations

from bisect import bisect_left
import json
import math
from pathlib import Path


START_TOLERANCES = {
    'position_m': .30, 'yaw_deg': 15, 'min_height_m': .15, 'max_tilt_deg': 15,
    'stable_duration_s': 3, 'max_drift_m': .03, 'max_yaw_drift_deg': 3,
    'max_sample_gap_s': .5,
}
POSE_FIELDS = ('t', 'x', 'y', 'yaw', 'z', 'tilt')


def load_instructions() -> dict:
    return json.loads(Path(__file__).with_name('instructions.json').read_text())


def planned_pairs(*, ids=None, trials=None) -> list[tuple[str, int]]:
    """Validate a session's explicit selection before reserving output paths."""
    available = tuple(item['id'] for item in load_instructions()['instructions'])
    selected = available if ids is None else tuple(ids.split(',') if isinstance(ids, str) else ids)
    if (not selected or any(not isinstance(item, str) or item not in available for item in selected)
            or len(set(selected)) != len(selected)):
        raise ValueError('--ids は重複のない指示IDを 02,06,10 の形式で指定してください。')
    count = 3 if trials is None else trials
    if isinstance(count, bool) or not isinstance(count, int) or count < 1:
        raise ValueError('--trials は正の整数で指定してください。')
    return [(instruction, run) for instruction in selected for run in range(1, count + 1)]


def session_pairs(metadata: dict) -> list[tuple[str, int]]:
    return planned_pairs(ids=metadata.get('instruction_ids'),
                         trials=metadata.get('trials_per_instruction'))


def valid_pose(sample: dict) -> bool:
    try:
        return (all(isinstance(sample[k], (int, float)) and not isinstance(sample[k], bool)
                    and math.isfinite(sample[k]) for k in POSE_FIELDS)
                and sample['t'] >= 0 and sample['tilt'] >= 0)
    except (KeyError, TypeError):
        return False


def _cross(a, b, c):
    return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])


def _planar_drift(samples):
    """Diameter of the convex hull, including excursions between endpoints."""
    points = sorted({(s['x'], s['y']) for s in samples})
    if len(points) < 2:
        return 0.0

    def half_hull(ordered):
        hull = ()
        for point in ordered:
            while len(hull) >= 2 and _cross(hull[-2], hull[-1], point) <= 0:
                hull = hull[:-1]
            hull = (*hull, point)
        return hull

    hull = half_hull(points)[:-1] + half_hull(reversed(points))[:-1]
    j, maximum = 1, 0.0
    for i, point in enumerate(hull):
        next_point = hull[(i + 1) % len(hull)]
        while (_cross(point, next_point, hull[(j + 1) % len(hull)])
               > _cross(point, next_point, hull[j])):
            j = (j + 1) % len(hull)
        maximum = max(maximum, math.dist(point, hull[j]), math.dist(next_point, hull[j]))
    return maximum


def _yaw_drift(samples):
    angles = sorted(s['yaw'] % (2 * math.pi) for s in samples)
    doubled = angles + [angle + 2 * math.pi for angle in angles]
    maximum = 0.0
    for angle in angles:
        nearest = bisect_left(doubled, angle + math.pi)
        for candidate in doubled[max(0, nearest - 1):nearest + 1]:
            difference = abs(candidate - angle) % (2 * math.pi)
            maximum = max(maximum, min(difference, 2 * math.pi - difference))
    return math.degrees(maximum)


def check_standing_start(samples: list[dict]) -> dict:
    """Check a sane upright start; arrival accuracy uses the measured motion start."""
    base = {'ready': False, 'reason': None, 'sample_count': len(samples),
            'tolerances': dict(START_TOLERANCES), 'duration_s': 0.0, 'window': None,
            'position_error_m': None, 'yaw_error_deg': None, 'drift_m': None,
            'yaw_drift_deg': None, 'z_min_m': None, 'tilt_max_deg': None, 'ranges': {}}
    if not samples:
        return {**base, 'reason': 'pose_missing'}
    if not all(valid_pose(sample) for sample in samples):
        return {**base, 'reason': 'invalid_sample'}
    duration = samples[-1]['t'] - samples[0]['t']
    measurements = {
        'duration_s': duration,
        'window': {'start_t': samples[0]['t'], 'end_t': samples[-1]['t']},
        'position_error_m': max(math.hypot(s['x'] + 5, s['y']) for s in samples),
        'yaw_error_deg': max(math.degrees(abs(math.atan2(math.sin(s['yaw']), math.cos(s['yaw'])))) for s in samples),
        'drift_m': _planar_drift(samples), 'yaw_drift_deg': _yaw_drift(samples),
        'z_min_m': min(s['z'] for s in samples), 'tilt_max_deg': max(s['tilt'] for s in samples),
        'ranges': {field: [min(s[field] for s in samples), max(s[field] for s in samples)]
                   for field in ('x', 'y', 'z', 'yaw', 'tilt')},
    }
    result = {**base, **measurements}
    limits = START_TOLERANCES
    gaps = [b['t'] - a['t'] for a, b in zip(samples, samples[1:])]
    checks = (
        (any(not 0 < gap <= limits['max_sample_gap_s'] for gap in gaps), 'invalid_sample'),
        (result['position_error_m'] > limits['position_m'] + 1e-12, 'position_out_of_tolerance'),
        (result['yaw_error_deg'] > limits['yaw_deg'] + 1e-12, 'yaw_out_of_tolerance'),
        (result['z_min_m'] < limits['min_height_m'] or result['tilt_max_deg'] > limits['max_tilt_deg'], 'not_standing'),
        (result['drift_m'] > limits['max_drift_m'] + 1e-12
         or result['yaw_drift_deg'] > limits['max_yaw_drift_deg'] + 1e-12, 'unstable_pose'),
        (len(samples) < 2 or duration < limits['stable_duration_s'], 'insufficient_stable_window'),
    )
    reason = next((reason for rejected, reason in checks if rejected), None)
    return {**result, 'ready': reason is None, 'reason': reason}


def standing_start(samples: list[dict]) -> dict | None:
    return dict(samples[-1]) if check_standing_start(samples)['ready'] else None


def recording_ready(counts: dict[str, int]) -> bool:
    required = ('/clock', '/odom_ground_truth', '/joint_states', '/tf', '/tf_static', '/robot_description')
    return '/cmd_vel' in counts and all(counts.get(topic, 0) > 0 for topic in required)


def preparation_status(active: bool, pose_ok: bool, bag_ready: bool) -> str:
    if not active:
        return 'controller_inactive'
    if not pose_ok:
        return 'invalid_start_pose'
    if not bag_ready:
        return 'recording_not_started'
    return 'ready'
