"""Startup/lifespan verification: real init_db, lock, ledger restore, tidy shutdown.

The full application lifespan is driven with the environment fully redirected to
a temporary database and the proactive runtime disabled. Only the OS-hotkey
engine is stubbed — firing the real one would re-register the host's persisted
global hotkeys, which tests must never do. Everything else (Alembic migrations,
DB lock, ledger restore, engine dispose) runs against the temp database for
real.
"""

from __future__ import annotations


class _StubHotkeyEngine:
    """Replacement for HotkeyEngine: tracks start/shutdown without OS calls."""

    def __init__(self) -> None:
        self.started = False
        self.shutdown_count = 0

    def start(self) -> None:
        self.started = True

    def shutdown(self) -> None:
        self.shutdown_count += 1
        self.started = False


async def test_lifespan_graceful_shutdown(tmp_path, monkeypatch):
    import backend.engines.hotkeys as hotkeys_module
    from backend.core.config import get_settings
    from backend.database.base import get_engine
    from backend.main import create_app

    db_path = tmp_path / "life.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{db_path.as_posix()}")
    monkeypatch.setenv("BERU_PROACTIVE_ENABLED", "false")
    get_settings.cache_clear()
    get_engine.cache_clear()
    hotkeys = _StubHotkeyEngine()
    monkeypatch.setattr(hotkeys_module, "get_hotkey_engine", lambda: hotkeys)

    app = create_app()
    async with app.router.lifespan_context(app):
        # Startup ran: migrations applied to the temp DB, ledger factory wired.
        assert db_path.exists()
        assert hotkeys.started is True
        from backend.services.activity_ledger import ledger_persistence_enabled

        assert ledger_persistence_enabled() is True

    # Teardown ran: hotkey listener stopped, connections released (a fresh
    # connection through the engine still works after dispose), no exceptions.
    assert hotkeys.started is False
    assert hotkeys.shutdown_count == 1
    engine = get_engine()
    async with engine.connect() as conn:
        await conn.exec_driver_sql("SELECT 1")
    get_engine.cache_clear()
    get_settings.cache_clear()


async def test_lifespan_can_restart_cleanly(tmp_path, monkeypatch):
    """A second full lifespan cycle succeeds — shutdown fully released resources."""
    import backend.engines.hotkeys as hotkeys_module
    from backend.core.config import get_settings
    from backend.database.base import get_engine
    from backend.main import create_app

    db_path = tmp_path / "restart.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{db_path.as_posix()}")
    monkeypatch.setenv("BERU_PROACTIVE_ENABLED", "false")
    get_settings.cache_clear()
    get_engine.cache_clear()
    hotkeys = _StubHotkeyEngine()
    monkeypatch.setattr(hotkeys_module, "get_hotkey_engine", lambda: hotkeys)

    app = create_app()
    async with app.router.lifespan_context(app):
        pass
    async with app.router.lifespan_context(app):
        pass  # second clean start/shutdown on the same DB

    assert hotkeys.shutdown_count == 2
    get_settings.cache_clear()
    get_engine.cache_clear()


async def test_lifespan_without_proactive_leaf_runtime(tmp_path, monkeypatch):
    """The proactive runtime stays off when disabled; lifespan still cycles."""
    import backend.engines.hotkeys as hotkeys_module
    from backend.core.config import get_settings
    from backend.main import create_app
    from backend.services.proactive_service import get_scheduler

    db_path = tmp_path / "solo.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{db_path.as_posix()}")
    monkeypatch.setenv("BERU_PROACTIVE_ENABLED", "false")
    get_settings.cache_clear()
    hotkeys = _StubHotkeyEngine()
    monkeypatch.setattr(hotkeys_module, "get_hotkey_engine", lambda: hotkeys)

    app = create_app()
    async with app.router.lifespan_context(app):
        assert get_scheduler().running is False  # proactive loop not started

    assert hotkeys.shutdown_count == 1
    get_settings.cache_clear()