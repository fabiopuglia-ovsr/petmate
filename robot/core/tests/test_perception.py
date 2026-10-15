import math

import numpy as np
import pytest

from pmcore.detector import Detector, PetEventLimiter, build_detections_msg, parse_hailo_nms
from pmcore.geometry import bbox_bearing, camera_bearing
from pmcore.tracker import Detection, IdSource, IouTracker, iou
from pmcore.util import wrap_pi

HFOV = math.radians(102.0)


# ------------------------------------------------------------------ bearing
def test_center_is_zero_front_and_pi_rear():
    assert bbox_bearing([0.4, 0.2, 0.6, 0.8], "front", HFOV) == pytest.approx(0.0)
    assert abs(bbox_bearing([0.4, 0.2, 0.6, 0.8], "rear", HFOV)) == pytest.approx(math.pi)


def test_edges_equal_half_hfov_and_left_is_positive():
    assert camera_bearing(0.0, HFOV) == pytest.approx(HFOV / 2)   # left image edge → +51°
    assert camera_bearing(1.0, HFOV) == pytest.approx(-HFOV / 2)  # right image edge → −51°
    left = bbox_bearing([0.0, 0, 0.2, 1], "front", HFOV)
    assert left > 0


def test_pinhole_model_quarter_point():
    # x=0.25 → atan(0.5*tan(51°))
    assert camera_bearing(0.25, HFOV) == pytest.approx(math.atan(0.5 * math.tan(HFOV / 2)))


def test_rear_camera_offset_by_pi_and_wrapped():
    for x in (0.05, 0.3, 0.5, 0.7, 0.95):
        bb = [x - 0.05, 0.3, x + 0.05, 0.6]
        front = bbox_bearing(bb, "front", HFOV)
        rear = bbox_bearing(bb, "rear", HFOV)
        assert -math.pi <= rear <= math.pi
        assert wrap_pi(rear - front - math.pi) == pytest.approx(0.0, abs=1e-9)
    # object on the LEFT of the rear image is on the robot's RIGHT-rear: bearing in (π/2, π]?
    # rear yaw=π: image-left (+angle in camera) → π + small → wraps to negative (right-rear)
    assert bbox_bearing([0.0, 0, 0.2, 1], "rear", HFOV) < 0
    assert bbox_bearing([0.8, 0, 1.0, 1], "rear", HFOV) > 0


def test_wrap_pi():
    assert wrap_pi(3 * math.pi / 2) == pytest.approx(-math.pi / 2)
    assert wrap_pi(-3 * math.pi / 2) == pytest.approx(math.pi / 2)
    assert wrap_pi(0.3) == pytest.approx(0.3)


def test_unknown_camera():
    with pytest.raises(ValueError):
        bbox_bearing([0, 0, 1, 1], "top", HFOV)


# ------------------------------------------------------------------ tracker
def test_iou():
    assert iou([0, 0, 1, 1], [0, 0, 1, 1]) == pytest.approx(1.0)
    assert iou([0, 0, 0.5, 1], [0.5, 0, 1, 1]) == 0.0
    assert iou([0, 0, 0.5, 0.5], [0.25, 0.25, 0.75, 0.75]) == pytest.approx(0.0625 / (0.25 + 0.25 - 0.0625))


def test_tracker_keeps_id_and_separates_classes_and_expires():
    tr = IouTracker(iou_threshold=0.3, max_age_s=1.0)
    a = tr.update([Detection("cat", 0.9, [0.1, 0.1, 0.3, 0.3]), Detection("dog", 0.8, [0.6, 0.6, 0.9, 0.9])], 0.0)
    cat_id, dog_id = a[0].track_id, a[1].track_id
    assert cat_id != dog_id
    b = tr.update([Detection("dog", 0.8, [0.62, 0.6, 0.92, 0.9]), Detection("cat", 0.9, [0.12, 0.1, 0.32, 0.3])], 0.1)
    assert (b[0].track_id, b[1].track_id) == (dog_id, cat_id)
    # same box, different class → new track
    c = tr.update([Detection("person", 0.9, [0.12, 0.1, 0.32, 0.3])], 0.2)
    assert c[0].track_id not in (cat_id, dog_id)
    assert tr.get(cat_id).hits == 2
    # expiry
    d = tr.update([Detection("cat", 0.9, [0.12, 0.1, 0.32, 0.3])], 5.0)
    assert d[0].track_id != cat_id


def test_tracker_greedy_best_iou_wins():
    tr = IouTracker(0.1, 5)
    t = tr.update([Detection("cat", 0.9, [0.0, 0.0, 0.4, 0.4]), Detection("cat", 0.9, [0.5, 0.5, 0.9, 0.9])], 0)
    ids = [d.track_id for d in t]
    u = tr.update([Detection("cat", 0.9, [0.48, 0.5, 0.88, 0.9]), Detection("cat", 0.9, [0.02, 0.0, 0.42, 0.4])], 0.1)
    assert [d.track_id for d in u] == [ids[1], ids[0]]


def test_track_ids_unique_across_cameras():
    ids = IdSource()
    f, r = IouTracker(ids=ids), IouTracker(ids=ids)
    a = f.update([Detection("cat", 0.9, [0, 0, 0.2, 0.2])], 0)[0].track_id
    b = r.update([Detection("cat", 0.9, [0, 0, 0.2, 0.2])], 0)[0].track_id
    assert a != b


# ------------------------------------------------------------------ hailo + detector
def test_parse_hailo_nms_per_class_list():
    out = [np.zeros((0, 5), np.float32) for _ in range(80)]
    out[15] = np.array([[0.2, 0.1, 0.6, 0.5, 0.91], [0.1, 0.1, 0.2, 0.2, 0.2]], np.float32)  # cat (y1,x1,y2,x2,s)
    out[2] = np.array([[0.2, 0.1, 0.6, 0.5, 0.99]], np.float32)  # car: not wanted
    dets = parse_hailo_nms(out, 0.4)
    assert len(dets) == 1
    assert dets[0].cls == "cat" and dets[0].bbox == [0.1, 0.2, 0.5, 0.6]


def test_parse_hailo_nms_flat_buffer():
    flat = []
    for c in range(80):
        if c == 16:
            flat += [1, 0.0, 0.5, 0.4, 0.9, 0.8]
        else:
            flat += [0]
    dets = parse_hailo_nms(np.array(flat, np.float32), 0.5)
    assert [(d.cls, d.bbox) for d in dets] == [("dog", [0.5, 0.0, 0.9, 0.4])]


def test_detections_message_schema():
    d = Detection("cat", 0.8, [0.4, 0.2, 0.6, 0.8], track_id=7)
    m = build_detections_msg("rear", 123.5, [d], HFOV)
    assert set(m) == {"stamp", "camera", "objects"}
    o = m["objects"][0]
    assert set(o) == {"cls", "conf", "bbox", "bearing_rad", "track_id"}
    assert abs(o["bearing_rad"]) == pytest.approx(math.pi, abs=1e-3)


def test_pet_event_rate_limit_and_detector_process():
    pub, events, sight = [], [], []
    cfg = {"iou_match": 0.3, "track_max_age_s": 2.0, "pet_event_min_interval_s": 60.0, "pet_event_min_conf": 0.5}
    det = Detector(cfg, 102.0, loop=None, publish=pub.append, on_pet_event=events.append, on_sighting=sight.append)
    for i in range(5):
        det.process("front", [Detection("cat", 0.9, [0.1, 0.1, 0.3, 0.3])], 1000.0 + i, float(i) * 0.1)
    assert len(pub) == 5
    assert len({m["objects"][0]["track_id"] for m in pub}) == 1
    assert events == [{"t": "pet_detected", "ts": 1002.0, "camera": "front", "cls": "cat"}]  # after 3 hits
    det.process("front", [Detection("cat", 0.9, [0.1, 0.1, 0.3, 0.3])], 1100.0, 30.0)
    assert len(events) == 1  # within 60 s
    det.process("front", [Detection("cat", 0.9, [0.1, 0.1, 0.3, 0.3])], 1200.0, 63.0)
    assert len(events) == 1  # track expired → new track has 1 hit
    assert sight[-1].bearing_rad > 0
    lim = PetEventLimiter(10, 0.5, 1)
    assert lim.check("person", 0.9, 5, 0) is False
    assert lim.check("dog", 0.4, 5, 0) is False
