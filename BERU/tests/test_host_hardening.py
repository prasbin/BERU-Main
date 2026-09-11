"""Stage 5.4 Host hardening (tool sandbox) tests.

Covers the guardrail gate — destructive commands are blocked in **every**
executor backend (host + container) — plus the least-privilege account
handling and per-tool credential scoping.
"""

from __future__ import annotations

import platform

import pytest

from backend.agents.base import BaseAgent
from backend.core.config import Settings
from backend.engines.command import CommandExecutor, CommandStatus, get_command_executor
from backend.engines.container import ContainerCommandExecutor
from backend.engines.llm.base import ToolCall
from backend.tools.base import Tool, ToolResult
from backend.tools.credentials import PERMITTED_CREDENTIAL_NAMES, scope_for_tool

# ---------------------------------------------------------------------------
# Guardrail gate: destructive commands are blocked in every backend
# ---------------------------------------------------------------------------

_DESTRUCTIVE_COMMANDS = (
    # Recursive + force deletes / wipes (spacing, case, quoting tricks).
    "rm -rf /important",
    "rm   -rF  /important && echo done",
    "rm --recursive --force /important",
    "rd /s /q C:\\Windows",
    "rmdir /S /Q C:\\whatever",
    "del /s /q C:\\Users\\joe\\Documents",
    "Remove-Item -Recurse -Force C:\\Windows\\System32",
    "Remove-Item -Recurse -Force ~",
    "git clean -fdx",
    "Clear-Content C:\\important.txt",
    # Fork bombs.
    ":(){ :|:& };:",
    # Host-destructive standalone commands.
    "shutdown -s -t 0",
    "shutdown.exe /s /t 0",
    "reboot now",
    "halt",
    "mkfs.ext4 /dev/sda4",
    "mkfs",
    "fdisk /dev/sda",
    "diskpart",
    "format C:",
    "dd if=/dev/zero of=/dev/sdb bs=512 count=1",
    "dd if=/dev/zero of=/dev/sda",
)


def _all_executor_backends() -> dict[str, CommandExecutor]:
    """One instance per backend (host subprocess + container via docker exec)."""
    return {
        "host": CommandExecutor(),
        "container": ContainerCommandExecutor(container="beru-test-sandbox"),
    }


@pytest.mark.parametrize("command", _DESTRUCTIVE_COMMANDS)
@pytest.mark.parametrize("backend", ["host", "container"])
async def test_destructive_commands_blocked_in_every_backend(command, backend):
    executor = _all_executor_backends()[backend]
    result = await executor.execute(command)
    assert result.status == CommandStatus.BLOCKED, (
        f"[{backend}] expected {command!r} to be blocked"
    )
    assert result.blocked_reason


async def test_fork_bomb_blocked_before_spawn_in_every_backend():
    for executor in _all_executor_backends().values():
        result = await executor.execute(":(){ :|:& };:")
        assert result.status == CommandStatus.BLOCKED
        # Blocked before the runner stage started — never spawned.
        assert result.started_at is None


async def test_allowlist_rejects_unlisted_command_in_container_backend():
    """Allowlisted-only container also blocks non-allowlisted input pre-spawn."""
    executor = ContainerCommandExecutor(
        "beru-test-sandbox", allowed_commands=["echo"]
    )
    blocked = await executor.execute("id")
    assert blocked.status == CommandStatus.BLOCKED
    assert "allowlist" in (blocked.blocked_reason or "")


# ---------------------------------------------------------------------------
# Container executor backend
# ---------------------------------------------------------------------------


def test_container_argv_forwards_workdir_and_explicit_env_only():
    executor = ContainerCommandExecutor("sandbox", shell=["sh", "-c"])
    argv = executor._build_shell_command(
        "echo hi", cwd="/work", env={"TOKEN": "x"}
    )
    assert argv == [
        "docker",
        "exec",
        "-i",
        "--workdir",
        "/work",
        "-e",
        "TOKEN=x",
        "sandbox",
        "sh",
        "-c",
        "echo hi",
    ]


def test_container_argv_no_env_means_no_env_flags():
    executor = ContainerCommandExecutor("sandbox", shell=["bash", "-lc"])
    argv = executor._build_shell_command("pwd", cwd=None)
    assert argv == [
        "docker",
        "exec",
        "-i",
        "sandbox",
        "bash",
        "-lc",
        "pwd",
    ]


def test_container_shell_defaults_are_two_tokens():
    executor = ContainerCommandExecutor("sandbox")
    assert len(executor._shell) == 2


async def test_container_timeout_records_timeout_status():
    """The shared runner handles timeouts identically across backends.

    Runs a command that is *allowed* by both guardrails against a container
    that (almost certainly) does not exist: docker exec fails fast, but the
    runner must classify that as a normal FAILED (never a crash, never a
    timelimit hang).
    """
    executor = ContainerCommandExecutor(
        "beru-no-such-sandbox-container", timeout_seconds=0.1
    )
    result = await executor.execute("echo hi")
    assert result.status in (CommandStatus.FAILED, CommandStatus.TIMEOUT)
    assert result.stderr or result.exit_code is not None


# ---------------------------------------------------------------------------
# Least-privilege service account for host subprocesses
# ---------------------------------------------------------------------------


async def test_windows_refuses_low_privilege_host_execution():
    if platform.system().lower() != "windows":
        pytest.skip("Windows-only behaviour")
    for kwargs in ({"user": "beru_svc"}, {"group": "beru_svc"}, {"user": "u", "group": "g"}):
        executor = CommandExecutor(**kwargs)
        result = await executor.execute("echo hi")
        assert result.status == CommandStatus.BLOCKED
        assert "not supported on Windows" in (result.blocked_reason or "")
        assert "container" in (result.blocked_reason or "")


@pytest.mark.skipif(
    platform.system().lower() == "windows", reason="POSIX-only subprocess kwargs"
)
async def test_posix_passes_least_privilege_account_to_subprocess(monkeypatch):
    captured: dict[str, object] = {}

    class _FakeProcess:
        returncode = 0

        async def communicate(self) -> tuple[bytes, bytes]:
            return b"ok", b""

        async def wait(self) -> None:
            return None

    async def _fake_exec(*args, **kwargs):  # noqa: ANN002, ANN003
        captured["args"] = args
        captured["kwargs"] = kwargs
        return _FakeProcess()

    monkeypatch.setattr(
        "backend.engines.command.asyncio.create_subprocess_exec", _fake_exec
    )
    executor = CommandExecutor(user="nobody", group="nogroup")
    result = await executor.execute("echo hi")
    assert result.status == CommandStatus.COMPLETED
    assert captured["args"] == ["bash", "-c", "echo hi"]
    assert captured["kwargs"]["user"] == "nobody"
    assert captured["kwargs"]["group"] == "nogroup"


# ---------------------------------------------------------------------------
# Executor factory (backend selection from settings)
# ---------------------------------------------------------------------------


def test_factory_defaults_to_host(monkeypatch):
    import backend.engines.command as cmd_module

    monkeypatch.setattr(
        cmd_module,
        "get_settings",
        lambda: Settings(
            BERU_COMMAND_EXECUTOR="host",
            BERU_COMMAND_RUN_USER="svc_beru",
            BERU_COMMAND_RUN_GROUP="svc_beru",
        ),
    )
    get_command_executor.cache_clear()
    try:
        executor = get_command_executor()
        assert isinstance(executor, CommandExecutor)
        assert not isinstance(executor, ContainerCommandExecutor)
        assert executor._user == "svc_beru"
        assert executor._group == "svc_beru"
    finally:
        get_command_executor.cache_clear()


def test_factory_selects_container_backend(monkeypatch):
    import backend.engines.command as cmd_module

    monkeypatch.setattr(
        cmd_module,
        "get_settings",
        lambda: Settings(
            BERU_COMMAND_EXECUTOR="container",
            BERU_COMMAND_CONTAINER="beru-sandbox",
        ),
    )
    get_command_executor.cache_clear()
    try:
        executor = get_command_executor()
        assert isinstance(executor, ContainerCommandExecutor)
        assert executor._container == "beru-sandbox"
    finally:
        get_command_executor.cache_clear()


def test_factory_falls_back_to_host_when_container_unset(monkeypatch):
    import backend.engines.command as cmd_module

    monkeypatch.setattr(
        cmd_module,
        "get_settings",
        lambda: Settings(BERU_COMMAND_EXECUTOR="container"),
    )
    get_command_executor.cache_clear()
    try:
        executor = get_command_executor()
        assert isinstance(executor, CommandExecutor)
        assert not isinstance(executor, ContainerCommandExecutor)
    finally:
        get_command_executor.cache_clear()


# ---------------------------------------------------------------------------
# Per-tool credential scoping
# ---------------------------------------------------------------------------


class CredentialProbeTool(Tool):
    name = "credential_probe"
    description = "Reports the injected credential scope."
    required_credentials = ["llm_api_key"]
    parameters = {"type": "object", "properties": {}, "required": []}

    async def run(self, **kwargs) -> ToolResult:
        return ToolResult.success(
            {
                "has": self.credentials.has("llm_api_key"),
                "value": self.credentials.get("llm_api_key"),
                "names": self.credentials.names(),
            }
        )


def test_scope_grants_declared_permitted_keys():
    settings = Settings(llm_api_key="sk-llm", BERU_EMBEDDING_API_KEY="sk-emb")
    scope = scope_for_tool(settings, ["llm_api_key", "embedding_api_key"])
    assert scope.names() == ["embedding_api_key", "llm_api_key"]
    assert scope.get("llm_api_key") == "sk-llm"
    assert scope.get("embedding_api_key") == "sk-emb"


def test_scope_never_grants_global_owner_key():
    """Tools request scoped keys — the global BERU_API_KEY is structurally out."""
    settings = Settings(BERU_API_KEY="owner-secret", llm_api_key="sk")
    scope = scope_for_tool(settings, ["api_key", "llm_api_key"])
    assert "api_key" not in PERMITTED_CREDENTIAL_NAMES
    assert "api_key" not in scope.names()
    assert scope.get("api_key") is None
    assert scope.get("llm_api_key") == "sk"


def test_scope_omits_unconfigured_empty_keys():
    assert scope_for_tool(Settings(), ["llm_api_key"]).empty()
    assert scope_for_tool(Settings(llm_api_key=""), ["llm_api_key"]).empty()


def test_scope_without_settings_is_empty():
    assert scope_for_tool(None, ["llm_api_key"]).empty()
    assert scope_for_tool(Settings(), []).empty()


def test_scope_repr_and_str_never_expose_values():
    secret = "super-secret-value-xyz"
    scope = scope_for_tool(Settings(llm_api_key=secret), ["llm_api_key"])
    assert secret not in repr(scope)
    assert secret not in str(scope)
    assert "llm_api_key" in repr(scope)
    # The scope is read-only: no reassignment and no in-place mutation.
    with pytest.raises(AttributeError):
        scope._values = {}  # type: ignore[misc]
    with pytest.raises(TypeError):
        scope._values["llm_api_key"] = "mutated"  # type: ignore[index]


async def test_agent_injects_scoped_credentials_with_settings():
    agent = BaseAgent()
    tool = CredentialProbeTool()
    agent.register_tool(tool)
    call = ToolCall(id="tc-1", name="credential_probe", arguments="{}")
    settings = Settings(llm_api_key="sk-injected")

    result = await agent.run_tool(
        call, confirm=True, agent_name="beru_core", settings=settings
    )
    assert result.ok
    assert result.output == {
        "has": True,
        "value": "sk-injected",
        "names": ["llm_api_key"],
    }
    assert tool.credentials.get("llm_api_key") == "sk-injected"
    assert "sk-injected" not in repr(tool.credentials)


async def test_agent_injects_empty_scope_without_settings():
    agent = BaseAgent()
    tool = CredentialProbeTool()
    agent.register_tool(tool)
    call = ToolCall(id="tc-2", name="credential_probe", arguments="{}")

    result = await agent.run_tool(
        call, confirm=True, agent_name="beru_core", settings=None
    )
    assert result.ok
    assert result.output == {"has": False, "value": None, "names": []}
    assert tool.credentials.empty()


async def test_confirmation_gated_tool_still_gets_scoped_credentials():
    """The explicit confirmation re-injection path also scopes credentials."""
    agent = BaseAgent()
    tool = CredentialProbeTool()
    agent.register_tool(tool)
    call = ToolCall(id="tc-3", name="credential_probe", arguments="{}")
    settings = Settings(llm_api_key="sk-confirmed")

    result = await agent.run_tool(call, confirm=True, agent_name="beru_core", settings=settings)
    assert result.ok and result.output["has"] is True
    scope = result.output["value"]
    assert scope == "sk-confirmed"


async def test_probe_tool_without_declared_credentials_gets_nothing():
    class NoCredsTool(Tool):
        name = "no_creds"
        description = "tool"
        parameters = {"type": "object", "properties": {}, "required": []}

        async def run(self, **kwargs) -> ToolResult:
            return ToolResult.success({"empty": self.credentials.empty()})

    agent = BaseAgent()
    tool = NoCredsTool()
    agent.register_tool(tool)
    settings = Settings(llm_api_key="sk-x", BERU_API_KEY="owner")
    result = await agent.run_tool(
        ToolCall(id="tc-4", name="no_creds", arguments="{}"),
        confirm=True,
        agent_name="beru_core",
        settings=settings,
    )
    assert result.ok
    assert result.output == {"empty": True}