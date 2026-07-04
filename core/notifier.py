"""
Local notifications — desktop toasts and console output (no Slack/GitHub).
"""
from __future__ import annotations

import platform
import subprocess
import sys


def notify(title: str, message: str, *, timeout: int = 12) -> None:
    title = title[:64]
    message = message[:240]
    print(f"[BERU] NOTIFY {title}: {message}")

    system = platform.system()
    try:
        if system == "Windows":
            from win10toast import ToastNotifier
            ToastNotifier().show_toast(title, message, duration=timeout, threaded=True)
        elif system == "Darwin":
            safe = message.replace('"', '\\"')
            subprocess.Popen(
                ["osascript", "-e", f'display notification "{safe}" with title "{title}"'],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        else:
            subprocess.Popen(
                ["notify-send", title, message],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
    except Exception as e:
        print(f"[BERU] ⚠️ Notification failed: {e}", file=sys.stderr)
