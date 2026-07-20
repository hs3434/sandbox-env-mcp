from __future__ import annotations

from sandbox_mcp.shell_provider import ShellProvider


class PowerShellProvider(ShellProvider):
    """Generates PowerShell commands for Windows containers and remote
    Windows machines (WinRM / SSH).

    All commands are designed for ``powershell.exe -NoLogo -NoProfile
    -NonInteractive`` (interactive stdin) or with ``exec_flag`` ``-Command``
    for one-off execution.  Paths are single-quoted to avoid
    variable-expansion issues.

    *encoding* is the system's active code page (``gbk`` for Chinese
    Windows, ``shift-jis`` for Japanese, etc.).  Defaults to ``gbk``.
    """

    def __init__(self, encoding: str = "gbk"):
        self._encoding = encoding

    @property
    def default_shell(self) -> str:
        return "powershell.exe"

    @property
    def default_shell_args(self) -> list[str]:
        return ["powershell.exe", "-NoLogo", "-NoProfile", "-NonInteractive"]

    @property
    def exec_flag(self) -> str:
        return "-Command"

    @property
    def input_encoding(self) -> str:
        return self._encoding

    @property
    def setup_command(self) -> str:
        return (
            "chcp 65001 | Out-Null; "
            "[Console]::InputEncoding = [Text.Encoding]::UTF8; "
            "[Console]::OutputEncoding = [Text.Encoding]::UTF8; "
            "$OutputEncoding = [Text.Encoding]::UTF8; "
            "$env:PYTHONIOENCODING='utf-8'; "
            "$env:PYTHONUTF8='1'; "
            "$PSDefaultParameterValues['*:Encoding']='utf8'"
        )

    @staticmethod
    def _esc(path: str) -> str:
        """Escape *path* for a single-quoted PowerShell string literal."""
        return path.replace("'", "''")

    # ---- File read ----

    def file_read_command(self, path: str, offset: int, limit: int,
                          max_size: int) -> str:
        p = self._esc(path)
        end_line = offset + limit - 1
        return (
            f"$f='{p}'; "
            f"if(-not (Test-Path $f -PathType Leaf)){{exit 2}}; "
            f"$sz=(Get-Item $f).Length; Write-Host $sz; "
            f"if($sz -le {max_size}){{"
            f"  $bytes=[IO.File]::ReadAllBytes($f); "
            f"  if($bytes.Length -ge 4096){{$head=$bytes[0..4095]}}else{{$head=$bytes}}; "
            f"  Write-Host ([Convert]::ToBase64String($head)); "
            f"  $lines=Get-Content $f -TotalCount {end_line} | Select-Object -Skip {offset - 1}; "
            f'  $txt=($lines -join "`n"); '
            f"  Write-Host ([Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($txt))); "
            f"  $lc=(Get-Content $f | Measure-Object -Line).Lines; Write-Host $lc"
            f"}}"
        )

    # ---- File write helpers ----

    def atomic_write_script(self, path: str) -> str:
        p = self._esc(path)
        return (
            f"$t='{p}'; "
            f"$dir=Split-Path $t -Parent; "
            f"if($dir -and -not (Test-Path $dir)){{"
            f"New-Item -ItemType Directory -Path $dir -Force | Out-Null}}; "
            f'$tmp=Join-Path $dir ".sandbox_mcp_$(Get-Random -Hex 6)"; '
            f"$content=[Console]::In.ReadToEnd(); "
            f"[IO.File]::WriteAllText($tmp, $content, [Text.Encoding]::UTF8); "
            f"Move-Item -Force $tmp $t; "
            f"Remove-Item -Force $tmp -ErrorAction SilentlyContinue"
        )

    def mkdir_command(self, parent_dir: str) -> str:
        d = self._esc(parent_dir)
        return (
            f"New-Item -ItemType Directory -Path '{d}' -Force "
            f"-ErrorAction SilentlyContinue | Out-Null"
        )

    # ---- File utilities ----

    def cat_command(self, path: str) -> str:
        p = self._esc(path)
        return f"Get-Content '{p}' -Raw 2>$null"

    def list_dir_command(self, dir_: str, limit: int = 50) -> str:
        d = self._esc(dir_)
        return (
            f"Get-ChildItem '{d}' -Name -ErrorAction SilentlyContinue "
            f"| Select-Object -First {limit}"
        )

    # ---- Patch ----

    def base64_decode_command(self, encoded: str) -> str:
        return (
            f"[Text.Encoding]::UTF8.GetString("
            f"[Convert]::FromBase64String('{self._esc(encoded)}'))"
        )

    def patch_apply_command(self) -> str:
        return "patch -p0 2>$null; if($LASTEXITCODE -ne 0){exit 2}"

    # ---- Search ----

    def search_files_command(self, pattern: str, path: str, limit: int) -> str:
        p = self._esc(path)
        q = self._esc(pattern)
        return (
            f"rg --files --sortr=modified -g '{q}' "
            f"'{p}' 2>&1 | Select-Object -First {limit}"
        )

    def search_content_command(self, pattern: str, path: str,
                                file_glob: str, limit: int,
                                output_mode: str,
                                context: int) -> str:
        p = self._esc(path)
        q = self._esc(pattern)
        parts = ["rg", "--line-number", "--no-heading", "--with-filename"]
        if context > 0:
            parts += ["-C", str(context)]
        if file_glob:
            parts += ["--glob", f"'{self._esc(file_glob)}'"]
        if output_mode == "files_only":
            parts.append("-l")
        elif output_mode == "count":
            parts.append("-c")
        parts += [f"'{q}'", f"'{p}'"]
        parts += ["|", "Select-Object", "-First", str(limit)]
        return " ".join(parts)

    # ---- Dual-marker protocol ----

    def marker_start_command(self, marker_id: str) -> str:
        return f"Write-Host '__START_{marker_id}__'"

    def marker_end_command(self, marker_id: str) -> str:
        return f"Write-Host \"__END_{marker_id}__:$LASTEXITCODE\""
