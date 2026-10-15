"""Pure validation of §4.2 commands received on ZMQ topic ``cmd``."""
from __future__ import annotations

import math
import re
import uuid
from typing import Optional, Tuple

TELEOP_V_MAX = 0.50   # §1 teleop limit
W_MAX = 2.0           # §1

GAMES = ('chase_ball', 'teaser', 'screen_birds', 'laser_dots')
LED_MODES = ('solid', 'breathe', 'rainbow', 'chase', 'off')
TEASE_PATTERNS = ('wiggle', 'sweep', 'random', 'stop')
INTAKE_MODES = ('in', 'out', 'off')
MODES = ('auto', 'manual', 'sleep')
GOTO_TARGETS = ('dock', 'pet', 'poi')
MAP_ACTIONS = ('start_mapping', 'stop_mapping', 'save', 'clear')

# Commands handled directly in the bridge (not forwarded to behavior)
BRIDGE_LOCAL = ('teleop',)
KNOWN = ('teleop', 'feed', 'feed_abort', 'launch', 'intake', 'tease', 'led', 'goto',
         'mode', 'estop', 'play', 'save_poi', 'map')

_HEX = re.compile(r'^#?[0-9A-Fa-f]{6}$')
_POI = re.compile(r'^[\w\- ]{1,32}$', re.UNICODE)


def _num(d: dict, k: str) -> Optional[float]:
    v = d.get(k)
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    v = float(v)
    return v if math.isfinite(v) else None


def ensure_id(cmd: dict) -> str:
    cid = cmd.get('id')
    if not isinstance(cid, str) or not cid:
        cid = str(uuid.uuid4())
        cmd['id'] = cid
    return cid


def validate(cmd) -> Tuple[bool, Optional[str]]:
    """Return (ok, error). Range/enum checks only; semantic checks (busy,
    unknown POI, daily max…) are done by petmate_behavior."""
    if not isinstance(cmd, dict):
        return False, 'not_an_object'
    t = cmd.get('t')
    if t not in KNOWN:
        return False, f'unknown_command:{t}'
    if t == 'teleop':
        v, w = _num(cmd, 'v'), _num(cmd, 'w')
        if v is None or w is None:
            return False, 'teleop requires numeric v,w'
    elif t == 'feed':
        g = _num(cmd, 'grams')
        if g is None or not (1 <= g <= 60):
            return False, 'grams must be 1..60'
    elif t == 'launch':
        p = _num(cmd, 'power') if 'power' in cmd else 40.0
        c = _num(cmd, 'count') if 'count' in cmd else 1.0
        if p is None or c is None or not (0 <= p <= 100) or not (1 <= c <= 4):
            return False, 'power 0..100 (clamped 20..70), count 1..4'
    elif t == 'intake':
        if cmd.get('mode') not in INTAKE_MODES:
            return False, 'mode must be in|out|off'
    elif t == 'tease':
        if cmd.get('pattern') not in TEASE_PATTERNS:
            return False, 'pattern must be wiggle|sweep|random|stop'
        d = _num(cmd, 'duration_s') if 'duration_s' in cmd else 20.0
        if d is None or not (0 <= d <= 65.535):
            return False, 'duration_s must be 0..65'
    elif t == 'led':
        if cmd.get('mode') not in LED_MODES:
            return False, 'bad led mode'
        col = cmd.get('color', '#FFFFFF')
        if not isinstance(col, str) or not _HEX.match(col):
            return False, 'color must be #RRGGBB'
    elif t == 'goto':
        tgt = cmd.get('target')
        if tgt not in GOTO_TARGETS:
            return False, 'target must be dock|pet|poi'
        if tgt == 'poi' and not (isinstance(cmd.get('poi'), str) and _POI.match(cmd['poi'])):
            return False, 'poi name required'
    elif t == 'mode':
        if cmd.get('mode') not in MODES:
            return False, 'mode must be auto|manual|sleep'
    elif t == 'estop':
        if not isinstance(cmd.get('engage'), bool):
            return False, 'engage must be bool'
    elif t == 'play':
        if cmd.get('game') not in GAMES:
            return False, 'unknown game'
        d = _num(cmd, 'duration_s') if 'duration_s' in cmd else 300.0
        if d is None or not (1 <= d <= 3600):
            return False, 'duration_s must be 1..3600'
    elif t == 'save_poi':
        n = cmd.get('name')
        if not (isinstance(n, str) and _POI.match(n)):
            return False, 'name must be 1..32 chars [A-Za-z0-9_- ]'
    elif t == 'map':
        if cmd.get('action') not in MAP_ACTIONS:
            return False, 'bad map action'
    return True, None


def clamp_teleop(v: float, w: float) -> Tuple[float, float]:
    v = max(-TELEOP_V_MAX, min(TELEOP_V_MAX, float(v)))
    w = max(-W_MAX, min(W_MAX, float(w)))
    return v, w
