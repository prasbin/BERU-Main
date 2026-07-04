import os
import sys
import time
import ast
import traceback
import importlib
import threading
from pathlib import Path

# In-memory backup of last valid file content
# filepath -> file_content (string)
_good_content = {}
_file_mtimes = {}
_watching = True
_watcher_thread = None

# A callback function for when UI needs refresh
_ui_refresh_callback = None


def init_watcher(base_dir: Path, ui_refresh_cb=None):
    global _watcher_thread, _ui_refresh_callback
    _ui_refresh_callback = ui_refresh_cb

    # Store initial valid states of all files in memory
    for folder in ["agents", "actions", "."]:
        folder_path = base_dir / folder
        if not folder_path.exists():
            continue
        for p in folder_path.glob("*.py"):
            try:
                content = p.read_text(encoding="utf-8")
                # Validate syntax on start
                ast.parse(content)
                key = str(p.resolve())
                _good_content[key] = content
                _file_mtimes[key] = p.stat().st_mtime
            except Exception as e:
                print(f"[HotReload] ⚠️ Initial validation failed for {p.name}: {e}")

    _watcher_thread = threading.Thread(
        target=_watch_loop, args=(base_dir,), daemon=True, name="BeruHotWatcher"
    )
    _watcher_thread.start()


def stop_watcher():
    global _watching
    _watching = False


def _watch_loop(base_dir: Path):
    print("[HotReload] Background file watcher started.")
    while _watching:
        time.sleep(1.0)  # Check every second
        for folder in ["agents", "actions", "."]:
            folder_path = base_dir / folder
            if not folder_path.exists():
                continue
            for p in folder_path.glob("*.py"):
                file_key = str(p.resolve())
                try:
                    mtime = p.stat().st_mtime
                    if file_key not in _file_mtimes:
                        # New file created!
                        _file_mtimes[file_key] = mtime
                        content = p.read_text(encoding="utf-8")
                        ast.parse(content)
                        _good_content[file_key] = content
                        print(f"[HotReload] New file watched: {p.name}")
                        _trigger_reload(p, base_dir)
                    elif mtime > _file_mtimes[file_key]:
                        # File modified!
                        _file_mtimes[file_key] = mtime
                        _handle_file_change(p, base_dir)
                except Exception:
                    pass


def _handle_file_change(filepath: Path, base_dir: Path):
    file_key = str(filepath.resolve())
    name = filepath.name
    print(f"[HotReload] Detected change in {name}")

    # 1. Read the new content
    try:
        new_content = filepath.read_text(encoding="utf-8")
    except Exception as e:
        print(f"[HotReload] Error reading {name}: {e}")
        return

    # 2. Syntax validation
    try:
        ast.parse(new_content)
    except Exception as e:
        print(f"[HotReload] ❌ Syntax Error in {name}: {e}. Rolling back...")
        _rollback(filepath, file_key)
        return

    # 3. Reload module dynamically and test import
    try:
        relative = filepath.relative_to(base_dir)
        module_parts = list(relative.parts)
        module_parts[-1] = filepath.stem  # strip .py
        
        # Don't try to fully reload main.py as it has the running loop, 
        # but we do check its syntax (done above) and keep it updated
        if name == "main.py":
            print(f"[HotReload] main.py modified & validated. Memory backup updated.")
            _good_content[file_key] = new_content
            return

        module_name = ".".join(module_parts)

        # Force re-import and reload
        if module_name in sys.modules:
            mod = sys.modules[module_name]
            importlib.reload(mod)
        else:
            mod = importlib.import_module(module_name)

        print(f"[HotReload] ✅ Module {module_name} successfully reloaded.")
        
        # Update good content backup
        _good_content[file_key] = new_content

        # 4. Trigger system-specific updates
        _trigger_reload(filepath, base_dir)

    except Exception as e:
        print(f"[HotReload] ❌ Runtime Error reloading {name}: {e}. Rolling back...")
        traceback.print_exc()
        _rollback(filepath, file_key)


def _rollback(filepath: Path, file_key: str):
    if file_key in _good_content:
        try:
            old_content = _good_content[file_key]
            filepath.write_text(old_content, encoding="utf-8")
            time.sleep(0.1)
            _file_mtimes[file_key] = filepath.stat().st_mtime
            print(f"[HotReload] 🛡️ Rolled back {filepath.name} to last known good state.")

            # Re-reload the rolled back module to restore state
            base_dir = filepath.parent.parent if filepath.parent.name in ("agents", "actions") else filepath.parent
            relative = filepath.relative_to(base_dir)
            module_parts = list(relative.parts)
            module_parts[-1] = filepath.stem
            module_name = ".".join(module_parts)
            if module_name in sys.modules:
                importlib.reload(sys.modules[module_name])
        except Exception as e:
            print(f"[HotReload] Rollback reload failed for {filepath.name}: {e}")


def _trigger_reload(filepath: Path, base_dir: Path):
    from core import registry
    
    # Reload registry caches
    registry.load_all()

    relative = filepath.relative_to(base_dir)
    parent_folder = relative.parts[0]

    if parent_folder == "agents":
        print(f"[HotReload] Shadow agent reloaded in registry.")
    elif parent_folder == "actions":
        print(f"[HotReload] Action tool reloaded in registry.")
    elif filepath.name == "ui.py":
        print(f"[HotReload] UI code changed. Triggering UI refresh...")
        if _ui_refresh_callback:
            _ui_refresh_callback()
