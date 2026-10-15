"""Display state (pure logic, no pygame) shared between asyncio and the render thread."""

from __future__ import annotations

import math
import threading
import time
from enum import Enum
from typing import Any, Optional

MODES = ("face", "owner", "game")
GAMES = ("birds", "fish", "mouse")


class FaceExpression(str, Enum):
    IDLE = "idle"
    HAPPY = "happy"
    SLEEPY = "sleepy"
    ALERT = "alert"   # error / estop


def expression_for(activity: Optional[str], mode: Optional[str], pet_recent: bool, estop: bool) -> FaceExpression:
    if estop or activity == "error":
        return FaceExpression.ALERT
    if activity == "sleeping" or mode == "sleep":
        return FaceExpression.SLEEPY
    if pet_recent or activity == "playing":
        return FaceExpression.HAPPY
    return FaceExpression.IDLE


def gaze_from_bearing(bearing_rad: Optional[float], max_offset: float = 1.0) -> float:
    """Map a base_link bearing (left +) to a horizontal pupil offset in [-1, 1]
    (+ = screen right as seen by someone facing the robot).

    The display faces forward, so the robot's left (+bearing) is the
    onlooker's right: the pupils move to screen-right to look toward the pet.
    Bearings behind the robot (|b| > 90°) saturate at ±1.
    """
    if bearing_rad is None:
        return 0.0
    g = math.sin(max(-math.pi / 2, min(math.pi / 2, bearing_rad)))
    if abs(bearing_rad) > math.pi / 2:
        g = 1.0 if bearing_rad > 0 else -1.0
    return max(-max_offset, min(max_offset, g))


class DisplayState:
    """Thread-safe snapshot read by the renderer each frame."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.mode = "face"
        self.game = "birds"
        self.streaming = False
        self.stream_led = True
        self.activity: Optional[str] = None
        self.robot_mode: Optional[str] = None
        self.estop = False
        self.pet_bearing: Optional[float] = None
        self.pet_seen_t = 0.0
        self.owner_frame: Any = None  # numpy RGB (h, w, 3)
        self.owner_frame_t = 0.0
        self.game_until: Optional[float] = None
        self.prev_mode = "face"

    def set_mode(self, mode: str, game: Optional[str] = None, duration_s: Optional[float] = None) -> None:
        if mode not in MODES:
            raise ValueError(mode)
        with self._lock:
            if mode != self.mode:
                self.prev_mode = self.mode
            self.mode = mode
            if game is not None:
                if game not in GAMES:
                    raise ValueError(game)
                self.game = game
            self.game_until = time.monotonic() + duration_s if (mode == "game" and duration_s) else None

    def update_robot(self, state: dict) -> None:
        with self._lock:
            self.activity = state.get("activity")
            self.robot_mode = state.get("mode")
            self.estop = bool((state.get("safety") or {}).get("estop"))

    def pet_sighting(self, bearing: Optional[float]) -> None:
        with self._lock:
            if bearing is None:
                self.pet_bearing = None
            else:
                self.pet_bearing = bearing
                self.pet_seen_t = time.monotonic()

    def set_owner_frame(self, rgb: Any) -> None:
        with self._lock:
            self.owner_frame = rgb
            self.owner_frame_t = time.monotonic()

    def snapshot(self) -> dict:
        now = time.monotonic()
        with self._lock:
            if self.mode == "game" and self.game_until and now > self.game_until:
                self.mode = "face"
                self.game_until = None
            pet_recent = now - self.pet_seen_t < 5.0
            return {
                "mode": self.mode,
                "game": self.game,
                "streaming": self.streaming,
                "stream_led": self.stream_led,
                "expression": expression_for(self.activity, self.robot_mode, pet_recent, self.estop),
                "gaze": gaze_from_bearing(self.pet_bearing if pet_recent else None),
                "owner_frame": self.owner_frame if now - self.owner_frame_t < 2.0 else None,
            }
