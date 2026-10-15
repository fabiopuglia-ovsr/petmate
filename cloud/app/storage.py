"""Blob storage for clips. `LocalStorage` writes to disk; an S3-compatible backend can
implement the same interface (put/open/delete/url) later."""
from __future__ import annotations

import abc
import os
import shutil
from pathlib import Path
from typing import BinaryIO, Optional


class Storage(abc.ABC):
    @abc.abstractmethod
    def put(self, key: str, src: BinaryIO, max_bytes: Optional[int] = None) -> int:
        """Store stream under key; returns size in bytes. Raises ValueError if > max_bytes."""

    @abc.abstractmethod
    def local_path(self, key: str) -> Optional[Path]:
        """Filesystem path if the backend is local (served with FileResponse), else None."""

    @abc.abstractmethod
    def presigned_url(self, key: str, ttl_s: int = 3600) -> Optional[str]:
        """Direct download URL for remote backends; None for local."""

    @abc.abstractmethod
    def delete(self, key: str) -> None: ...

    @abc.abstractmethod
    def exists(self, key: str) -> bool: ...


class LocalStorage(Storage):
    def __init__(self, root: str | os.PathLike):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        p = (self.root / key).resolve()
        if self.root not in p.parents:
            raise ValueError("invalid storage key")
        return p

    def put(self, key, src, max_bytes=None):
        dst = self._path(key)
        dst.parent.mkdir(parents=True, exist_ok=True)
        tmp = dst.with_suffix(dst.suffix + ".part")
        size = 0
        try:
            with open(tmp, "wb") as f:
                while chunk := src.read(1024 * 1024):
                    size += len(chunk)
                    if max_bytes is not None and size > max_bytes:
                        raise ValueError("file too large")
                    f.write(chunk)
            os.replace(tmp, dst)
        finally:
            if tmp.exists():
                tmp.unlink()
        return size

    def local_path(self, key):
        p = self._path(key)
        return p if p.exists() else None

    def presigned_url(self, key, ttl_s=3600):
        return None

    def delete(self, key):
        p = self._path(key)
        if p.exists():
            p.unlink()

    def exists(self, key):
        return self._path(key).exists()

    def wipe(self) -> None:  # tests
        shutil.rmtree(self.root, ignore_errors=True)
        self.root.mkdir(parents=True, exist_ok=True)
