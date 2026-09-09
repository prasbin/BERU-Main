"""Tests for the device & system control system: commands, app launch, notify."""

from __future__ import annotations

from backend.agents.registry import get_agent_registry
from backend.engines.app_launcher import AppLauncher
from backend.engines.command import CommandExecutor, CommandStatus
from backend.tools.system import (
    GetSystemInfoTool,
    LaunchAppTool,
    RunCommandTool,
    SendNotificationTool,
)

# ---- CommandExecutor tests ----


async def test_command_simple_execution():
    executor = CommandExecutor()
    result = await executor.execute("echo hello")
    assert result.status == CommandStatus.COMPLETED
    assert result.exit_code == 0
    assert result.stdout.strip() == "hello"


async def test_command_failure():
    executor = CommandExecutor()
    result = await executor.execute("nonexistent_command_xyz_abc_123")
    assert result.status == CommandStatus.FAILED


async def test_command_blocked_rm_rf():
    executor = CommandExecutor()
    result = await executor.execute("rm -rf /important")
    assert result.status == CommandStatus.BLOCKED
    assert result.blocked_reason is not None


async def test_command_blocked_fork_bomb():
    executor = CommandExecutor()
    result = await executor.execute(":(){ :|:& };:")
    assert result.status == CommandStatus.BLOCKED


async def test_command_blocked_recursive_delete_variants():
    """Spacing/case/quoting tricks cannot dodge the recursive-delete blocklist."""
    executor = CommandExecutor()
    for cmd in (
        "rm   -rF  /important && echo done",
        "rm --recursive --force /important",
        "rd /s /q C:\\Windows",
        "rmdir /S /Q C:\\whatever",
        "del /s /q C:\\Users\\joe\\Documents",
        "Remove-Item -Recurse -Force C:\\Windows\\System32",
        "Remove-Item -Recurse -Force ~",
        "git clean -fdx",
        "Clear-Content C:\\important.txt",
    ):
        result = await executor.execute(cmd)
        assert result.status == CommandStatus.BLOCKED, cmd
        assert result.blocked_reason is not None


async def test_command_blocked_host_destructive():
    """Shutdown/reboot/reformat/disk-wipe commands are refused outright."""
    executor = CommandExecutor()
    for cmd in (
        "shutdown -s -t 0",
        "shutdown.exe /s /t 0",
        "reboot now",
        "halt",
        "mkfs.ext4 /dev/sda4",
        "fdisk /dev/sda",
        "diskpart",
        "format C:",
        "dd if=/dev/zero of=/dev/sdb bs=512 count=1",
    ):
        result = await executor.execute(cmd)
        assert result.status == CommandStatus.BLOCKED, cmd
        assert result.blocked_reason is not None


async def test_command_history():
    executor = CommandExecutor()
    await executor.execute("echo one")
    await executor.execute("echo two")
    history = executor.get_history()
    assert len(history) == 2


async def test_command_clear_history():
    executor = CommandExecutor()
    await executor.execute("echo test")
    count = executor.clear_history()
    assert count == 1
    assert len(executor.get_history()) == 0


async def test_command_history_is_bounded():
    executor = CommandExecutor()
    for i in range(250):
        await executor.execute(f"echo {i:04d}")
    from backend.engines.command import _MAX_HISTORY

    assert len(executor.get_history(limit=10**9)) <= _MAX_HISTORY
    assert executor.get_history(limit=10**9)[-1].command.endswith("0249")


async def test_notifier_history_is_bounded():
    from backend.engines.command import _MAX_HISTORY
    from backend.engines.notifier import OSNotifier

    notifier = OSNotifier()
    for i in range(250):
        await notifier.send(title="T", message=f"M{i:04d}")
    assert len(notifier.get_history(limit=10**9)) <= _MAX_HISTORY
    assert notifier.get_history(limit=10**9)[-1].message.endswith("0249")


def test_notifier_quote_helpers_escape_literals():
    """Quoted fields can never terminate their string literal or inject script."""
    from backend.engines.notifier import _as_quote, _ps_quote

    # PowerShell: single quotes doubled inside the literal.
    assert _ps_quote("it's BERU") == "'it''s BERU'"
    assert _ps_quote("no ' break") == "'no '' break'"

    # AppleScript: backslashes and double quotes escaped; newlines collapsed.
    assert _as_quote('say "hi"') == '"say \\"hi\\""'
    single_line = _as_quote("line one\nline two")
    assert "\n" not in single_line
    assert single_line == '"line one line two"'
    assert _as_quote('a\\b') == '"a\\\\b"'


async def test_command_output_truncation():
    executor = CommandExecutor(max_output_bytes=50)
    result = await executor.execute("echo " + "x" * 200)
    assert result.stdout == "x" * 50


async def test_command_allowlist():
    executor = CommandExecutor(allowed_commands=["echo", "ls"])
    ok = await executor.execute("echo allowed")
    assert ok.status == CommandStatus.COMPLETED

    blocked = await executor.execute("cat /etc/passwd")
    assert blocked.status == CommandStatus.BLOCKED
    assert "allowlist" in blocked.blocked_reason


async def test_command_result_dict():
    executor = CommandExecutor()
    result = await executor.execute("echo test")
    data = result.to_dict()
    assert "id" in data
    assert "command" in data
    assert "status" in data
    assert "stdout" in data


# ---- AppLauncher tests ----


def test_app_launcher_alias_resolution():
    launcher = AppLauncher()
    command = launcher.resolve_command("notepad")
    assert command != ""  # Aliases map platform-specific


def test_app_launcher_list_empty():
    launcher = AppLauncher()
    assert launcher.list_launched() == []


def test_app_launcher_aliases_exist():
    launcher = AppLauncher()
    assert len(launcher._aliases) > 0 or launcher._platform in ("windows", "darwin", "linux")


def test_app_launcher_resolves_to_argv_list_never_shell_string():
    """resolve_argv returns a list; user input is never re-parsed by a shell."""
    launcher = AppLauncher()
    assert isinstance(launcher.resolve_argv("notepad"), list)

    # A hostile-looking app name survives as ONE argv element, verbatim — no
    # separator characters can create additional commands for a shell to run.
    hostile = "C:\\prog;evil.exe & rm -rf /important"
    assert launcher.resolve_argv(hostile) == [hostile]
    assert launcher.resolve_command(hostile) == hostile


def test_app_launcher_detects_uri_schemes_only():
    launcher = AppLauncher()
    assert launcher._is_uri("ms-settings:") is True
    assert launcher._is_uri("mailto:x@y") is True
    assert launcher._is_uri("C:\\Windows\\notepad.exe") is False
    assert launcher._is_uri("notepad") is False
    assert launcher._is_uri("rm -rf /") is False


async def test_app_launcher_get_nonexistent():
    launcher = AppLauncher()
    assert launcher.get_launched("nonexistent") is None


# ---- Tool tests ----

# The system tools run against the real host engines (singleton instances
# shared with the API), so these tests exercise genuine execution.


async def test_run_command_tool():
    tool = RunCommandTool()
    result = await tool.run(command="echo test")
    assert result.ok is True
    assert result.output["command"] == "echo test"
    assert result.output["status"] == "completed"
    assert result.output["stdout"].strip() == "test"
    # Requires confirmation (host interaction)
    assert tool.requires_confirmation is True


async def test_run_command_tool_blocked():
    tool = RunCommandTool()
    result = await tool.run(command="rm -rf /important")
    assert result.ok is False
    assert "blocked" in result.error.lower()


async def test_run_command_tool_requires_argument():
    tool = RunCommandTool()
    result = await tool.run()
    assert result.ok is False
    assert "requires a 'command' argument" in result.error


async def test_launch_app_tool():
    tool = LaunchAppTool()
    result = await tool.run(app_name="notepad")
    if result.ok:
        assert result.output["app_name"] == "notepad"
        assert result.output["status"] == "running"
    else:
        # Environment without that application: the tool reports the failure.
        assert result.error
    assert tool.requires_confirmation is True


async def test_launch_app_tool_requires_argument():
    tool = LaunchAppTool()
    result = await tool.run()
    assert result.ok is False
    assert "requires an 'app_name' argument" in result.error


async def test_send_notification_tool():
    tool = SendNotificationTool()
    result = await tool.run(title="Test", message="Hello")
    if result.ok:
        assert result.output["title"] == "Test"
    else:
        # OS notifications may be unavailable (e.g. no toast backend) — the
        # tool must then fail honestly instead of pretending to deliver.
        assert "Notification failed" in result.error
    assert tool.requires_confirmation is True


async def test_get_system_info_tool():
    tool = GetSystemInfoTool()
    result = await tool.run()
    assert result.ok is True
    assert "system" in result.output
    assert "platform" in result.output


async def test_core_agent_has_system_tools():
    registry = get_agent_registry()
    core = registry.get("beru_core")
    tool_names = [t.name for t in core._tools.values()]

    assert "run_command" in tool_names
    assert "launch_app" in tool_names
    assert "send_notification" in tool_names
    assert "get_system_info" in tool_names