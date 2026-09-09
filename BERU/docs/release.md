# Release checklist

BERU's release tooling lives in `scripts/release.py` (run as
`python -m scripts.release`). It covers three jobs: **versioning**, **dependency
pinning**, and a **pre-release smoke gate**. Everything runs offline against the
repo; no CI is required.

## Versioning

The version has a single source of truth:

- `backend/__init__.py` → `__version__ = "0.1.0"` (canonical).
- `pyproject.toml` → `[project] version = "0.1.0"` (mirror, kept in sync by the
  bump command).
- `backend/core/config.py` → `Settings.version` reads `backend.__version__`.

Bump a release:

```bash
python -m scripts.release bump patch    # 0.1.0 -> 0.1.1
python -m scripts.release bump minor    # 0.1.1 -> 0.2.0
python -m scripts.release bump major    # 0.2.0 -> 1.0.0
python -m scripts.release bump 1.2.3    # explicit version
```

The bump rewrites `backend/__init__.py` and the single `version = "..."` in
`pyproject.toml` (it errors if either is missing or ambiguous). After a bump,
run the gate (`check` below) — the version-consistency step will fail if the two
files ever drift.

## Lockfiles

Dependencies are declared with lower bounds in `requirements.txt` (runtime) and
`requirements-dev.txt` (dev, `-r` the runtime file). For reproducible installs
the repo also pins exact versions:

- `requirements.lock` — transitive closure of the runtime requirements,
  resolved in the environment that generated it.
- `requirements-dev.lock` — runtime closure plus the dev/test tools
  (pytest, pytest-asyncio, ruff).

Regenerate them from a **fresh, production-like install** before a release:

```bash
pip install -r requirements-dev.txt
python -m scripts.release lock
```

`lock` walks the installed distributions' declared runtime requirements
(extra-gated `; extra == "..."` entries are excluded so test-only packages never
leak into the runtime lock) and writes `name==version` pins for that closure.
Treat any lockfile change as a deliberate dependency upgrade, and commit it.

## Pre-release gate

```bash
python -m scripts.release check            # full gate (slow: ~20-30 min)
python -m scripts.release check --skip-tests   # fast smoke (lint + migrations)
```

`check` exits 0 only when every step passes:

1. **Version consistency** — `backend/__init__.py`, `pyproject.toml`, and
   `Settings.version` all agree.
2. **Single Alembic head** — the migration chain has exactly one head
   (`b5c6d7e8f9a1`).
3. **Migration round-trip** — upgrade `head` → downgrade `base` → upgrade `head`
   on a throwaway SQLite DB, then assert the version and the expected tables
   survive.
4. **ruff** over `backend`, `tests`, and `scripts`.
5. **`node --check`** on the frontend.
6. **Full pytest** (skipped with `--skip-tests`).
7. **Lockfile sanity** — both lockfiles exist and every line is a well-formed
   `name==version` pin.

1–3 and 7 are run inline by the script; ruff, node, and pytest are invoked as
subprocesses. A green `check` is the signal that the release is shippable.

## Release checklist (run in order)

```bash
python -m scripts.release bump <part|X.Y.Z>   # 1. set the new version
python -m pytest tests/test_release.py -q     # 2. release-tooling tests
python -m scripts.release lock                # 3. re-pin if deps changed
python -m scripts.release check               # 4. full pre-release gate
```

Steps 1–3 modify the repo; commit `backend/__init__.py`, `pyproject.toml`,
`requirements*.lock`, and any docs/changelog updates together, then run step 4
on the clean tree before tagging.