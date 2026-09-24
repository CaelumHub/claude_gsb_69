"""Durable JSON storage with atomic replacement and rollback support.

Every persistent mutation goes through :func:`atomic_write_json`, which writes
to a temp file in the target directory, ``fsync``s it, and ``os.replace``s it
over the destination.  ``os.replace`` is atomic on POSIX filesystems, so a
reader can never observe a half-written file — this is the foundation of the
"JSON node sync atomic replacement" requirement.

A light version ledger (``versions.json``) records the chain head height/hash
at each write so the admin backend can roll the chain back to an earlier head.
"""

import json
import os
import tempfile
import time


# --------------------------------------------------------------------------- #
# Low-level atomic JSON IO
# --------------------------------------------------------------------------- #
def atomic_write_json(path, obj):
    """Atomically write ``obj`` as pretty-printed JSON to ``path``."""
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=directory or ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(obj, f, indent=2, sort_keys=True, ensure_ascii=False)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def read_json(path, default=None):
    """Read a JSON file, returning ``default`` when missing or unreadable."""
    if not os.path.exists(path):
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return default


def canonical_json(obj) -> bytes:
    """Deterministic JSON encoding used for hashing state / committing roots."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False).encode("utf-8")


def ensure_dir(path):
    os.makedirs(path, exist_ok=True)


def list_json_files(directory):
    """Return sorted names of ``*.json`` files in a directory."""
    if not os.path.isdir(directory):
        return []
    return sorted(f for f in os.listdir(directory) if f.endswith(".json"))


# --------------------------------------------------------------------------- #
# Version ledger (for rollback)
# --------------------------------------------------------------------------- #
class VersionLedger:
    """Tracks chain head snapshots so the chain can be rolled back.

    Each entry stores ``height``, ``head_hash``, and a timestamp.  Rolling back
    re-links the chain to a previous head and deletes blocks above it.
    """

    def __init__(self, path):
        self.path = path
        self.entries = read_json(path, [])
        self._max_entries = 200

    def record(self, height, head_hash, meta=None):
        entry = {
            "height": height,
            "head_hash": head_hash,
            "time": time.time(),
            "meta": meta or {},
        }
        # De-duplicate consecutive identical heads.
        if self.entries and self.entries[-1]["head_hash"] == head_hash:
            self.entries[-1] = entry
        else:
            self.entries.append(entry)
        if len(self.entries) > self._max_entries:
            self.entries = self.entries[-self._max_entries:]
        atomic_write_json(self.path, self.entries)

    def latest(self):
        return self.entries[-1] if self.entries else None

    def get(self, height):
        """Return the newest recorded head at or below ``height``."""
        best = None
        for e in self.entries:
            if e["height"] <= height:
                best = e
        return best

    def all(self):
        return list(self.entries)

    def truncate_after(self, height):
        self.entries = [e for e in self.entries if e["height"] <= height]
        atomic_write_json(self.path, self.entries)


# --------------------------------------------------------------------------- #
# File-path helpers for a node data directory
# --------------------------------------------------------------------------- #
class DataPaths:
    def __init__(self, root, cfg):
        from . import config as _cfg
        self.root = root
        self.blocks_dir = os.path.join(root, _cfg.BLOCKS_SUBDIR)
        self.state_dir = os.path.join(root, _cfg.STATE_SUBDIR)
        self.contracts_dir = os.path.join(root, _cfg.CONTRACTS_SUBDIR)
        self.meta_path = os.path.join(root, _cfg.META_FILE)
        self.txpool_path = os.path.join(root, _cfg.TXPOOL_FILE)
        self.wallets_path = os.path.join(root, _cfg.WALLETS_FILE)
        self.versions_path = os.path.join(root, _cfg.VERSIONS_FILE)
        self.logs_path = os.path.join(root, _cfg.LOGS_FILE)

    def block_path(self, height):
        return os.path.join(self.blocks_dir, "%06d.json" % height)

    def state_path(self, height):
        return os.path.join(self.state_dir, "%06d.json" % height)

    def contract_path(self, address):
        return os.path.join(self.contracts_dir, address + ".json")

    def ensure(self):
        for d in (self.blocks_dir, self.state_dir, self.contracts_dir):
            ensure_dir(d)
