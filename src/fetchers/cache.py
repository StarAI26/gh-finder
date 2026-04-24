"""File-based cache with TTL + ETag support.

Pure stdlib: json + pathlib + time.
Each cache entry is a single JSON file with metadata (value, created_at, ttl).
ETags stored in a separate index file.
"""

from __future__ import annotations

import json
import time
import hashlib
from pathlib import Path
from typing import Optional

from src.core.interfaces import BaseCache

# Sentinel for cache misses
_NOT_FOUND = object()


class FileCache(BaseCache):
    """Local filesystem cache. Thread-safe for single-process use."""

    def __init__(self, cache_dir: str = "cache", default_ttl: int = 3600):
        self._dir = Path(cache_dir)
        self._dir.mkdir(parents=True, exist_ok=True)
        self._etag_file = self._dir / "_etags.json"
        self._default_ttl = default_ttl
        self._etags: dict[str, str] = {}
        self._load_etags()

    def _key_path(self, key: str) -> Path:
        """Hash key to avoid filesystem issues with special chars."""
        hashed = hashlib.sha256(key.encode()).hexdigest()[:16]
        return self._dir / f"{hashed}.json"

    def get(self, key: str) -> Optional[dict]:
        """Return cached value or None if expired/missing."""
        path = self._key_path(key)
        if not path.exists():
            return None
        try:
            with open(path, "r", encoding="utf-8") as f:
                entry = json.load(f)
            age = time.time() - entry["created_at"]
            if age > entry.get("ttl", self._default_ttl):
                path.unlink(missing_ok=True)
                return None
            return entry.get("value")
        except (json.JSONDecodeError, KeyError, OSError):
            return None

    def set(self, key: str, value: dict, ttl: int = 3600) -> None:
        """Store value with TTL."""
        path = self._key_path(key)
        entry = {
            "key": key,
            "value": value,
            "created_at": time.time(),
            "ttl": ttl,
        }
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(entry, f, ensure_ascii=False)
        except OSError:
            pass  # Cache write failure is non-fatal

    def get_etag(self, url: str) -> Optional[str]:
        """Return cached ETag for conditional requests."""
        return self._etags.get(url)

    def set_etag(self, url: str, etag: str) -> None:
        """Store ETag for conditional requests."""
        self._etags[url] = etag
        self._save_etags()

    def invalidate(self, pattern: str = "*") -> int:
        """Remove entries matching pattern. Return count removed."""
        count = 0
        if pattern == "*":
            for f in self._dir.glob("*.json"):
                f.unlink(missing_ok=True)
                count += 1
            self._etags.clear()
            self._save_etags()
        else:
            # Pattern as substring match on original key (stored in entry)
            for f in self._dir.glob("*.json"):
                try:
                    with open(f, "r") as fh:
                        entry = json.load(fh)
                    if pattern in entry.get("key", ""):
                        f.unlink(missing_ok=True)
                        count += 1
                except (json.JSONDecodeError, OSError):
                    pass
        return count

    def clear_expired(self) -> int:
        """Remove all expired entries. Return count removed."""
        count = 0
        now = time.time()
        for f in self._dir.glob("*.json"):
            if f.name.startswith("_"):
                continue
            try:
                with open(f, "r") as fh:
                    entry = json.load(fh)
                if now - entry["created_at"] > entry.get("ttl", self._default_ttl):
                    f.unlink(missing_ok=True)
                    count += 1
            except (json.JSONDecodeError, KeyError, OSError):
                f.unlink(missing_ok=True)
                count += 1
        return count

    def stats(self) -> dict:
        """Return cache statistics."""
        entries = list(self._dir.glob("*.json"))
        now = time.time()
        total = 0
        expired = 0
        for f in entries:
            if f.name.startswith("_"):
                continue
            total += 1
            try:
                with open(f, "r") as fh:
                    entry = json.load(fh)
                if now - entry["created_at"] > entry.get("ttl", self._default_ttl):
                    expired += 1
            except (json.JSONDecodeError, KeyError, OSError):
                expired += 1
        return {
            "total_entries": total,
            "expired_entries": expired,
            "valid_entries": total - expired,
            "etag_count": len(self._etags),
            "cache_dir": str(self._dir),
        }

    # ─── Internal ──────────────────────────────────────────────

    def _load_etags(self) -> None:
        try:
            if self._etag_file.exists():
                with open(self._etag_file, "r", encoding="utf-8") as f:
                    self._etags = json.load(f)
        except (json.JSONDecodeError, OSError):
            self._etags = {}

    def _save_etags(self) -> None:
        try:
            with open(self._etag_file, "w", encoding="utf-8") as f:
                json.dump(self._etags, f, ensure_ascii=False)
        except OSError:
            pass
