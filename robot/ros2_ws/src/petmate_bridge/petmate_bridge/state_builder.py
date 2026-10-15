"""Pure builder for the §4.3 state JSON published on ZMQ topic ``state``.

Inputs are plain dicts/values gathered by the bridge node from ROS topics, so
this module is testable without ROS.
"""
from __future__ import annotations

import math
from typing import Any, Dict, Optional

ACTIVITIES = ('idle', 'patrolling', 'feeding', 'playing', 'docking', 'undocking',
              'teleop', 'seeking_pet', 'mapping', 'sleeping', 'error')
NAV_STATUSES = ('idle', 'active', 'succeeded', 'failed', 'canceled')
FEED_STATES = ('idle', 'dispensing', 'done', 'jam', 'hopper_empty', 'aborted')
LAUNCH_STATES = ('idle', 'spinning_up', 'launching', 'done', 'empty')


def _f(v, nd=3, default=0.0) -> float:
    try:
        v = float(v)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(v):
        return default
    return round(v, nd)


def _goal(g) -> Optional[Dict[str, Any]]:
    """§9.11: nav.goal = {"target": "dock"|"pet"|"poi", "poi": str|null} or null.
    petmate_behavior stores a bare goal name (dock, pet or the POI name)."""
    if g is None or g == '':
        return None
    if isinstance(g, dict):
        return {'target': g.get('target'), 'poi': g.get('poi')}
    g = str(g)
    if g in ('dock', 'pet'):
        return {'target': g, 'poi': None}
    return {'target': 'poi', 'poi': g}


def build_state(ts: float,
                hw: Optional[Dict[str, Any]] = None,
                pose: Optional[Dict[str, Any]] = None,
                behavior: Optional[Dict[str, Any]] = None,
                feed: Optional[Dict[str, Any]] = None,
                launch: Optional[Dict[str, Any]] = None,
                teleop_active: bool = False) -> Dict[str, Any]:
    """Assemble the state object.

    hw:       {soc, voltage, current, charging, docked, estop, cliff_mask,
               bump_mask, wheel_drop, motor_fault, fw, connected}
    pose:     {x, y, th, localized}
    behavior: {mode, activity, today_g, last_meal, nav{status,goal,mapping},
               pet{last_seen_ts,camera,bearing_rad}, pois[]}
    feed:     {state, hopper_g}
    launch:   {state, balls}
    """
    hw = hw or {}
    pose = pose or {}
    beh = behavior or {}
    feed = feed or {}
    launch = launch or {}

    activity = beh.get('activity', 'idle')
    if activity not in ACTIVITIES:
        activity = 'error'
    if teleop_active and activity in ('idle', 'mapping') and beh.get('mode') != 'sleep':
        activity = 'teleop' if activity == 'idle' else activity
    if hw and not hw.get('connected', True):
        activity = 'error'

    nav = beh.get('nav') or {}
    nav_status = nav.get('status', 'idle')
    if nav_status not in NAV_STATUSES:
        nav_status = 'idle'

    pet = beh.get('pet') or {}
    last_meal = beh.get('last_meal')
    if last_meal is not None:
        last_meal = {'ts': _f(last_meal.get('ts'), 3), 'grams': _f(last_meal.get('grams'), 1)}

    feed_state = feed.get('state', 'idle')
    if feed_state not in FEED_STATES:
        feed_state = 'idle'
    launch_state = launch.get('state', 'idle')
    if launch_state not in LAUNCH_STATES:
        launch_state = 'idle'

    hopper = feed.get('hopper_g')
    return {
        't': 'state',
        'ts': _f(ts, 3),
        'battery': {
            'soc': int(max(0, min(100, int(hw.get('soc', 0) or 0)))),
            'voltage': _f(hw.get('voltage'), 2),
            'current': _f(hw.get('current'), 2),
            'charging': bool(hw.get('charging', False)),
            'docked': bool(hw.get('docked', False)),
        },
        'pose': {
            'x': _f(pose.get('x'), 3),
            'y': _f(pose.get('y'), 3),
            'th': _f(pose.get('th'), 3),
            'localized': bool(pose.get('localized', False)),
        },
        'mode': beh.get('mode', 'auto'),
        'activity': activity,
        'feeder': {
            'state': feed_state,
            'hopper_g': _f(hopper, 1) if hopper is not None else None,
            'today_g': _f(beh.get('today_g', 0.0), 1),
            'last_meal': last_meal,
        },
        'launcher': {
            'state': launch_state,
            'balls': int(launch.get('balls', 0) or 0),
        },
        'safety': {
            'estop': bool(hw.get('estop', False)),
            'cliff_mask': int(hw.get('cliff_mask', 0) or 0),
            'bump_mask': int(hw.get('bump_mask', 0) or 0),
            'wheel_drop': bool(hw.get('wheel_drop', False)),
            'motor_fault': bool(hw.get('motor_fault', False)),
        },
        'nav': {
            'status': nav_status,
            'goal': _goal(nav.get('goal')),
            'mapping': bool(nav.get('mapping', False)),
        },
        'pet': {
            'last_seen_ts': _f(pet['last_seen_ts'], 3) if pet.get('last_seen_ts') is not None else None,
            'camera': pet.get('camera'),
            'bearing_rad': _f(pet['bearing_rad'], 3) if pet.get('bearing_rad') is not None else None,
        },
        'pois': sorted(str(p) for p in (beh.get('pois') or []) if p != 'dock'),
        'fw': hw.get('fw') or '',
    }
