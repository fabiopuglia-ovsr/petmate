import datetime as dt
import math
import os
import sys
from zoneinfo import ZoneInfo

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from petmate_behavior import events as E  # noqa: E402
from petmate_behavior.geometry import (SeekParams, dock_reverse_cmd, predock_pose,  # noqa: E402
                                       range_at_bearing, seek_cmd)
from petmate_behavior.pois import PoiStore  # noqa: E402
from petmate_behavior.scheduler import Scheduler, SchedulerState, in_quiet_hours  # noqa: E402

ROME = ZoneInfo('Europe/Rome')


def cfg(**over):
    c = {
        'version': 1,
        'feeding': {'schedule': [{'time': '08:00', 'grams': 25, 'days': [1, 2, 3, 4, 5, 6, 7]},
                                 {'time': '19:00', 'grams': 60, 'days': [6, 7]}],
                    'portion_max_g': 50, 'daily_max_g': 70, 'feeding_poi': 'cucina'},
        'play': {'auto_play': True,
                 'sessions': [{'time': '14:00', 'duration_min': 10, 'days': [1, 2, 3, 4, 5]},
                              {'time': '23:00', 'duration_min': 5, 'days': [1, 2, 3, 4, 5, 6, 7]}],
                 'launch_power': 40, 'games': ['chase_ball', 'teaser', 'screen_birds'],
                 'quiet_hours': {'start': '22:00', 'end': '07:00'}},
        'patrol': {'enabled': True, 'interval_min': 120},
        'timezone': 'Europe/Rome',
    }
    c.update(over)
    return c


class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t

    def set_local(self, y, mo, d, h, mi, tz=ROME):
        self.t = dt.datetime(y, mo, d, h, mi, tzinfo=tz)


# 2026-10-14 is a Wednesday (ISO 3); 2026-10-17 Saturday (6)
def test_meal_fires_once_in_local_time():
    clk = Clock(dt.datetime(2026, 10, 14, 5, 59, tzinfo=dt.timezone.utc))  # 07:59 Rome (CEST)
    s = Scheduler(cfg(), now_fn=clk)
    assert s.poll() == []
    clk.t = dt.datetime(2026, 10, 14, 6, 0, 30, tzinfo=dt.timezone.utc)   # 08:00:30 Rome
    due = s.poll()
    assert len(due) == 1 and due[0].kind == 'meal' and due[0].runnable
    assert due[0].grams == 25 and due[0].key == 'meal|2026-10-14|08:00'
    # not handled yet -> returned again (lets caller defer while busy)
    assert len(s.poll()) == 1
    s.mark_handled(due[0].key)
    assert s.poll() == []


def test_meal_missed_after_grace():
    clk = Clock(None)
    clk.set_local(2026, 10, 14, 8, 20)
    s = Scheduler(cfg(), now_fn=clk, grace_s=900)
    due = s.poll()
    assert due[0].skip_reason == 'missed'


def test_days_filter_iso_weekday():
    clk = Clock(None)
    clk.set_local(2026, 10, 14, 19, 1)   # Wednesday: weekend-only 19:00 meal must not fire
    s = Scheduler(cfg(), now_fn=clk)
    assert [d for d in s.poll() if d.kind == 'meal' and d.slot_time == '19:00'] == []
    clk.set_local(2026, 10, 17, 19, 1)   # Saturday
    s = Scheduler(cfg(), now_fn=clk)
    meals = [d for d in s.poll() if d.kind == 'meal' and d.slot_time == '19:00']
    assert len(meals) == 1


def test_portion_and_daily_max_clamping():
    clk = Clock(None)
    clk.set_local(2026, 10, 17, 8, 0)
    s = Scheduler(cfg(), now_fn=clk)
    d = s.poll()[0]
    assert d.grams == 25
    s.record_meal(25.0)
    s.mark_handled(d.key)
    clk.set_local(2026, 10, 17, 19, 0)
    d2 = [x for x in s.poll() if x.kind == 'meal'][0]
    # requested 60, portion_max 50, remaining 70-25=45
    assert d2.grams == 45 and d2.runnable
    s.record_meal(45.0)
    s.mark_handled(d2.key)
    assert s.clamp_portion(10) == (0.0, 'daily_max_reached')
    assert s.remaining_g() == 0.0


def test_daily_total_resets_at_local_midnight():
    clk = Clock(None)
    clk.set_local(2026, 10, 14, 23, 50)
    s = Scheduler(cfg(), now_fn=clk)
    s.record_meal(60.0)
    assert s.today_g == 60.0
    clk.set_local(2026, 10, 15, 0, 5)
    assert s.today_g == 0.0
    assert s.state.last_meal['grams'] == 60.0


def test_play_skipped_in_quiet_hours_and_when_disabled():
    clk = Clock(None)
    clk.set_local(2026, 10, 14, 23, 0)
    s = Scheduler(cfg(), now_fn=clk)
    plays = {d.slot_time: d for d in s.poll() if d.kind == 'play'}
    assert plays['23:00'].skip_reason == 'quiet_hours'
    assert plays['14:00'].skip_reason == 'missed'
    clk.set_local(2026, 10, 14, 14, 1)
    c = cfg()
    c['play']['auto_play'] = False
    s = Scheduler(c, now_fn=clk)
    assert [d for d in s.poll() if d.kind == 'play'][0].skip_reason == 'auto_play_disabled'
    s = Scheduler(cfg(), now_fn=clk)
    p = [d for d in s.poll() if d.kind == 'play'][0]
    assert p.runnable and p.duration_s == 600


def test_quiet_hours_windows():
    q = {'start': '22:00', 'end': '07:00'}
    f = lambda h, m: in_quiet_hours(dt.datetime(2026, 1, 1, h, m, tzinfo=ROME), q)  # noqa: E731
    assert f(22, 0) and f(23, 59) and f(0, 0) and f(6, 59)
    assert not f(7, 0) and not f(12, 0) and not f(21, 59)
    q2 = {'start': '13:00', 'end': '15:00'}
    assert in_quiet_hours(dt.datetime(2026, 1, 1, 14, 0, tzinfo=ROME), q2)
    assert not in_quiet_hours(dt.datetime(2026, 1, 1, 15, 0, tzinfo=ROME), q2)
    assert not in_quiet_hours(dt.datetime(2026, 1, 1, 3, 0, tzinfo=ROME), {'start': '1:00', 'end': '1:00'})
    assert not in_quiet_hours(dt.datetime(2026, 1, 1, 3, 0, tzinfo=ROME), None)


def test_timezone_change_and_dst():
    # 2026-03-29: Europe/Rome switches CET->CEST at 02:00. 08:00 local = 06:00 UTC.
    clk = Clock(dt.datetime(2026, 3, 29, 6, 0, 10, tzinfo=dt.timezone.utc))
    s = Scheduler(cfg(), now_fn=clk)
    assert [d.slot_time for d in s.poll() if d.kind == 'meal'] == ['08:00']
    # Same instant, New York timezone: 02:00 local, nothing due
    s2 = Scheduler(cfg(timezone='America/New_York'), now_fn=clk)
    assert [d for d in s2.poll() if d.kind == 'meal'] == []
    # bad tz falls back to UTC rather than crashing
    s3 = Scheduler(cfg(timezone='Mars/Base'), now_fn=clk)
    assert s3.now_local().utcoffset() == dt.timedelta(0)


def test_float_clock_and_state_roundtrip():
    clk = Clock(dt.datetime(2026, 10, 14, 6, 0, tzinfo=dt.timezone.utc).timestamp())
    s = Scheduler(cfg(), now_fn=clk)
    d = s.poll()
    s.mark_handled(d[0].key)
    s.record_meal(20.0, ts=123.0)
    st = SchedulerState.from_dict(s.state.to_dict())
    s2 = Scheduler(cfg(), now_fn=clk, state=st)
    assert s2.today_g == 20.0 and s2.poll() == []


def test_game_rotation_and_patrol():
    clk = Clock(None)
    clk.set_local(2026, 10, 14, 12, 0)
    s = Scheduler(cfg(), now_fn=clk)
    assert [s.next_game() for _ in range(4)] == ['chase_ball', 'teaser', 'screen_birds', 'chase_ball']
    now = clk.t.timestamp()
    assert s.patrol_due(now - 7201) and not s.patrol_due(now - 100)
    clk.set_local(2026, 10, 14, 23, 0)
    assert not s.patrol_due(0)   # quiet hours


def test_empty_config_is_safe():
    clk = Clock(None)
    clk.set_local(2026, 10, 14, 8, 0)
    s = Scheduler({}, now_fn=clk)
    assert s.poll() == []
    assert s.next_game() is None
    assert s.clamp_portion(25) == (25, None)


# ------------------------------------------------------------------ events
def test_mcu_event_mapping():
    assert E.mcu_event_to_events(E.HOPPER_LOW, 40, 1.0) == [{'t': 'hopper_low', 'ts': 1.0, 'grams': 40}]
    assert E.mcu_event_to_events(E.CLIFF, 0, 1.0)[0]['t'] == 'cliff'
    assert E.mcu_event_to_events(E.FEEDER_JAM, 0, 1.0)[0]['t'] == 'feeder_jam'
    assert E.mcu_event_to_events(E.DOCK_CONTACT, 0, 1.0) == []   # from flag edges
    assert E.mcu_event_to_events(E.BATTERY_LOW, 15, 1.0) == []   # BatteryMonitor
    assert E.mcu_event_to_events(E.BUTTON_PROVISION, 0, 1.0) == [{'t': 'button_provision', 'ts': 1.0}]
    assert E.mcu_event_to_events(E.BUTTON_SHORT, 0, 1.0) == []


def test_flag_edges():
    f = E.FlagEdges()
    assert f.update(0, 0.0) == []
    assert [e['t'] for e in f.update(E.FLAG_DOCKED, 1.0)] == ['docked']
    assert f.update(E.FLAG_DOCKED, 2.0) == []
    assert [e['t'] for e in f.update(E.FLAG_ESTOP, 3.0)] == ['undocked', 'estop']


def test_battery_latch():
    b = E.BatteryMonitor(low_soc=20, hysteresis=10)
    assert b.update(50, False, False, 0.0) == []
    ev = b.update(19, False, False, 1.0)
    assert ev == [{'t': 'battery_low', 'ts': 1.0, 'soc': 19}]
    assert b.update(18, False, False, 2.0) == []
    assert b.update(25, True, True, 3.0) == []   # still latched (needs >= 30)
    b.update(31, True, True, 4.0)
    assert not b.latched
    assert b.update(10, True, True, 5.0) == []   # charging: no event
    assert b.update(40, False, False, 6.0, mcu_low=True)[0]['soc'] == 40


def test_pet_sightings_cooldown():
    p = E.PetSightings(cooldown_s=60)
    det = {'stamp': 10.0, 'camera': 'front', 'objects': [
        {'cls': 'person', 'conf': 0.99, 'bearing_rad': 0.0},
        {'cls': 'cat', 'conf': 0.8, 'bearing_rad': 0.3, 'bbox': [0, 0, 1, 1], 'track_id': 1}]}
    ev = p.update(det, 10.0)
    assert ev == [{'t': 'pet_detected', 'ts': 10.0, 'camera': 'front', 'cls': 'cat'}]
    assert p.bearing == 0.3
    assert p.update(det, 20.0) == []
    assert len(p.update(det, 71.0)) == 1
    assert p.update({'objects': [{'cls': 'cat', 'conf': 0.2}]}, 200.0) == []
    assert p.last_seen_ts == 10.0           # detection stamp, not receive time
    assert p.seen_within(12.0, 5.0) and not p.seen_within(72.0, 5.0)


# ------------------------------------------------------------------ geometry
def test_seek_rotates_first_then_approaches_capped():
    p = SeekParams()
    v, w, arrived = seek_cmd(1.0, 3.0, p)
    assert v == 0.0 and w > 0 and not arrived
    v, w, arrived = seek_cmd(0.05, 5.0, p)
    assert 0 < v <= 0.25 and not arrived
    v, w, arrived = seek_cmd(0.0, 0.85, p)
    assert v == 0.0 and arrived
    v, w, arrived = seek_cmd(0.0, 0.3, p)
    assert v == 0.0 and arrived          # too close: never advance
    assert seek_cmd(None, None, p) == (0.0, 0.0, False)
    p2 = SeekParams(v_max=1.0)
    assert seek_cmd(0.0, 10.0, p2)[0] <= 1.0


def test_predock_and_reverse():
    pre = predock_pose({'x': 1.0, 'y': 2.0, 'th': math.pi / 2}, 0.5)
    assert math.isclose(pre['x'], 1.0, abs_tol=1e-9) and math.isclose(pre['y'], 2.5)
    v, w = dock_reverse_cmd(yaw=0.1, dock_th=0.0, v=0.05)
    assert v == -0.05 and w < 0
    v, w = dock_reverse_cmd(yaw=3.0, dock_th=-3.0, v=0.05)   # wrap-around
    assert abs(w) <= 0.3 and w > 0


def test_range_at_bearing_full_scan():
    n = 360
    inc = 2 * math.pi / n
    ranges = [5.0] * n
    # obstacle at +90 deg (left), scan from -pi
    i = int(round((math.pi / 2 + math.pi) / inc))
    ranges[i] = 0.9
    assert range_at_bearing(ranges, -math.pi, inc, math.pi / 2) == 0.9
    assert range_at_bearing(ranges, -math.pi, inc, 0.0) == 5.0
    ranges[0] = 0.5   # at -pi == +pi (behind)
    assert range_at_bearing(ranges, -math.pi, inc, math.pi) == 0.5
    assert range_at_bearing([], -math.pi, inc, 0.0) is None
    assert range_at_bearing([float('inf')] * n, -math.pi, inc, 0.0) is None


# ------------------------------------------------------------------ POIs
def test_poi_store_roundtrip(tmp_path):
    path = str(tmp_path / 'sub' / 'pois.yaml')
    s = PoiStore(path)
    assert s.names() == []
    s.set('cucina', 1.23456, -2, 0.5)
    s.set('dock', 0, 0, 3.14)
    s2 = PoiStore(path)
    assert s2.names() == ['cucina', 'dock']
    assert s2.get('cucina') == {'x': 1.2346, 'y': -2.0, 'th': 0.5}
    with pytest.raises(ValueError):
        s2.set('../etc', 0, 0, 0)
    assert s2.remove('cucina') and not s2.remove('cucina')
    s2.clear()
    assert PoiStore(path).names() == []


# ------------------------------------------------------------------ SLAM manager
def test_slam_manager_fallback_and_stop(tmp_path):
    from petmate_behavior.slam_manager import SlamManager
    base = str(tmp_path / 'home')
    m = SlamManager(base, launch_cmd=['python3', '-c', 'import time; time.sleep(30)'])
    # no serialized map -> localization falls back to mapping
    assert m.start('localization') == 'mapping'
    assert m.running()
    m.stop(timeout=2.0)
    assert not m.running()
    for ext in ('.posegraph', '.data', '.pgm', '.yaml'):
        open(base + ext, 'w').close()
    assert m.map_exists()
    assert m.start('localization') == 'localization'
    m.stop(timeout=2.0)
    assert len(m.delete_map()) == 4 and not m.map_exists()
    with pytest.raises(ValueError):
        m.start('explore')
