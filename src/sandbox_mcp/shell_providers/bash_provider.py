from __future__ import annotations

import shlex

from sandbox_mcp.shell_provider import ShellProvider


class BashShellProvider(ShellProvider):
    """Generates bash (Linux) commands — the original sandbox-mcp behaviour.

    All commands use GNU coreutils (``stat``, ``head``, ``sed``, ``base64``,
    ``wc``, ``cat``, ``ls``, ``rg``, ``patch``) via ``bash``.
    """

    # Default mktemp pattern; used by ``atomic_write_script``.
    _TMPFILE_PATTERN = ".sandbox-mcp-tmp.XXXXXX"

    @property
    def default_shell(self) -> str:
        return "bash"

    @property
    def default_shell_args(self) -> list[str]:
        return ["bash"]

    # ---- File read ----

    def file_read_command(self, path: str, offset: int, limit: int,
                          max_size: int) -> str:
        q_path = shlex.quote(path)
        end_line = offset + limit - 1
        return (
            f"f={q_path}; ms={max_size}; "
            f'[[ ! -r "$f" ]] && exit 2; '
            f'sz=$(stat -c %s "$f"); echo "$sz"; '
            f"if (( sz <= ms )); then "
            f'head -c 4096 "$f" | base64 -w0; echo; '
            f'sed -n {offset},{end_line}p "$f" | base64 -w0; echo; '
            f"wc -l < \"$f\" | tr -d ' '; "
            f"fi"
        )

    # ---- File write helpers ----

    def atomic_write_script(self, path: str) -> str:
        return (
            "set -e; "
            f"t={shlex.quote(path)}; "
            f'tmp=$(mktemp -p "${{t%/*}}" {self._TMPFILE_PATTERN} 2>/dev/null || '
            f"mktemp {self._TMPFILE_PATTERN} 2>/dev/null); "
            '[ -n "$tmp" ] || { echo "atomic write: mktemp failed" >&2; exit 1; }; '
            'cat > "$tmp"; '
            'mv -f "$tmp" "$t"; '
            'rm -f "$tmp"'
        )

    def mkdir_command(self, parent_dir: str) -> str:
        return f"mkdir -p {shlex.quote(parent_dir)}"

    # ---- File utilities ----

    def cat_command(self, path: str) -> str:
        return f"cat {shlex.quote(path)} 2>/dev/null"

    def list_dir_command(self, dir_: str, limit: int = 50) -> str:
        return f"ls -1 {shlex.quote(dir_)} 2>/dev/null | head -{limit}"

    # ---- Patch ----

    def base64_decode_command(self, encoded: str) -> str:
        return f"echo {shlex.quote(encoded)} | base64 -d"

    def patch_apply_command(self) -> str:
        return "patch -p0"

    # ---- Search ----

    def search_files_command(self, pattern: str, path: str, limit: int) -> str:
        glob_pattern = (
            f"*{pattern}"
            if "/" not in pattern and not pattern.startswith("*")
            else pattern
        )
        return (
            f"set -o pipefail; "
            f"rg --files --sortr=modified -g {shlex.quote(glob_pattern)} "
            f"{shlex.quote(path)} 2>/dev/null | head -n {limit}"
        )

    def search_content_command(self, pattern: str, path: str,
                                file_glob: str, limit: int,
                                output_mode: str,
                                context: int) -> str:
        q_pattern = shlex.quote(pattern)
        q_path = shlex.quote(path)
        parts = ["set -o pipefail; rg",
                 "--line-number", "--no-heading", "--with-filename"]
        if context > 0:
            parts += ["-C", str(context)]
        if file_glob:
            parts += ["--glob", shlex.quote(file_glob)]
        if output_mode == "files_only":
            parts.append("-l")
        elif output_mode == "count":
            parts.append("-c")
        parts += [q_pattern, q_path, "|", "head", "-n", str(limit)]
        return " ".join(parts)

    # ---- Dual-marker protocol ----

    def marker_start_command(self, marker_id: str) -> str:
        return f"echo __START_{marker_id}__"

    def marker_end_command(self, marker_id: str) -> str:
        return f"echo __END_{marker_id}__:$?"
