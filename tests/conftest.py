"""
Shared test setup.

`storage.store` is a module-level singleton, so every test module in a run
shares one in-memory database. Without an explicit reset, whichever module
imports first owns the admin account and the others get 401s. reset_panel()
puts the process back to a first-run state.
"""
import os
import sys
import tempfile

# Must be set before `storage` is imported, or DATA_DIR binds to the real one.
os.environ.setdefault("PEYK_DATA_DIR", tempfile.mkdtemp(prefix="peyk_tests_"))
os.environ.setdefault("PEYK_PANEL_NAME", "TestPanel")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def reset_panel():
    """Wipe users, admin and live connection state; keep the same store object."""
    import main
    import storage

    fresh = storage._fresh_db()
    main.store.db.clear()
    main.store.db.update(fresh)
    main.store._dirty = False

    main.runtime["active"].clear()
    main.runtime["pending_traffic"].clear()
    main.runtime["pending_requests"].clear()
    main.runtime["colo"] = {"value": "FRA", "at": float("inf")}  # no outbound call


import pytest


@pytest.fixture
def anyio_backend():
    """Run @pytest.mark.anyio tests on asyncio only; trio is not installed."""
    return "asyncio"
