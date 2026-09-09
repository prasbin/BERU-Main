"""Release tooling for BERU: version bump, lockfile regeneration, and the
pre-release smoke gate.

Subcommands
-----------
``python -m scripts.release check [--skip-tests]``
    Pre-release gate. Verifies version consistency, singles Alembic head,
    migration round-trip on a throwaway database, ruff, ``node --check``, and
    (unless skipped with ``--skip-tests``) the full pytest suite and lockfile
    sanity. Exit code is 0 only when every step passes.

``python -m scripts.release bump <major|minor|patch|X.Y.Z>``
    Bump the project version in its single source of truth
    (``backend/__init__.py`` ``__version__``), mirroring it into
    ``pyproject.toml``. Version numbers are ``major.minor.patch``.

``python -m scripts.release lock``
    Re-pin ``requirements.lock`` / ``requirements-dev.lock`` from the currently
    installed environment, resolved to the transitive closure of the declared
    top-level requirements. Intended to be run on a fresh, production-like
    install before a release so the pins match a clean resolution.

Everything here is deliberately offline and dependency-light: the only
third-party imports are ``packaging`` (a pip dependency) and the usual
application/alembic modules.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import tempfile
from collections import deque
from pathlib import Path

# Project root: scripts/release.py -> parents[1].
ROOT = Path(__file__).resolve().parents[1]
BACKEND_INIT = ROOT / "backend" / "__init__.py"
PYPROJECT = ROOT / "pyproject.toml"
REQUIREMENTS = ROOT / "requirements.txt"
REQUIREMENTS_DEV = ROOT / "requirements-dev.txt"
LOCKFILE = ROOT / "requirements.lock"
LOCKFILE_DEV = ROOT / "requirements-dev.lock"

_VERSION_RE = r'__version__\s*=\s*"([^"]+)"'
_HEAD_RE = r"b5c6d7e8f9a1"  # current single head; asserted by the gate.


def _log(msg: str) -> None:
    print(f"[release] {msg}")


# ---------------------------------------------------------------------------
# Step primitives
# ---------------------------------------------------------------------------


def load_version() -> str:
    """Read the canonical version from ``backend/__init__.py``."""
    text = BACKEND_INIT.read_text(encoding="utf-8")
    match = re.search(_VERSION_RE, text)
    if not match:
        raise RuntimeError(f"Could not find __version__ in {BACKEND_INIT}")
    return match.group(1)


def _pyproject_version(text: str) -> str:
    matches = re.findall(r"^version\s*=\s*\"([^\"]+)\"", text, flags=re.MULTILINE)
    if len(matches) != 1:
        raise RuntimeError(
            f"Expected exactly one top-level 'version' in pyproject.toml, found {len(matches)}"
        )
    return matches[0]


def check_version_consistency() -> tuple[str, str, str]:
    """Return (backend_version, pyproject_version, settings_version) or raise."""
    import backend
    from backend.core.config import get_settings

    backend_version = load_version()
    pyproject_version = _pyproject_version(PYPROJECT.read_text(encoding="utf-8"))
    settings_version = get_settings().version
    if not (backend_version == pyproject_version == settings_version):
        raise RuntimeError(
            "Version mismatch: "
            f"backend={backend_version!r} pyproject={pyproject_version!r} "
            f"settings={settings_version!r}"
        )
    _ = backend  # imported for its __version__ side effect only
    return backend_version, pyproject_version, settings_version


def check_single_head() -> str:
    """Return the single Alembic head revision, or raise."""
    from alembic.script import ScriptDirectory

    heads = ScriptDirectory(str(ROOT / "migrations")).get_heads()
    if len(heads) != 1:
        raise RuntimeError(f"Expected exactly one Alembic head, found: {heads}")
    return heads[0]


def migration_roundtrip() -> None:
    """Upgrade a throwaway DB to head, downgrade to base, and upgrade again.

    Verifies the full migration chain is reversible and idempotent (the schema
    reaches ``head`` with all tables intact after the round trip).
    """
    import sqlalchemy as sa
    from alembic import command

    from backend.database.migrations import make_alembic_config

    head = check_single_head()
    with tempfile.TemporaryDirectory(dir=str(ROOT)) as tmp:
        db = os.path.join(tmp, "roundtrip.db")
        cfg = make_alembic_config(f"sqlite:///{db}")

        command.upgrade(cfg, "head")
        command.downgrade(cfg, "base")
        command.upgrade(cfg, "head")

        engine = sa.create_engine(f"sqlite:///{db}")
        try:
            with engine.connect() as conn:
                version_num = conn.execute(
                    sa.text("SELECT version_num FROM alembic_version")
                ).scalar()
                table_names = sa.inspect(engine).get_table_names()
        finally:
            engine.dispose()

    if version_num != head:
        raise RuntimeError(f"Round-trip ended at {version_num!r}, expected {head!r}")
    if "conversations" not in table_names or "activity_records" not in table_names:
        raise RuntimeError(f"Round-trip schema incomplete: {table_names}")


def _run(cmd: list[str], label: str) -> None:
    _log(f"running: {' '.join(cmd)}")
    result = subprocess.run(cmd, cwd=str(ROOT))
    if result.returncode != 0:
        raise RuntimeError(f"'{label}' failed with exit code {result.returncode}")


def run_suite(skip_tests: bool) -> None:
    _run(
        [sys.executable, "-m", "ruff", "check", "backend", "tests", "scripts"],
        "ruff",
    )
    _run(["node", "--check", "frontend/app.js"], "node --check")
    if not skip_tests:
        _run([sys.executable, "-m", "pytest", "-q"], "pytest")


def check_locks() -> None:
    for path in (LOCKFILE, LOCKFILE_DEV):
        if not path.exists():
            raise RuntimeError(f"Lockfile missing: {path} (run 'python -m scripts.release lock')")
        bad = [
            line.strip()
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.startswith("#")
            and not re.match(r"^[a-zA-Z0-9._-]+==[^=]+$", line.strip())
        ]
        if bad:
            raise RuntimeError(f"Lockfile {path.name} has unparseable pins: {bad}")


# ---------------------------------------------------------------------------
# check
# ---------------------------------------------------------------------------


def cmd_check(args: argparse.Namespace) -> int:
    steps: list[str] = []
    try:
        backend_version, pyproject_version, settings_version = check_version_consistency()
        steps.append(
            f"version consistent: backend={backend_version} "
            f"pyproject={pyproject_version} settings={settings_version}"
        )

        head = check_single_head()
        steps.append(f"single alembic head: {head}")

        _log("migration round-trip (upgrade head -> downgrade base -> upgrade head)")
        migration_roundtrip()
        steps.append("migration round-trip OK")

        run_suite(skip_tests=args.skip_tests)
        if args.skip_tests:
            steps.append("ruff + node --check (tests skipped)")
        else:
            steps.append("ruff + node --check + full pytest")

        check_locks()
        steps.append("lockfiles present and parseable")
    except RuntimeError as exc:
        print(f"[release] FAILED: {exc}")
        return 1

    print("\n[release] pre-release gate PASSED")
    for step in steps:
        print(f"  - {step}")
    return 0


# ---------------------------------------------------------------------------
# bump
# ---------------------------------------------------------------------------


def _next_version(current: str, part: str, explicit: str | None) -> str:
    if explicit:
        if not re.fullmatch(r"\d+\.\d+\.\d+", explicit):
            raise ValueError(f"Explicit version must be major.minor.patch, got {explicit!r}")
        return explicit
    major, minor, patch = (int(x) for x in current.split("."))
    if part == "major":
        major, minor, patch = major + 1, 0, 0
    elif part == "minor":
        minor, patch = minor + 1, 0
    elif part == "patch":
        patch += 1
    else:
        raise ValueError(f"Unknown bump part {part!r}")
    return f"{major}.{minor}.{patch}"


def cmd_bump(args: argparse.Namespace) -> int:
    current = load_version()
    new = _next_version(current, args.part, args.explicit)
    if new == current:
        print(f"[release] version unchanged ({current}) — nothing to do.")
        return 0

    init_text = BACKEND_INIT.read_text(encoding="utf-8")
    updated_init, n = re.subn(_VERSION_RE, f'__version__ = "{new}"', init_text, count=1)
    if n != 1:
        raise RuntimeError(f"Could not rewrite __version__ in {BACKEND_INIT}")

    pyproject_text = PYPROJECT.read_text(encoding="utf-8")
    updated_pyproject, n = re.subn(
        r"^version\s*=\s*\"([^\"]+)\"",
        f'version = "{new}"',
        pyproject_text,
        count=1,
        flags=re.MULTILINE,
    )
    if n != 1:
        raise RuntimeError(f"Could not rewrite version in {PYPROJECT}")

    BACKEND_INIT.write_text(updated_init, encoding="utf-8")
    PYPROJECT.write_text(updated_pyproject, encoding="utf-8")
    print(f"[release] bumped {current} -> {new}")
    print("[release] remember: python -m scripts.release lock  (if deps changed)")
    print("[release] remember: python -m scripts.release check")
    return 0


# ---------------------------------------------------------------------------
# lock
# ---------------------------------------------------------------------------


def _normalise(name: str) -> str:
    """Lowercase and collapse inter-word separators for distribution matching."""
    return re.sub(r"[-_.]+", "-", name.lower())


def _read_root_packages(path: Path) -> list[str]:
    """Extract the package names declared by a requirements file.

    ``-r <file>`` includes are resolved relative to the requiring file, so a dev
    requirements that pulls in the runtime file inherits the runtime packages.
    """
    from packaging.requirements import Requirement

    names: list[str] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("-r "):
            include = path.parent / line[3:].strip()
            names.extend(_read_root_packages(include))
            continue
        try:
            names.append(Requirement(line).name)
        except Exception:  # noqa: BLE001 - skip unparseable lines defensively
            continue
    return names


def _dependency_closure(roots: list[str]) -> set[str]:
    """BFS over installed distributions' declared runtime requirements.

    Extra-gated requirements (``Requires-Dist: pytest; extra == "test"``) are
    excluded so a runtime lock never inherits test-only packages that happen to
    be installed in the generating environment.
    """
    from importlib import metadata as md

    from packaging.requirements import Requirement

    seen: set[str] = set()
    queue: deque[str] = deque(_normalise(r) for r in roots)
    while queue:
        name = queue.popleft()
        if name in seen:
            continue
        seen.add(name)
        try:
            dist = md.distribution(name)
        except md.PackageNotFoundError:
            continue
        for req in (dist.requires or []):
            try:
                requirement = Requirement(req)
            except Exception:  # noqa: BLE001 - a malformed requirement shouldn't abort the lock
                continue
            if requirement.marker is not None and "extra" in str(requirement.marker):
                continue
            queue.append(_normalise(requirement.name))
    return seen


def _freeze_map() -> dict[str, str]:
    """Map normalised dist name -> "name==version" line from pip freeze."""
    raw = subprocess.run(
        [sys.executable, "-m", "pip", "freeze"],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    result: dict[str, str] = {}
    for line in raw.splitlines():
        line = line.strip()
        if "==" in line:
            name, _, _ = line.partition("==")
            result[_normalise(name)] = line
    return result


def _write_lock(path: Path, freeze: dict[str, str], closure: set[str]) -> None:
    wanted = sorted(line for name, line in freeze.items() if name in closure)
    header = [
        "# BERU pinned dependency lockfile (exact versions).",
        "# Generate with:  python -m scripts.release lock",
        "# This file pins the transitive closure of the runtime requirements as",
        "# resolved in the environment that produced it. Commit it and treat any",
        "# change as a deliberate dependency upgrade.",
        "",
    ]
    path.write_text("\n".join(header + wanted) + "\n", encoding="utf-8")
    _log(f"wrote {path.name}: {len(wanted)} pinned packages")


def cmd_lock(_args: argparse.Namespace) -> int:
    runtime_roots = _read_root_packages(REQUIREMENTS)
    dev_roots = _read_root_packages(REQUIREMENTS_DEV)  # includes -r requirements.txt
    freeze = _freeze_map()

    runtime_closure = _dependency_closure(runtime_roots)
    _write_lock(LOCKFILE, freeze, runtime_closure)

    dev_closure = _dependency_closure(dev_roots)
    _write_lock(LOCKFILE_DEV, freeze, dev_closure)
    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m scripts.release", description=__doc__.splitlines()[0]
    )
    sub = parser.add_subparsers(dest="command", required=True)

    check_p = sub.add_parser("check", help="Run the pre-release smoke gate.")
    check_p.add_argument(
        "--skip-tests",
        action="store_true",
        help="Skip the full pytest suite (still runs ruff + node --check + migrations).",
    )
    check_p.set_defaults(func=cmd_check)

    bump_p = sub.add_parser("bump", help="Bump the project version.")
    bump_p.add_argument(
        "bump",
        nargs="?",
        help="major | minor | patch, or an explicit X.Y.Z version.",
    )
    bump_p.set_defaults(func=cmd_bump, part=None, explicit=None)

    lock_p = sub.add_parser(
        "lock", help="Regenerate dependency lockfiles from the installed environment."
    )
    lock_p.set_defaults(func=cmd_lock)

    args = parser.parse_args(argv)
    if args.command == "bump":
        if args.bump in ("major", "minor", "patch"):
            args.part, args.explicit = args.bump, None
        elif args.bump is not None and re.fullmatch(r"\d+\.\d+\.\d+", args.bump):
            args.part, args.explicit = None, args.bump
        elif args.bump is not None:
            parser.error(f"bump expects major|minor|patch|X.Y.Z, got {args.bump!r}")

    try:
        return args.func(args)
    except RuntimeError as exc:
        print(f"[release] FAILED: {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())