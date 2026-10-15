"""Owns the slam_toolbox process so mapping/localization can be switched at
runtime from pm-core commands (slam_toolbox's mode is fixed per process).

Runs ``ros2 launch petmate_bringup slam.launch.py mode:=<m> map_file:=<base>``
in its own process group. No ROS imports: unit-testable with a fake command.
"""
from __future__ import annotations

import glob
import os
import signal
import subprocess
import time
from typing import List, Optional


class SlamManager:
    def __init__(self, map_base: str, launch_cmd: Optional[List[str]] = None,
                 extra_args: Optional[List[str]] = None, logger=None):
        self.map_base = map_base
        self.launch_cmd = launch_cmd or ['ros2', 'launch', 'petmate_bringup', 'slam.launch.py']
        self.extra_args = extra_args or []
        self.mode: Optional[str] = None
        self.proc: Optional[subprocess.Popen] = None
        self.log = logger

    def _info(self, msg):
        if self.log:
            self.log.info(msg)

    def map_exists(self) -> bool:
        return os.path.isfile(self.map_base + '.posegraph') and os.path.isfile(self.map_base + '.data')

    def running(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def start(self, mode: str) -> str:
        """Start in ``mode`` ('mapping'|'localization'). Falls back to mapping if
        no serialized map exists. Returns the effective mode."""
        if mode not in ('mapping', 'localization'):
            raise ValueError(mode)
        if mode == 'localization' and not self.map_exists():
            self._info(f'no map at {self.map_base}.posegraph: falling back to mapping')
            mode = 'mapping'
        self.stop()
        args = self.launch_cmd + [f'mode:={mode}', f'map_file:={self.map_base}'] + self.extra_args
        self._info('starting slam: ' + ' '.join(args))
        self.proc = subprocess.Popen(args, start_new_session=True)
        self.mode = mode
        return mode

    def stop(self, timeout: float = 8.0):
        p = self.proc
        self.proc = None
        self.mode = None
        if p is None or p.poll() is not None:
            return
        for sig, wait in ((signal.SIGINT, timeout), (signal.SIGTERM, 3.0), (signal.SIGKILL, 2.0)):
            try:
                os.killpg(p.pid, sig)
            except ProcessLookupError:
                return
            t0 = time.monotonic()
            while time.monotonic() - t0 < wait:
                if p.poll() is not None:
                    return
                time.sleep(0.05)

    def delete_map(self) -> List[str]:
        removed = []
        for ext in ('.posegraph', '.data', '.pgm', '.yaml'):
            for f in glob.glob(self.map_base + ext):
                os.remove(f)
                removed.append(f)
        return removed
