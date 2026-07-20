from __future__ import annotations

from abc import ABC, abstractmethod


class ShellProvider(ABC):
    """Generates OS-specific shell commands for file operations and shell management.

    Each method returns a command string (or list of command parts) suitable
    for passing to a backend's ``exec_oneoff`` or ``open_shell``.

    The file-read protocol uses a 4-line structured output contract:
      line 1: file size in bytes (integer)
      line 2: base64 of first 4096 bytes (binary-detection sample)
      line 3: base64 of requested line-range content
      line 4: total line count (integer)
    Exit code 2 means "file not found / not readable".
    If file_size > max_size, only line 1 is emitted.
    """

    @property
    @abstractmethod
    def default_shell(self) -> str:
        """Shell binary name, e.g. 'bash' or 'powershell.exe'."""

    @property
    @abstractmethod
    def default_shell_args(self) -> list[str]:
        """Startup arguments for the shell, e.g. ['bash'] or
        ['powershell.exe', '-NoLogo', '-NoProfile', '-NonInteractive', '-Command']."""

    @property
    @abstractmethod
    def exec_flag(self) -> str:
        """Flag passed to the shell for one-off execution: '-c' for bash,
        '-Command' for PowerShell."""

    @property
    def setup_command(self) -> str:
        """Command to run once at shell startup before any user command.

        Bash needs nothing; PowerShell 5.1 needs to set UTF-8 output
        encoding to avoid UTF-16LE corruption over SSH.
        """
        return ""

    @property
    def reset_command(self) -> str:
        """Command to send when a command times out (shell may be stuck
        in continuation mode).  Should break the shell back to a clean
        prompt without producing output or changing state.

        Bash rarely reaches continuation mode; PowerShell may need a
        simple ``$null`` statement to break out.
        """
        return ""

    # ---- File read ----

    @abstractmethod
    def file_read_command(self, path: str, offset: int, limit: int,
                          max_size: int) -> str:
        """Generate a one-shot file-read command.

        Must produce the 4-line structured output described in the class
        docstring.  ``offset`` is 1-based, ``limit`` is the max lines to
        return.  ``max_size`` is the size threshold in bytes — when the
        file exceeds it, only emit line 1.
        """

    # ---- File write helpers ----

    @abstractmethod
    def atomic_write_script(self, path: str) -> str:
        """Return a script that reads content from stdin and writes
        it atomically to *path* (temp-file + rename)."""

    @abstractmethod
    def mkdir_command(self, parent_dir: str) -> str:
        """Create a directory (and parents), no-op if it exists."""

    # ---- File utilities ----

    @abstractmethod
    def cat_command(self, path: str) -> str:
        """Read a whole file to stdout.  Standard error suppressed."""

    @abstractmethod
    def list_dir_command(self, dir_: str, limit: int = 50) -> str:
        """List file names in *dir_*, one per line, up to *limit* entries."""

    # ---- Patch ----

    @abstractmethod
    def base64_decode_command(self, encoded: str) -> str:
        """Return a command that decodes *encoded* (a base64 string) to
        stdout.

        The caller provides the pre-encoded text so no stdin piping is
        needed.  Example (bash): ``echo 'aGVsbG8=' | base64 -d``
        """

    @abstractmethod
    def patch_apply_command(self) -> str:
        """Return a command that reads a unified-diff from stdin and
        applies it with ``patch -p0`` semantics.

        When the platform lacks ``patch`` (e.g. Windows), the command
        should exit with code 2 so the caller falls back to the in-process
        Python implementation.
        """

    # ---- Search (ripgrep) ----

    @abstractmethod
    def search_files_command(self, pattern: str, path: str, limit: int) -> str:
        """Glob-style file search via ripgrep's ``--files`` with mtime sort."""

    @abstractmethod
    def search_content_command(self, pattern: str, path: str,
                               file_glob: str, limit: int,
                               output_mode: str,
                               context: int) -> str:
        """ripgrep content search returning ``path:line:snippet`` output."""

    # ---- Dual-marker protocol ----

    @abstractmethod
    def marker_start_command(self, marker_id: str) -> str:
        """Command that emits ``__START_{marker_id}__`` on its own line."""

    @abstractmethod
    def marker_end_command(self, marker_id: str) -> str:
        """Command that emits ``__END_{marker_id}__:$?`` on its own line."""


class ShellProviderFactory:
    """Registry + factory for :class:`ShellProvider` implementations.

    Usage::

        provider = ShellProviderFactory.create("linux")   # → BashShellProvider
        provider = ShellProviderFactory.create("windows")  # → PowerShellProvider
    """

    _providers: dict[str, type[ShellProvider]] = {}

    @classmethod
    def register(cls, os_type: str, provider_cls: type[ShellProvider]) -> None:
        cls._providers[os_type] = provider_cls

    @classmethod
    def create(cls, os_type: str) -> ShellProvider:
        if not cls._providers:
            from sandbox_mcp.shell_providers.bash_provider import BashShellProvider
            from sandbox_mcp.shell_providers.powershell_provider import PowerShellProvider

            cls.register("linux", BashShellProvider)
            cls.register("windows", PowerShellProvider)

        provider_cls = cls._providers.get(os_type)
        if provider_cls is None:
            raise ValueError(
                f"Unknown OS type: {os_type!r}. "
                f"Available: {list(cls._providers)}"
            )
        return provider_cls()
