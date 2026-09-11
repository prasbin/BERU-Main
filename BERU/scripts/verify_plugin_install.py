"""Stage 5.3 gate: install a sample tool from a wheel in a clean test env.

Turns the roadmap gate into a repeatable command:

1. Build BERU's wheel (exercises the new ``[build-system]`` / setuptools config).
2. Build a sample third-party tool plugin wheel (``beru-sample-tool``).
3. Create a fresh virtual environment and ``pip install`` both wheels.
4. In that clean env, discover plugins via the ``beru.tools`` entry-point
   group and run the installed sample tool end to end.

The check itself exercises the real discovery path: it never imports from the
source checkout — everything comes from the installed wheels.

Usage::

    python -m scripts.verify_plugin_install [--keep]

`--keep` leaves the temporary build/venv directory in place and prints its
path. Exit code is 0 only when every phase passes.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "scripts" / "plugin_fixture" / "beru_sample_tool"

# Directories that should never enter a wheel build.
_IGNORES = shutil.ignore_patterns(
    ".git",
    ".venv",
    "venv",
    "build",
    "dist",
    "node_modules",
    "__pycache__",
    ".pytest_cache",
    "*.egg-info",
    "frontend",
    "tests",
    "docs",
    ".env",
)

_CHECK_CODE = """
import asyncio
from importlib import metadata
from backend.tools.registry import get_tool_registry

# BERU's own provider entry points ship in the installed wheel metadata.
provider_eps = list(metadata.entry_points().select(group="beru.stt_providers"))
assert len(provider_eps) == 1, provider_eps

registry = get_tool_registry()
names = [tool.name for tool in registry.list()]
assert "greeting" in names, names

tool = registry.get("greeting")
result = asyncio.run(tool.run(who="gate"))
assert result.ok, result
assert result.output == {"greeting": "Hello, gate!"}, result
print("discovered tools:", sorted(names))
print("sample plugin result:", result.output)
print("beru.stt_providers entry points:", [ep.name for ep in provider_eps])
print("GATE OK")
"""


def _log(msg: str) -> None:
    print(f"[plugin-gate] {msg}")


def _wheel_python(venv: Path) -> Path:
    return venv / ("Scripts" if sys.platform == "win32" else "bin") / "python"


def _build_wheel(src: Path, wheels: Path) -> Path:
    """Build a wheel in a copy of ``src``; return the built ``.whl`` path."""
    _log(f"building wheel for {src.name} ...")
    result = subprocess.run(
        [sys.executable, "-m", "pip", "wheel", "--no-deps", "-w", str(wheels), "."],
        cwd=str(src),
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"wheel build failed for {src}:\n{result.stdout}\n{result.stderr}")
    wheels_found = sorted(wheels.glob("*.whl"))
    if not wheels_found:
        raise RuntimeError(f"no wheel produced for {src}")
    _log(f"built {wheels_found[-1].name}")
    return wheels_found[-1]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--keep", action="store_true", help="Keep the temporary build directory."
    )
    args = parser.parse_args(argv)

    workdir = Path(tempfile.mkdtemp(prefix="beru-plugin-gate-"))
    wheels = workdir / "wheels"
    wheels.mkdir()
    build_source = workdir / "beru-src"
    fixture_source = workdir / "plugin-src"
    venv = workdir / "venv"

    try:
        _log(f"working dir: {workdir}")

        # Offline-safe copy of the source tree so the wheel build never leaves
        # egg-info/build artifacts in the repository.
        shutil.copytree(ROOT, build_source, ignore=_IGNORES)
        shutil.copytree(FIXTURE, fixture_source, ignore=_IGNORES)
        beru_wheel = _build_wheel(build_source, wheels)
        plugin_wheel = _build_wheel(fixture_source, wheels)

        _log("creating fresh virtual environment ...")
        subprocess.run(
            [sys.executable, "-m", "venv", str(venv)],
            check=True,
            capture_output=True,
            text=True,
        )

        venv_python = _wheel_python(venv)
        _log("installing wheels (beru + sample plugin) ...")
        install = subprocess.run(
            [str(venv_python), "-m", "pip", "install", str(beru_wheel), str(plugin_wheel)],
            capture_output=True,
            text=True,
        )
        if install.returncode != 0:
            raise RuntimeError(f"pip install failed:\n{install.stdout}\n{install.stderr}")

        _log("running clean-env discovery check ...")
        check = subprocess.run(
            [str(venv_python), "-c", _CHECK_CODE],
            capture_output=True,
            text=True,
        )
        if check.returncode != 0:
            raise RuntimeError(f"gate check failed:\n{check.stdout}\n{check.stderr}")

        print("\n--- clean-env check output ---")
        print(check.stdout, end="")
        print("-----------------------------")
    except RuntimeError as exc:
        print(f"[plugin-gate] FAILED: {exc}")
        if args.keep:
            print(f"[plugin-gate] artifacts kept at: {workdir}")
        return 1
    else:
        print("[plugin-gate] Gate PASSED: sample tool installed from a wheel "
              "and discovered in a clean environment.")
    finally:
        if not args.keep:
            shutil.rmtree(workdir, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())