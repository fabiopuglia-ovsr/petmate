"""Camera geometry → bearing in base_link (INTERFACES.md §3, §1).

``bearing_rad`` is in the ``base_link`` frame, positive to the LEFT (ROS REP-103,
counter-clockwise about +z). Image coordinates are normalised 0..1 with x
growing to the right of the (upright) image.

Front camera: optical axis along +x of base_link (yaw 0).
Rear camera: yaw π (looking backwards) → bearing = wrap(π + bearing_cam).

Pinhole model: a point at normalised x has horizontal angle
``atan((0.5 - x) * 2 * tan(hfov/2))`` from the optical axis (left positive).
The Camera Module 3 Wide (102° HFOV) has some barrel distortion; for the
1280x720 / 640x640 full-FOV binned modes the pinhole error at the image edge
is a few degrees, acceptable for "turn toward the pet".
"""

from __future__ import annotations

import math
from typing import Sequence

from .util import wrap_pi

CAMERA_YAW = {"front": 0.0, "rear": math.pi}


def camera_bearing(x_norm: float, hfov_rad: float) -> float:
    """Horizontal angle of image column ``x_norm`` from the optical axis (left +)."""
    x = min(1.0, max(0.0, float(x_norm)))
    return math.atan((0.5 - x) * 2.0 * math.tan(hfov_rad / 2.0))


def bbox_bearing(bbox: Sequence[float], camera: str, hfov_rad: float, yaw_offset: float = 0.0) -> float:
    """Bearing in base_link of the centre of a normalised bbox [x1,y1,x2,y2]."""
    if camera not in CAMERA_YAW:
        raise ValueError(f"unknown camera {camera!r}")
    cx = (float(bbox[0]) + float(bbox[2])) / 2.0
    return wrap_pi(CAMERA_YAW[camera] + yaw_offset + camera_bearing(cx, hfov_rad))
