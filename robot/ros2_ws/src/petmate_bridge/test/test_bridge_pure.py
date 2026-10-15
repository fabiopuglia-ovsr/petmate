import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from petmate_bridge import commands as C  # noqa: E402
from petmate_bridge.state_builder import build_state  # noqa: E402

SPEC_KEYS = {'t', 'ts', 'battery', 'pose', 'mode', 'activity', 'feeder', 'launcher',
             'safety', 'nav', 'pet', 'pois', 'fw'}


def full_inputs():
    hw = {'soc': 82, 'voltage': 15.6, 'current': -1.2, 'charging': False, 'docked': True,
          'estop': False, 'cliff_mask': 0, 'bump_mask': 0, 'wheel_drop': False,
          'motor_fault': False, 'fw': '0.1.0', 'connected': True}
    pose = {'x': 1.2, 'y': -0.4, 'th': 0.3, 'localized': True}
    beh = {'mode': 'auto', 'activity': 'idle', 'today_g': 50.0,
           'last_meal': {'ts': 1760000000.0, 'grams': 25.0},
           'nav': {'status': 'idle', 'goal': None, 'mapping': False},
           'pet': {'last_seen_ts': 1760000000.0, 'camera': 'front', 'bearing_rad': 0.1},
           'pois': ['salotto', 'dock', 'cucina']}
    return hw, pose, beh


def test_state_matches_spec_example():
    hw, pose, beh = full_inputs()
    st = build_state(1760000000.0, hw=hw, pose=pose, behavior=beh,
                     feed={'state': 'idle', 'hopper_g': 210.0},
                     launch={'state': 'idle', 'balls': 3})
    expected = {
        't': 'state', 'ts': 1760000000.0,
        'battery': {'soc': 82, 'voltage': 15.6, 'current': -1.2, 'charging': False, 'docked': True},
        'pose': {'x': 1.2, 'y': -0.4, 'th': 0.3, 'localized': True},
        'mode': 'auto', 'activity': 'idle',
        'feeder': {'state': 'idle', 'hopper_g': 210.0, 'today_g': 50.0,
                   'last_meal': {'ts': 1760000000.0, 'grams': 25.0}},
        'launcher': {'state': 'idle', 'balls': 3},
        'safety': {'estop': False, 'cliff_mask': 0, 'bump_mask': 0, 'wheel_drop': False,
                   'motor_fault': False},
        'nav': {'status': 'idle', 'goal': None, 'mapping': False},
        'pet': {'last_seen_ts': 1760000000.0, 'camera': 'front', 'bearing_rad': 0.1},
        'pois': ['cucina', 'salotto'],
        'fw': '0.1.0'}
    assert st == expected
    json.dumps(st)  # serialisable


def test_state_defaults_without_inputs():
    st = build_state(1.0)
    assert set(st) == SPEC_KEYS
    assert st['pose']['localized'] is False
    assert st['feeder']['hopper_g'] is None and st['feeder']['last_meal'] is None
    assert st['pet'] == {'last_seen_ts': None, 'camera': None, 'bearing_rad': None}
    assert st['activity'] == 'idle' and st['nav']['status'] == 'idle'
    json.dumps(st, allow_nan=False)


def test_state_sanitises_values():
    hw, pose, beh = full_inputs()
    hw['connected'] = False
    pose['x'] = float('nan')
    beh['activity'] = 'dancing'
    beh['nav']['status'] = 'weird'
    st = build_state(2.0, hw=hw, pose=pose, behavior=beh, feed={'state': 'bogus'})
    assert st['activity'] == 'error'
    assert st['nav']['status'] == 'idle'
    assert st['pose']['x'] == 0.0
    assert st['feeder']['state'] == 'idle'
    json.dumps(st, allow_nan=False)


def test_teleop_activity_override():
    st = build_state(1.0, behavior={'mode': 'manual', 'activity': 'idle'}, teleop_active=True)
    assert st['activity'] == 'teleop'
    st = build_state(1.0, behavior={'mode': 'auto', 'activity': 'feeding'}, teleop_active=True)
    assert st['activity'] == 'feeding'


def test_validate_all_spec_examples():
    examples = [
        {"t": "teleop", "v": 0.2, "w": 0.5},
        {"t": "feed", "grams": 20},
        {"t": "feed_abort"},
        {"t": "launch", "power": 40, "count": 1},
        {"t": "intake", "mode": "in"},
        {"t": "tease", "pattern": "wiggle", "duration_s": 20},
        {"t": "led", "mode": "solid", "color": "#F0A830"},
        {"t": "goto", "target": "dock"},
        {"t": "goto", "target": "poi", "poi": "cucina"},
        {"t": "goto", "target": "pet"},
        {"t": "mode", "mode": "sleep"},
        {"t": "estop", "engage": True},
        {"t": "play", "game": "laser_dots", "duration_s": 300},
        {"t": "save_poi", "name": "cucina"},
        {"t": "map", "action": "start_mapping"},
    ]
    for e in examples:
        assert C.validate(e) == (True, None), e


def test_validate_rejections():
    bad = [
        {"t": "feed", "grams": 0}, {"t": "feed", "grams": 61}, {"t": "feed", "grams": "20"},
        {"t": "teleop", "v": 0.1}, {"t": "teleop", "v": float('nan'), "w": 0},
        {"t": "intake", "mode": "sideways"}, {"t": "led", "mode": "solid", "color": "red"},
        {"t": "goto", "target": "poi"}, {"t": "mode", "mode": "party"},
        {"t": "estop", "engage": 1}, {"t": "play", "game": "fetch"},
        {"t": "save_poi", "name": ""}, {"t": "map", "action": "load"},
        {"t": "camera", "id": "front"}, "nope", {"no_t": 1},
        {"t": "launch", "count": 5},
    ]
    for b in bad:
        ok, err = C.validate(b)
        assert not ok and err, b


def test_ensure_id_and_clamp():
    c = {"t": "feed_abort"}
    cid = C.ensure_id(c)
    assert c['id'] == cid and len(cid) == 36
    c2 = {"t": "feed_abort", "id": "abc"}
    assert C.ensure_id(c2) == 'abc'
    assert C.clamp_teleop(2.0, -9.0) == (0.5, -2.0)


def test_nav_goal_shape_9_11():
    from petmate_bridge.state_builder import build_state
    g = lambda goal: build_state(0.0, behavior={'nav': {'status': 'active', 'goal': goal}})['nav']['goal']
    assert g(None) is None
    assert g('dock') == {'target': 'dock', 'poi': None}
    assert g('pet') == {'target': 'pet', 'poi': None}
    assert g('cucina') == {'target': 'poi', 'poi': 'cucina'}
