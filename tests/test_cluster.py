"""
Multi-worker correctness.

The relay is pinned to one core, so throughput falls as concurrency rises.
Running N workers behind SO_REUSEPORT fixes that but breaks the single-process
assumption db.json was written under. These tests cover the two things that
would silently corrupt a live panel: workers clobbering each other's writes,
and per-user limits being granted N times over.
"""
import asyncio
import json
import os
import subprocess
import sys
import time

import pytest

import cluster
import storage

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# ------------------------------------------------------------------ file lock
def test_lock_is_exclusive(tmp_path):
    path = str(tmp_path / "db.lock")
    with cluster.FileLock(path):
        with pytest.raises(TimeoutError):
            cluster.FileLock(path, timeout=0.3).acquire()


def test_lock_released_on_exit(tmp_path):
    path = str(tmp_path / "db.lock")
    with cluster.FileLock(path):
        pass
    with cluster.FileLock(path, timeout=1):
        pass  # must not raise


def test_lock_released_when_body_raises(tmp_path):
    """A crash inside the critical section must not wedge every other worker."""
    path = str(tmp_path / "db.lock")
    with pytest.raises(ValueError):
        with cluster.FileLock(path):
            raise ValueError("boom")
    with cluster.FileLock(path, timeout=1):
        pass


def test_lock_serialises_across_real_processes(tmp_path):
    """Two OS processes must not hold the lock at once."""
    lock_path = str(tmp_path / "db.lock")
    out_path = str(tmp_path / "order.txt")
    code = (
        "import sys, time;"
        "sys.path.insert(0, %r);"
        "import cluster;"
        "lk = cluster.FileLock(%r, timeout=15);"
        "lk.acquire();"
        "f = open(%r, 'a');"
        "f.write('IN\\n'); f.flush();"
        "time.sleep(0.6);"
        "f.write('OUT\\n'); f.flush(); f.close();"
        "lk.release()"
    ) % (REPO, lock_path, out_path)

    procs = [subprocess.Popen([sys.executable, "-c", code]) for _ in range(3)]
    for p in procs:
        assert p.wait(timeout=60) == 0

    events = open(out_path).read().split()
    # Never two INs without the matching OUT between them.
    depth = 0
    for e in events:
        depth += 1 if e == "IN" else -1
        assert depth in (0, 1), f"overlapping critical sections: {events}"
    assert events.count("IN") == 3


# ------------------------------------------------------------------ store
@pytest.fixture
def mp_stores(tmp_path, monkeypatch):
    """Two Store objects over one db.json, as two workers would be."""
    data_dir = str(tmp_path / "data")
    os.makedirs(data_dir, exist_ok=True)
    monkeypatch.setattr(storage, "DATA_DIR", data_dir)
    monkeypatch.setattr(storage, "DB_PATH", os.path.join(data_dir, "db.json"))
    monkeypatch.setattr(storage, "LOCK_PATH", os.path.join(data_dir, "db.lock"))

    a, b = storage.Store(), storage.Store()
    a.enable_multiprocess()
    b.enable_multiprocess()
    return a, b


def test_worker_sees_siblings_write(mp_stores):
    a, b = mp_stores

    async def go():
        await a.mutate(lambda db: db["inbounds"].append(
            {"uid": "u1", "uuid": "x", "name": "FromA"}))
        # b has never been told; it must notice via the file's mtime.
        db = await b.get()
        assert [i["name"] for i in db["inbounds"]] == ["FromA"]

    asyncio.run(go())


def test_concurrent_writers_do_not_clobber(mp_stores):
    """The failure this guards against: A adds a user, B adds another, and
    whichever writes last silently drops the other's."""
    a, b = mp_stores

    async def go():
        await a.mutate(lambda db: db["inbounds"].append(
            {"uid": "ua", "uuid": "a", "name": "Ali"}))
        await b.mutate(lambda db: db["inbounds"].append(
            {"uid": "ub", "uuid": "b", "name": "Sara"}))

        on_disk = json.load(open(storage.DB_PATH, encoding="utf-8"))
        names = sorted(i["name"] for i in on_disk["inbounds"])
        assert names == ["Ali", "Sara"], f"a write was lost: {names}"

        # both live views agree
        assert sorted(i["name"] for i in (await a.get())["inbounds"]) == ["Ali", "Sara"]
        assert sorted(i["name"] for i in (await b.get())["inbounds"]) == ["Ali", "Sara"]

    asyncio.run(go())


def test_interleaved_writes_all_survive(mp_stores):
    a, b = mp_stores

    async def go():
        for i in range(10):
            target = a if i % 2 == 0 else b
            await target.mutate(
                lambda db, n=i: db["inbounds"].append(
                    {"uid": f"u{n}", "uuid": str(n), "name": f"user{n}"}))
        on_disk = json.load(open(storage.DB_PATH, encoding="utf-8"))
        assert len(on_disk["inbounds"]) == 10

    asyncio.run(go())


def test_get_reloads_in_place(mp_stores):
    """Callers hold the dict returned by get(); reloading must not swap the
    object out from under them."""
    a, b = mp_stores

    async def go():
        held = await b.get()
        await a.mutate(lambda db: db["inbounds"].append(
            {"uid": "u1", "uuid": "x", "name": "Later"}))
        refreshed = await b.get()
        assert refreshed is held, "reload replaced the dict instead of updating it"
        assert held["inbounds"][0]["name"] == "Later"

    asyncio.run(go())


def test_single_process_mode_takes_no_lock(tmp_path, monkeypatch):
    """Single-worker deployments must not pay for coordination they don't need."""
    data_dir = str(tmp_path / "data")
    os.makedirs(data_dir, exist_ok=True)
    monkeypatch.setattr(storage, "DATA_DIR", data_dir)
    monkeypatch.setattr(storage, "DB_PATH", os.path.join(data_dir, "db.json"))
    monkeypatch.setattr(storage, "LOCK_PATH", os.path.join(data_dir, "db.lock"))

    s = storage.Store()  # multiprocess NOT enabled

    async def go():
        with cluster.FileLock(storage.LOCK_PATH):
            # Would block forever if the store tried to take the same lock.
            await asyncio.wait_for(
                s.mutate(lambda db: db["inbounds"].append(
                    {"uid": "u", "uuid": "u", "name": "x"})),
                timeout=5)

    asyncio.run(go())
    assert len(s.get_sync()["inbounds"]) == 1


# ------------------------------------------------------------------ journals
def test_connection_counts_merge_across_workers(tmp_path):
    d = str(tmp_path)
    a = cluster.RuntimeJournal(d, worker_id=1)
    b = cluster.RuntimeJournal(d, worker_id=2)

    a_active = {"u1": {"c1": {"ip": "1.1.1.1", "since": 0}}}
    b_active = {"u1": {"c2": {"ip": "2.2.2.2", "since": 0},
                       "c3": {"ip": "3.3.3.3", "since": 0}}}
    a.publish(a_active, {}, {}, force=True)
    b.publish(b_active, {}, {}, force=True)

    # Each worker must see the total, or a 2-device cap becomes 2 per worker.
    assert a.merged_connection_counts(a_active)["u1"] == 3
    assert b.merged_connection_counts(b_active)["u1"] == 3


def test_active_ips_merge_and_dedupe(tmp_path):
    d = str(tmp_path)
    a = cluster.RuntimeJournal(d, worker_id=1)
    b = cluster.RuntimeJournal(d, worker_id=2)
    a_active = {"u1": {"c1": {"ip": "1.1.1.1", "since": 0}}}
    b_active = {"u1": {"c2": {"ip": "1.1.1.1", "since": 0},
                       "c3": {"ip": "9.9.9.9", "since": 0}}}
    a.publish(a_active, {}, {}, force=True)
    b.publish(b_active, {}, {}, force=True)
    assert a.merged_active_ips(a_active)["u1"] == ["1.1.1.1", "9.9.9.9"]


def test_journal_never_serialises_websockets(tmp_path):
    """The live table holds WebSocket objects; only ip/since may be published."""
    d = str(tmp_path)
    j = cluster.RuntimeJournal(d, worker_id=1)

    class FakeWS:
        pass

    active = {"u1": {"c1": {"ip": "1.1.1.1", "since": 123.0, "ws": FakeWS()}}}
    assert j.publish(active, {}, {}, force=True) is True
    written = json.load(open(j.path, encoding="utf-8"))
    assert written["active"]["u1"] == [{"ip": "1.1.1.1", "since": 123.0}]


def test_stale_journal_is_ignored_and_reaped(tmp_path):
    d = str(tmp_path)
    a = cluster.RuntimeJournal(d, worker_id=1)
    dead = cluster.RuntimeJournal(d, worker_id=99)
    dead.publish({"u1": {"c1": {"ip": "1.1.1.1", "since": 0}}}, {}, {}, force=True)

    # Backdate it past the staleness horizon.
    payload = json.load(open(dead.path, encoding="utf-8"))
    payload["ts"] = time.time() - cluster.JOURNAL_STALE_AFTER - 5
    json.dump(payload, open(dead.path, "w", encoding="utf-8"))

    assert a.merged_connection_counts({}) == {}
    assert not os.path.exists(dead.path), "stale journal was not reaped"


def test_traffic_from_siblings_is_visible(tmp_path):
    d = str(tmp_path)
    a = cluster.RuntimeJournal(d, worker_id=1)
    b = cluster.RuntimeJournal(d, worker_id=2)
    b.publish({}, {"u1": {"up": 100, "down": 200}}, {}, force=True)
    assert a.drain_others_traffic() == {"u1": {"up": 100, "down": 200}}
    # a's own buffer is never counted as a sibling's
    a.publish({}, {"u1": {"up": 5, "down": 5}}, {}, force=True)
    assert a.drain_others_traffic() == {"u1": {"up": 100, "down": 200}}


def test_publish_respects_interval(tmp_path):
    j = cluster.RuntimeJournal(str(tmp_path), worker_id=1)
    assert j.publish({}, {}, {}, force=True) is True
    assert j.publish({}, {}, {}) is False       # inside the interval
    assert j.publish({}, {}, {}, force=True) is True


def test_corrupt_journal_is_skipped(tmp_path):
    d = str(tmp_path)
    a = cluster.RuntimeJournal(d, worker_id=1)
    with open(os.path.join(d, "rt-77.json"), "w") as f:
        f.write("{not json")
    assert a.read_others() == {}          # must not raise


def test_cleanup_removes_all_journals(tmp_path):
    d = str(tmp_path)
    for wid in (1, 2, 3):
        cluster.RuntimeJournal(d, worker_id=wid).publish({}, {}, {}, force=True)
    assert len([f for f in os.listdir(d) if f.startswith("rt-")]) == 3
    cluster.cleanup_journals(d)
    assert not [f for f in os.listdir(d) if f.startswith("rt-")]


# ------------------------------------------------------------------ worker count
@pytest.mark.parametrize("setting,cores,expected", [
    # "auto" below two effective cores stays single-process: on a fraction of
    # a vCPU, extra workers only add memory and startup time, and a slow start
    # is what makes a platform report that no port was exposed.
    ("auto", 0.25, 1), ("auto", 0.5, 1), ("auto", 1, 1), ("auto", 1.9, 1),
    ("auto", 2, 2), ("auto", 4, 4), ("auto", 8, 8), ("auto", 32, 8),
    (None, 8, 8), ("", 8, 8),
    # An explicit number is always honoured, even on a small container.
    ("1", 8, 1), ("3", 8, 3), ("4", 0.5, 4),
    ("999", 8, 32), ("0", 8, 1), ("-5", 8, 1), ("garbage", 8, 1),
])
def test_resolve_workers(setting, cores, expected):
    assert cluster.resolve_workers(setting, cpu_count=cores) == expected


# ------------------------------------------------------------------ cgroup
def _quota(tmp_path, monkeypatch, files):
    """Point the cgroup reader at fixture files instead of the real ones."""
    real_open = cluster._read_first

    def fake(path):
        return files.get(path, None)

    monkeypatch.setattr(cluster, "_read_first", fake)
    try:
        return cluster.cgroup_cpu_quota()
    finally:
        monkeypatch.setattr(cluster, "_read_first", real_open)


def test_cgroup_v2_quota(tmp_path, monkeypatch):
    """Half a vCPU is the shape Railway-style platforms actually hand out."""
    assert _quota(tmp_path, monkeypatch, {"/sys/fs/cgroup/cpu.max": "50000 100000"}) == 0.5
    assert _quota(tmp_path, monkeypatch, {"/sys/fs/cgroup/cpu.max": "200000 100000"}) == 2.0


def test_cgroup_v2_unrestricted(tmp_path, monkeypatch):
    assert _quota(tmp_path, monkeypatch, {"/sys/fs/cgroup/cpu.max": "max 100000"}) is None


def test_cgroup_v1_quota(tmp_path, monkeypatch):
    assert _quota(tmp_path, monkeypatch, {
        "/sys/fs/cgroup/cpu/cpu.cfs_quota_us": "25000",
        "/sys/fs/cgroup/cpu/cpu.cfs_period_us": "100000",
    }) == 0.25


def test_cgroup_v1_unrestricted(tmp_path, monkeypatch):
    assert _quota(tmp_path, monkeypatch, {
        "/sys/fs/cgroup/cpu/cpu.cfs_quota_us": "-1",
        "/sys/fs/cgroup/cpu/cpu.cfs_period_us": "100000",
    }) is None


def test_cgroup_absent_or_malformed(tmp_path, monkeypatch):
    assert _quota(tmp_path, monkeypatch, {}) is None
    assert _quota(tmp_path, monkeypatch, {"/sys/fs/cgroup/cpu.max": "garbage"}) is None
    assert _quota(tmp_path, monkeypatch, {"/sys/fs/cgroup/cpu.max": "abc def"}) is None


def test_available_cpus_never_zero(monkeypatch):
    """A pathological reading must not disable the server entirely."""
    monkeypatch.setattr(cluster, "cgroup_cpu_quota", lambda: 0.0)
    assert cluster.available_cpus() > 0
    assert cluster.resolve_workers("auto") >= 1


def test_available_cpus_takes_the_smallest_constraint(monkeypatch):
    monkeypatch.setattr(cluster, "cgroup_cpu_quota", lambda: 0.5)
    monkeypatch.setattr(cluster.os, "cpu_count", lambda: 64)
    assert cluster.available_cpus() == 0.5


# ------------------------------------------------------------------ leader election
def test_only_one_worker_becomes_leader(tmp_path):
    """lifespan runs in every worker. Without election, each would send its
    own copy of every alert and upload its own backup."""
    path = str(tmp_path / "leader.lock")
    workers = [cluster.LeaderLock(path) for _ in range(5)]
    elected = [w for w in workers if w.try_acquire()]
    assert len(elected) == 1
    assert sum(w.is_leader() for w in workers) == 1


def test_leadership_is_stable_across_polls(tmp_path):
    """The leader must not hand off just because others keep asking."""
    path = str(tmp_path / "leader.lock")
    a, b = cluster.LeaderLock(path), cluster.LeaderLock(path)
    assert a.try_acquire() is True
    for _ in range(5):
        assert b.try_acquire() is False
        assert a.try_acquire() is True


def test_leadership_passes_on_when_the_leader_goes_away(tmp_path):
    path = str(tmp_path / "leader.lock")
    a, b = cluster.LeaderLock(path), cluster.LeaderLock(path)
    assert a.try_acquire() is True
    assert b.try_acquire() is False
    a.release()                      # worker dies / shuts down
    assert b.try_acquire() is True
    assert a.try_acquire() is False  # and does not reclaim it


def test_single_process_is_always_leader(monkeypatch):
    """No election object means one process, which owns everything."""
    import main
    monkeypatch.setitem(main.runtime, "leader", None)
    assert main._is_leader() is True


def test_multiworker_defers_to_the_lock(tmp_path, monkeypatch):
    import main
    path = str(tmp_path / "leader.lock")
    held = cluster.LeaderLock(path)
    assert held.try_acquire() is True
    try:
        monkeypatch.setitem(main.runtime, "leader", cluster.LeaderLock(path))
        assert main._is_leader() is False
    finally:
        held.release()
