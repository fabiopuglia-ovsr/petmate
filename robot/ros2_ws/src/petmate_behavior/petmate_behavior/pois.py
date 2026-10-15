"""POI store persisted at /var/lib/petmate/pois.yaml (INTERFACES.md §8).

File format (frame = map)::

    version: 1
    frame: map
    pois:
      dock:   {x: 0.0, y: 0.0, th: 0.0}
      cucina: {x: 2.1, y: -0.4, th: 1.57}
"""
from __future__ import annotations

import os
import re
import tempfile
from typing import Dict, List, Optional

import yaml

DEFAULT_PATH = '/var/lib/petmate/pois.yaml'
_NAME = re.compile(r'^[\w\- ]{1,32}$', re.UNICODE)


class PoiStore:
    def __init__(self, path: str = DEFAULT_PATH):
        self.path = path
        self.pois: Dict[str, dict] = {}
        self.load()

    def load(self):
        try:
            with open(self.path, 'r', encoding='utf-8') as f:
                data = yaml.safe_load(f) or {}
        except FileNotFoundError:
            data = {}
        raw = data.get('pois') or {}
        self.pois = {}
        for k, v in raw.items():
            try:
                self.pois[str(k)] = {'x': float(v['x']), 'y': float(v['y']),
                                     'th': float(v.get('th', 0.0))}
            except (KeyError, TypeError, ValueError):
                continue

    def save(self):
        d = os.path.dirname(self.path) or '.'
        os.makedirs(d, exist_ok=True)
        data = {'version': 1, 'frame': 'map', 'pois': self.pois}
        fd, tmp = tempfile.mkstemp(dir=d, prefix='.pois.', suffix='.yaml')
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            yaml.safe_dump(data, f, sort_keys=True, allow_unicode=True)
        os.replace(tmp, self.path)

    @staticmethod
    def valid_name(name: str) -> bool:
        return isinstance(name, str) and bool(_NAME.match(name))

    def set(self, name: str, x: float, y: float, th: float):
        if not self.valid_name(name):
            raise ValueError('invalid POI name')
        self.pois[name] = {'x': round(float(x), 4), 'y': round(float(y), 4),
                           'th': round(float(th), 4)}
        self.save()

    def get(self, name: str) -> Optional[dict]:
        return self.pois.get(name)

    def remove(self, name: str) -> bool:
        if name in self.pois:
            del self.pois[name]
            self.save()
            return True
        return False

    def clear(self):
        self.pois = {}
        self.save()

    def names(self) -> List[str]:
        return sorted(self.pois)
