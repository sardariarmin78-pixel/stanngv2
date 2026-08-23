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


def pytest_configure(config):
    config.addinivalue_line(
        "markers", "allow_network: let this test reach the real Telegram API")


@pytest.fixture
def anyio_backend():
    """Run @pytest.mark.anyio tests on asyncio only; trio is not installed."""
    return "asyncio"


@pytest.fixture(autouse=True)
def no_outbound_telegram(request, monkeypatch):
    """Redirect every Telegram call at an address that refuses instantly.

    The suite used to reach api.telegram.org for real. On a good connection
    those calls failed fast and nobody noticed; on a flaky one the same run
    took 78 minutes instead of two, because each call sat waiting for a
    timeout. Worse, it meant results depended on connectivity.

    Pointing the base URL at a closed local port keeps the code paths intact --
    the callers still see a connection error and take their failure branch --
    while removing both the wait and the dependency. Tests that stub these
    modules themselves are unaffected; tests that genuinely want the real host
    can ask for it with @pytest.mark.allow_network.
    """
    if request.node.get_closest_marker("allow_network"):
        return
    for module in ("userbot", "notify", "backup"):
        try:
            mod = __import__(module)
        except ImportError:      # pragma: no cover - module always present
            continue
        if hasattr(mod, "TELEGRAM_API"):
            # 127.0.0.1:9 is the discard port: nothing listens, so connections
            # are refused immediately rather than hanging.
            monkeypatch.setattr(mod, "TELEGRAM_API", "http://127.0.0.1:9")
