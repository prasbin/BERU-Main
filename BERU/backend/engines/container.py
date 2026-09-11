"""Container command executor — runs commands inside a Docker container.

Shares the exact same guardrail layer (allowlist + pattern-blocklist) as the
host :class:`~backend.engines.command.CommandExecutor`, so the same battery
of destructive-command tests passes for every backend. The child process
lives inside a fixed container, isolating it from the host filesystem,
network, and credentials.

Configured via::

    BERU_COMMAND_EXECUTOR=container
    BERU_COMMAND_CONTAINER=<name-or-id>
"""

from __future__ import annotations

from backend.engines.command import CommandExecutor

#: Docker CLI entry point used to exec into the sandbox container.
_DOCKER = "docker"


class ContainerCommandExecutor(CommandExecutor):
    """Run commands in a container via ``docker exec``.

    The guardrail layer is inherited directly from
    :class:`CommandExecutor`: destructive and allowlist-rejected commands
    are blocked *before* anything is spawned, so the blocklist works even
    if the container is unavailable.
    """

    def __init__(
        self,
        container: str,
        *,
        shell: list[str] | None = None,
        allowed_commands: list[str] | None = None,
        blocked_commands: list[str] | None = None,
        timeout_seconds: float = 30,
        max_output_bytes: int = 1024 * 1024,
    ) -> None:
        super().__init__(
            allowed_commands=allowed_commands,
            blocked_commands=blocked_commands,
            timeout_seconds=timeout_seconds,
            max_output_bytes=max_output_bytes,
        )
        self._container = container
        # Shell inside the container.  Default by host platform (heuristic);
        # most sandbox images are Linux even when the host is Windows.
        self._shell = (
            shell[:]
            if shell
            else (["cmd", "/c"] if self._platform == "windows" else ["sh", "-c"])
        )

    def _build_shell_command(
        self,
        command: str,
        *,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
    ) -> list[str]:
        """Build a ``docker exec`` argv.

        *cwd* is forwarded as ``--workdir`` so the command's working
        directory is inside the container, not the host.  ``env`` entries
        are forwarded as ``-e`` flags — only the caller's explicit
        overrides are passed into the container, never the host's
        ``os.environ``.
        """
        argv = [_DOCKER, "exec", "-i"]
        if cwd:
            argv += ["--workdir", cwd]
        if env:
            for key, value in env.items():
                argv += ["-e", f"{key}={value}"]
        argv += [self._container, *self._shell, command]
        return argv
