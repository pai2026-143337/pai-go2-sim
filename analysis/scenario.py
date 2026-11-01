"""Scripted command sequence used to compare the gait controllers.

Shared by the recorder (ROS 2 node in the container) and the analysis (plain Python on the Mac),
so it must not import ROS. Times are simulation seconds from the first received clock message.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Iterator, Optional

SCENARIO_VERSION = 1

# Spawn position passed to the launch file (world_init_x); the robot faces +x.
START_X = -5.0


@dataclass(frozen=True)
class Segment:
    name: str
    duration: float
    vx: float = 0.0
    vy: float = 0.0
    wz: float = 0.0


SCENARIO: tuple[Segment, ...] = (
    Segment("stand", 3.0),
    Segment("fwd_slow", 6.0, vx=0.5),
    Segment("stop1", 2.0),
    Segment("left", 3.0, vy=0.3),
    Segment("right", 3.0, vy=-0.3),
    Segment("back", 4.0, vx=-0.3),
    Segment("turn_left", 4.0, wz=0.5),
    Segment("turn_right", 4.0, wz=-0.5),
    Segment("fwd_fast", 5.0, vx=1.0),
    Segment("stop2", 3.0),
)


def total_duration() -> float:
    return sum(s.duration for s in SCENARIO)


def segment_bounds() -> Iterator[tuple[Segment, float, float]]:
    """Yield (segment, start, end) in simulation seconds."""
    t = 0.0
    for seg in SCENARIO:
        yield seg, t, t + seg.duration
        t += seg.duration


def segment_at(t: float) -> Optional[Segment]:
    for seg, start, end in segment_bounds():
        if start <= t < end:
            return seg
    return None


def planned_path(start_x: float, dt: float = 0.05) -> list[tuple[float, float]]:
    """Integrate the commanded body velocities as if tracked perfectly (world frame)."""
    x, y, yaw = start_x, 0.0, 0.0
    path = [(x, y)]
    for seg in SCENARIO:
        steps = int(round(seg.duration / dt))
        for _ in range(steps):
            x += (seg.vx * math.cos(yaw) - seg.vy * math.sin(yaw)) * dt
            y += (seg.vx * math.sin(yaw) + seg.vy * math.cos(yaw)) * dt
            yaw += seg.wz * dt
            path.append((x, y))
    return path
