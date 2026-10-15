"""Small shared helpers."""

from __future__ import annotations

import asyncio
import json
import math
import os
import random
import tempfile
from pathlib import Path
from typing import Any, Awaitable, Callable


def atomic_write_json(path: Path, data: Any, mode: int = 0o600) -> None:
    """Write JSON atomically: temp file in same dir + fsync + rename + dir fsync."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2, sort_keys=False)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
        try:
            dfd = os.open(str(path.parent), os.O_DIRECTORY)
            try:
                os.fsync(dfd)
            finally:
                os.close(dfd)
        except OSError:
            pass
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise


def read_json(path: Path) -> Any:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def wrap_pi(a: float) -> float:
    """Wrap an angle to [-pi, pi]."""
    a = math.fmod(a + math.pi, 2.0 * math.pi)
    if a < 0:
        a += 2.0 * math.pi
    r = a - math.pi
    # fmod maps +pi to -pi; keep +pi representable for exact inputs
    return r


class Backoff:
    """Exponential backoff with full jitter."""

    def __init__(self, base: float = 1.0, cap: float = 60.0, rng: random.Random | None = None):
        self.base = base
        self.cap = cap
        self.attempt = 0
        self._rng = rng or random.Random()

    def next_delay(self) -> float:
        hi = min(self.cap, self.base * (2 ** self.attempt))
        self.attempt += 1
        return self._rng.uniform(hi / 2.0, hi)

    def reset(self) -> None:
        self.attempt = 0


def spawn(coro: Awaitable[Any], name: str, log: Any = None) -> asyncio.Task:
    """create_task that logs unexpected exceptions instead of losing them."""
    task = asyncio.ensure_future(coro)
    try:
        task.set_name(name)
    except AttributeError:
        pass

    def _done(t: asyncio.Task) -> None:
        if t.cancelled():
            return
        exc = t.exception()
        if exc is not None and log is not None:
            log.error("task %s crashed: %r", name, exc, exc_info=exc)

    task.add_done_callback(_done)
    return task


async def run_cmd(*args: str, timeout: float = 30.0, input_data: bytes | None = None) -> tuple[int, str, str]:
    """Run a subprocess asynchronously, returning (rc, stdout, stderr)."""
    proc = await asyncio.create_subprocess_exec(
        *args,
        stdin=asyncio.subprocess.PIPE if input_data is not None else None,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(input_data), timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return 124, "", "timeout"
    return proc.returncode or 0, out.decode(errors="replace"), err.decode(errors="replace")


Callback = Callable[..., Any]
