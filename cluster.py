"""
Cross-process coordination for multi-worker mode.

The relay is CPU-bound on one core: measured aggregate throughput *falls*
from ~700 Mbit/s at one stream to ~370 Mbit/s at 64 while the process sits
pinned at ~110% CPU. Running N workers behind SO_REUSEPORT fixes that, but
db.json was written on the assumption of a single process.

Two mechanisms make that safe:

1. **One writer at a time.** An advisory lock file serialises the
   read-modify-write of db.json. Admin actions are rare, so contention is
   negligible; the hot path never takes it.

2. **Per-worker runtime journals.** Traffic, request counts and live
   connections are written by each worker to its own small file and merged
   by readers. No worker ever writes another's file, so there is nothing to
   lock and a crashed worker just leaves a stale file that ages out.

Limits therefore converge within one journal interval (~1s) rather than
being exact instantaneously. For a connection cap that is the right
trade: a user might briefly hold one extra session, versus every worker
independently granting the full allowance.
"""
import glob
import json
import os
import sys
import time
from typing import Dict, Optional

# How often a worker publishes its runtime slice.
JOURNAL_INTERVAL = 1.0
# A journal older than this belongs to a worker that died; ignore it.
JOURNAL_STALE_AFTER = 15.0
# Give up rather than deadlock if another worker wedged while holding the lock.
LOCK_TIMEOUT = 10.0
LOCK_POLL = 0.02

_IS_WINDOWS = sys.platform == "win32"

if _IS_WINDOWS:
    import msvcrt
else:
    import fcntl


class FileLock:
    """Advisory cross-process lock, usable as a context manager.

    Uses fcntl on POSIX and msvcrt on Windows so the test suite and the
    production target behave the same way.
    """

    def __init__(self, path: str, timeout: float = LOCK_TIMEOUT):
        self.path = path
        self.timeout = timeout
        self._fh = None

    def acquire(self):
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        self._fh = open(self.path, "a+b")
        deadline = time.monotonic() + self.timeout
        while True:
            try:
                if _IS_WINDOWS:
                    msvcrt.locking(self._fh.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    fcntl.flock(self._fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                return
            except OSError:
                if time.monotonic() >= deadline:
                    self._fh.close()
                    self._fh = None
                    raise TimeoutError(f"could not lock {self.path} within {self.timeout}s")
                time.sleep(LOCK_POLL)

    def release(self):
        if self._fh is None:
            return
        try:
            if _IS_WINDOWS:
                self._fh.seek(0)
                msvcrt.locking(self._fh.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        finally:
            self._fh.close()
            self._fh = None

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, *exc):
        self.release()
        return False


class RuntimeJournal:
    """This worker's slice of live state, and the merged view of everyone's.

    Each worker owns exactly one file named by its pid. Writes are atomic
    (temp + replace) so a reader never sees half a document.
    """

    def __init__(self, directory: str, worker_id: Optional[int] = None):
        self.dir = directory
        self.worker_id = worker_id if worker_id is not None else os.getpid()
        self.path = os.path.join(directory, f"rt-{self.worker_id}.json")
        self._last_write = 0.0
        os.makedirs(directory, exist_ok=True)

    # ---------------------------------------------------------------- write
    def publish(self, active: Dict[str, list], traffic: Dict[str, dict],
                requests: Dict[str, int], force: bool = False) -> bool:
        """Write this worker's slice. Returns True if it hit the disk."""
        now = time.time()
        if not force and now - self._last_write < JOURNAL_INTERVAL:
            return False
        payload = {
            "worker": self.worker_id,
            "ts": now,
            # Only the fields other workers need: never the WebSocket objects.
            "active": {uid: [{"ip": c["ip"], "since": c["since"]} for c in conns.values()]
                       for uid, conns in active.items() if conns},
            "traffic": traffic,
            "requests": requests,
        }
        tmp = f"{self.path}.tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(payload, f)
            os.replace(tmp, self.path)
            self._last_write = now
            return True
        except OSError:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            return False

    def remove(self):
        for p in (self.path, f"{self.path}.tmp"):
            try:
                os.unlink(p)
            except OSError:
                pass

    # ---------------------------------------------------------------- read
    def read_others(self) -> Dict[int, dict]:
        """Every live sibling journal, keyed by worker id (excluding ours)."""
        out = {}
        now = time.time()
        for path in glob.glob(os.path.join(self.dir, "rt-*.json")):
            if os.path.abspath(path) == os.path.abspath(self.path):
                continue
            try:
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
            except (OSError, json.JSONDecodeError):
                continue
            if not isinstance(data, dict):
                continue
            if now - data.get("ts", 0) > JOURNAL_STALE_AFTER:
                # Worker is gone. Reap it so the directory does not accumulate.
                try:
                    os.unlink(path)
                except OSError:
                    pass
                continue
            out[data.get("worker", path)] = data
        return out

    def merged_connection_counts(self, local_active: Dict[str, list]) -> Dict[str, int]:
        """Live sessions per uid across every worker, including this one."""
        counts = {uid: len(conns) for uid, conns in local_active.items() if conns}
        for data in self.read_others().values():
            for uid, conns in (data.get("active") or {}).items():
                counts[uid] = counts.get(uid, 0) + len(conns)
        return counts

    def merged_active_ips(self, local_active: Dict[str, list]) -> Dict[str, list]:
        """Connected IPs per uid across every worker."""
        # local_active maps uid -> {conn_id: record}; iterate the records.
        ips = {uid: {c["ip"] for c in conns.values()}
               for uid, conns in local_active.items() if conns}
        for data in self.read_others().values():
            for uid, conns in (data.get("active") or {}).items():
                ips.setdefault(uid, set()).update(c.get("ip") for c in conns if c.get("ip"))
        return {uid: sorted(v) for uid, v in ips.items()}

    def drain_others_traffic(self) -> Dict[str, dict]:
        """Sum sibling traffic. Read-only — each worker clears its own."""
        totals: Dict[str, dict] = {}
        for data in self.read_others().values():
            for uid, delta in (data.get("traffic") or {}).items():
                acc = totals.setdefault(uid, {"up": 0, "down": 0})
                acc["up"] += delta.get("up", 0)
                acc["down"] += delta.get("down", 0)
        return totals


def cleanup_journals(directory: str):
    """Clear every journal. Called once at startup, before workers fork."""
    for path in glob.glob(os.path.join(directory, "rt-*.json*")):
        try:
            os.unlink(path)
        except OSError:
            pass


def resolve_workers(setting, cpu_count: Optional[int] = None) -> int:
    """Translate the WORKERS setting into a process count.

    "auto" uses every core, which is the point of the exercise, but is capped
    so a 64-core box does not spawn 64 copies of a JSON database.
    """
    cores = cpu_count or (os.cpu_count() or 1)
    if setting in (None, "", "auto"):
        return max(1, min(8, cores))
    try:
        n = int(setting)
    except (TypeError, ValueError):
        return 1
    return max(1, min(32, n))
