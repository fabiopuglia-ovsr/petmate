"""On-robot paths (INTERFACES.md §8).

``PETMATE_ROOT`` (env) prefixes every path, which allows running pm-core on a
development machine without touching /etc or /var.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Paths:
    root: Path

    @property
    def config(self) -> Path:  # §5 / §8
        return self.root / "var/lib/petmate/config.json"

    @property
    def identity(self) -> Path:  # §8 (factory)
        return self.root / "etc/petmate/identity.json"

    @property
    def cloud_state(self) -> Path:  # §8
        return self.root / "var/lib/petmate/cloud.json"

    @property
    def clips_dir(self) -> Path:  # §8
        return self.root / "var/lib/petmate/clips"

    @property
    def host_settings(self) -> Path:
        """Optional host-level tuning for pm-core (not part of §5 config)."""
        return self.root / "etc/petmate/core.json"

    @classmethod
    def from_env(cls) -> "Paths":
        return cls(Path(os.environ.get("PETMATE_ROOT", "/")))
