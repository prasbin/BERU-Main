"""
Windows / macOS / Linux startup registration for BERU headless daemon (--daemon).
"""
from __future__ import annotations

import json
import platform
import subprocess
import sys
from pathlib import Path


TASK_NAME = "BERU_Daemon"
MAC_LABEL = "com.beru.daemon"
LINUX_CRON_MARKER = "# BERU_Daemon"


def get_base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent


def _main_script() -> Path:
    return get_base_dir() / "main.py"


def _python_launcher() -> Path:
    exe = Path(sys.executable)
    pythonw = exe.parent / "pythonw.exe"
    return pythonw if pythonw.exists() else exe


def _launch_command() -> tuple[Path, str, Path]:
    """Return (executable, arguments, working_directory)."""
    script = _main_script()
    workdir = script.parent
    launcher = _python_launcher()
    args = f'"{script}" --daemon'
    return launcher, args, workdir


def install_startup() -> str:
    system = platform.system()
    if system == "Windows":
        return _install_windows()
    if system == "Darwin":
        return _install_mac()
    return _install_linux()


def uninstall_startup() -> str:
    system = platform.system()
    if system == "Windows":
        return _uninstall_windows()
    if system == "Darwin":
        return _uninstall_mac()
    return _uninstall_linux()


def startup_status() -> dict:
    system = platform.system()
    if system == "Windows":
        return _status_windows()
    if system == "Darwin":
        return _status_mac()
    return _status_linux()


def _startup_folder_script() -> Path:
    startup = (
        Path.home()
        / "AppData"
        / "Roaming"
        / "Microsoft"
        / "Windows"
        / "Start Menu"
        / "Programs"
        / "Startup"
        / "BERU_Daemon.bat"
    )
    return startup


def _install_windows_startup_folder() -> str:
    launcher, args, workdir = _launch_command()
    bat_path = _startup_folder_script()
    bat_path.parent.mkdir(parents=True, exist_ok=True)
    bat_path.write_text(
        f'@echo off\r\n'
        f'cd /d "{workdir}"\r\n'
        f'timeout /t 45 /nobreak >nul\r\n'
        f'start "" /B "{launcher}" {args}\r\n',
        encoding="utf-8",
    )
    return (
        "BERU daemon added to your Windows Startup folder "
        f"({bat_path.name}). It launches ~45s after sign-in."
    )


def _uninstall_windows_startup_folder() -> bool:
    bat_path = _startup_folder_script()
    if bat_path.exists():
        bat_path.unlink()
        return True
    return False


def _install_windows() -> str:
    launcher, args, workdir = _launch_command()
    xml_path = Path.home() / ".beru" / "beru_daemon_task.xml"
    xml_path.parent.mkdir(parents=True, exist_ok=True)

    xml_content = f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.4" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>BERU Shadow Army background daemon (headless scheduler + command inbox)</Description>
    <Author>BERU</Author>
  </RegistrationInfo>
  <Triggers>
    <LogonTrigger>
      <Enabled>true</Enabled>
      <Delay>PT45S</Delay>
    </LogonTrigger>
  </Triggers>
  <Actions>
    <Exec>
      <Command>{launcher}</Command>
      <Arguments>{args}</Arguments>
      <WorkingDirectory>{workdir}</WorkingDirectory>
    </Exec>
  </Actions>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowHardTerminate>true</AllowHardTerminate>
    <StartWhenAvailable>true</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
    <RestartOnFailure>
      <Interval>PT5M</Interval>
      <Count>3</Count>
    </RestartOnFailure>
    <Enabled>true</Enabled>
  </Settings>
  <Principals>
    <Principal id="Author">
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
</Task>"""

    xml_path.write_text(xml_content, encoding="utf-16")
    subprocess.run(["schtasks", "/Delete", "/TN", TASK_NAME, "/F"], capture_output=True)

    result = subprocess.run(
        ["schtasks", "/Create", "/TN", TASK_NAME, "/XML", str(xml_path), "/F"],
        capture_output=True,
        text=True,
    )
    if result.returncode == 0:
        return (
            f"BERU daemon registered at Windows logon (task: {TASK_NAME}). "
            f"Starts ~45s after you sign in. Reboot or run: schtasks /Run /TN {TASK_NAME}"
        )

    # Fallback: no admin required
    folder_msg = _install_windows_startup_folder()
    return (
        f"Task Scheduler registration failed ({result.stderr.strip() or 'access denied'}). "
        f"Used Startup folder instead. {folder_msg}"
    )


def _uninstall_windows() -> str:
    removed_task = False
    result = subprocess.run(
        ["schtasks", "/Delete", "/TN", TASK_NAME, "/F"],
        capture_output=True,
        text=True,
    )
    if result.returncode == 0:
        removed_task = True

    removed_folder = _uninstall_windows_startup_folder()
    if removed_task and removed_folder:
        return f"Removed Task Scheduler entry and Startup folder script for BERU."
    if removed_task:
        return f"Removed Windows startup task '{TASK_NAME}'."
    if removed_folder:
        return "Removed BERU daemon from Windows Startup folder."
    return f"No startup registration found ({TASK_NAME} / Startup folder)."


def _status_windows() -> dict:
    result = subprocess.run(
        ["schtasks", "/Query", "/TN", TASK_NAME, "/FO", "LIST", "/V"],
        capture_output=True,
        text=True,
    )
    installed = result.returncode == 0
    startup_bat = _startup_folder_script()
    info = {
        "platform": "windows",
        "task_name": TASK_NAME,
        "task_scheduler_installed": installed,
        "startup_folder_installed": startup_bat.exists(),
        "startup_folder_script": str(startup_bat),
    }
    if installed:
        for line in result.stdout.splitlines():
            low = line.strip().lower()
            if low.startswith("status:"):
                info["task_status"] = line.split(":", 1)[1].strip()
            if low.startswith("next run time:"):
                info["next_run"] = line.split(":", 1)[1].strip()
            if low.startswith("task to run:"):
                info["command"] = line.split(":", 1)[1].strip()
    launcher, args, workdir = _launch_command()
    info["launcher"] = str(launcher)
    info["arguments"] = args
    info["working_directory"] = str(workdir)
    info["installed"] = installed or startup_bat.exists()
    return info


def _install_mac() -> str:
    plist_dir = Path.home() / "Library" / "LaunchAgents"
    plist_dir.mkdir(parents=True, exist_ok=True)
    plist_path = plist_dir / f"{MAC_LABEL}.plist"
    launcher, args, workdir = _launch_command()
    arg_list = [str(launcher)] + args.split()
    plist_content = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>{MAC_LABEL}</string>
  <key>ProgramArguments</key>
  <array>
    {''.join(f'<string>{a}</string>' for a in arg_list)}
  </array>
  <key>WorkingDirectory</key><string>{workdir}</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>{Path.home() / '.beru' / 'daemon.log'}</string>
  <key>StandardErrorPath</key><string>{Path.home() / '.beru' / 'daemon.err'}</string>
</dict></plist>"""
    plist_path.write_text(plist_content, encoding="utf-8")
    subprocess.run(["launchctl", "unload", str(plist_path)], capture_output=True)
    result = subprocess.run(["launchctl", "load", str(plist_path)], capture_output=True, text=True)
    if result.returncode != 0:
        return f"Startup registration failed: {result.stderr.strip()}"
    return f"BERU daemon registered via launchd ({MAC_LABEL})."


def _uninstall_mac() -> str:
    plist_path = Path.home() / "Library" / "LaunchAgents" / f"{MAC_LABEL}.plist"
    if not plist_path.exists():
        return f"No startup agent found ({MAC_LABEL})."
    subprocess.run(["launchctl", "unload", str(plist_path)], capture_output=True)
    plist_path.unlink(missing_ok=True)
    return f"Removed macOS startup agent '{MAC_LABEL}'."


def _status_mac() -> dict:
    plist_path = Path.home() / "Library" / "LaunchAgents" / f"{MAC_LABEL}.plist"
    return {
        "platform": "darwin",
        "label": MAC_LABEL,
        "installed": plist_path.exists(),
        "plist": str(plist_path),
    }


def _install_linux() -> str:
    launcher, args, workdir = _launch_command()
    cron_entry = (
        f"@reboot sleep 45 && cd {workdir} && {launcher} {args} "
        f">> {Path.home() / '.beru' / 'daemon.log'} 2>&1 {LINUX_CRON_MARKER}"
    )
    existing = subprocess.run(["crontab", "-l"], capture_output=True, text=True)
    lines = [
        ln for ln in existing.stdout.splitlines()
        if LINUX_CRON_MARKER not in ln and "--daemon" not in ln
    ]
    lines.append(cron_entry)
    proc = subprocess.run(["crontab", "-"], input="\n".join(lines) + "\n", text=True, capture_output=True)
    if proc.returncode != 0:
        return f"Startup registration failed: {proc.stderr.strip()}"
    return "BERU daemon registered in user crontab (@reboot)."


def _uninstall_linux() -> str:
    existing = subprocess.run(["crontab", "-l"], capture_output=True, text=True)
    if existing.returncode != 0:
        return "No user crontab found."
    lines = [ln for ln in existing.stdout.splitlines() if LINUX_CRON_MARKER not in ln]
    if len(lines) == len(existing.stdout.splitlines()):
        return "No BERU daemon crontab entry found."
    proc = subprocess.run(["crontab", "-"], input="\n".join(lines) + "\n", text=True, capture_output=True)
    if proc.returncode == 0:
        return "Removed BERU daemon crontab entry."
    return f"Failed to update crontab: {proc.stderr.strip()}"


def _status_linux() -> dict:
    existing = subprocess.run(["crontab", "-l"], capture_output=True, text=True)
    installed = LINUX_CRON_MARKER in existing.stdout if existing.returncode == 0 else False
    return {"platform": "linux", "installed": installed, "marker": LINUX_CRON_MARKER}


def format_status_report(extra: dict | None = None) -> str:
    info = startup_status()
    if extra:
        info.update(extra)
    return json.dumps(info, indent=2, ensure_ascii=False)
