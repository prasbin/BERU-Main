"""Tests for the release tooling."""

from __future__ import annotations

import re
from unittest.mock import Mock, patch

import pytest

import scripts.release as R
from scripts.release import (
    _next_version,
    _normalise,
    check_locks,
    check_version_consistency,
    migration_roundtrip,
)


def test_next_version():
    assert _next_version("0.1.0", "patch", None) == "0.1.1"
    assert _next_version("0.1.3", "minor", None) == "0.2.0"
    assert _next_version("2.5.9", "major", None) == "3.0.0"
    assert _next_version("0.1.0", None, "9.9.9") == "9.9.9"


def test_next_version_rejects_invalid():
    with pytest.raises(SystemExit):
        R.main(["bump", "nonsense"])


def test_normalise():
    assert _normalise("PyAutoGUI") == "pyautogui"
    assert _normalise("typing_extensions") == "typing-extensions"
    assert _normalise("my_lib_v2") == "my-lib-v2"


def test_version_consistency_success(tmp_path):
    backend_init = tmp_path / "backend" / "__init__.py"
    pyproject = tmp_path / "pyproject.toml"
    backend_init.parent.mkdir()
    backend_init.write_text('__version__ = "3.2.1"\n', encoding="utf-8")
    pyproject.write_text('[project]\nversion = "3.2.1"\n', encoding="utf-8")

    fake_settings = Mock(version="3.2.1")
    with (
        patch.object(R, "BACKEND_INIT", backend_init),
        patch.object(R, "PYPROJECT", pyproject),
        patch("backend.core.config.get_settings", return_value=fake_settings),
    ):
        bp, pp, sp = check_version_consistency()
    assert bp == pp == sp == "3.2.1"


def test_version_consistency_failure(tmp_path):
    backend_init = tmp_path / "backend" / "__init__.py"
    pyproject = tmp_path / "pyproject.toml"
    backend_init.parent.mkdir()
    backend_init.write_text('__version__ = "1.0.0"\n', encoding="utf-8")
    pyproject.write_text('[project]\nversion = "2.0.0"\n', encoding="utf-8")
    with patch.object(R, "BACKEND_INIT", backend_init), patch.object(R, "PYPROJECT", pyproject):
        with pytest.raises(RuntimeError, match="Version mismatch"):
            check_version_consistency()


def test_migration_roundtrip_succeeds():
    migration_roundtrip()


def test_check_locks_pass(tmp_path):
    good = tmp_path / "requirements.lock"
    good.write_text("# lock\nfastapi==1.0\nuvicorn==0.2\n", encoding="utf-8")
    with patch.object(R, "LOCKFILE", good), patch.object(R, "LOCKFILE_DEV", good):
        check_locks()


def test_check_locks_fails_on_bad_format(tmp_path):
    bad = tmp_path / "requirements.lock"
    bad.write_text("fastapi\nsome-weird thing--another\n", encoding="utf-8")
    with patch.object(R, "LOCKFILE", bad), patch.object(R, "LOCKFILE_DEV", bad):
        with pytest.raises(RuntimeError, match="unparseable pins"):
            check_locks()


def test_lockfile_format_regex():
    pattern = re.compile(r"^[a-zA-Z0-9._-]+==[^=]+$")
    assert pattern.match("fastapi==0.115.0")
    assert pattern.match("typing_extensions==4.15.0")
    assert not pattern.match("fastapi>=0.115")
    assert not pattern.match("")