from __future__ import annotations

from sandbox_mcp.shell_provider import ShellProvider


class PowerShellProvider(ShellProvider):
    """Generates PowerShell commands for Windows containers and remote
    Windows machines over SSH.

    All commands are designed for ``powershell.exe -NoLogo -NoProfile
    -NonInteractive`` (interactive stdin) or with ``exec_flag`` ``-Command``
    for one-off execution.  Paths are single-quoted to avoid
    variable-expansion issues.

    *input_encoding* / *output_encoding* are the codecs the remote
    console actually uses — they default to ``gbk`` (Chinese Windows)
    but should be set from the encoding probe.  No ``chcp`` or
    ``[Console]::OutputEncoding`` overrides are issued, because those
    are honoured only after the host has finished reading the argv and
    break the prompt-function installation on localized Windows.
    """

    def __init__(
        self,
        input_encoding: str = "gbk",
        output_encoding: str | None = None,
    ):
        self._input_encoding = input_encoding
        self._output_encoding = output_encoding or input_encoding

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
        return self._input_encoding

    @property
    def output_encoding(self) -> str:
        return self._output_encoding

    @property
    def setup_command(self) -> str:
        return ""

    @staticmethod
    def _esc(path: str) -> str:
        return path.replace("'", "''")

    def file_read_command(self, path: str, offset: int, limit: int, max_size: int) -> str:
        p = self._esc(path)
        end_line = offset + limit - 1
        return (
            f"$f='{p}'; "
            f"if(-not (Test-Path $f -PathType Leaf)){{exit 2}}; "
            f"$sz=(Get-Item $f).Length; Write-Output $sz; "
            f"if($sz -le {max_size}){{"
            f"  $bytes=[IO.File]::ReadAllBytes($f); "
            f"  if($bytes.Length -ge 4096){{$head=$bytes[0..4095]}}else{{$head=$bytes}}; "
            f"  Write-Output ([Convert]::ToBase64String($head)); "
            f"  $lines=Get-Content $f -TotalCount {end_line} | Select-Object -Skip {offset - 1}; "
            f'  $txt=($lines -join "`n"); '
            f"  Write-Output ([Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($txt))); "
            f"  $lc=(Get-Content $f | Measure-Object -Line).Lines; Write-Output $lc"
            f"}}"
        )

    def atomic_write_script(self, path: str) -> str:
        p = self._esc(path)
        return (
            f"$t='{p}'; "
            f"$dir=Split-Path $t -Parent; "
            f"if($dir -and -not (Test-Path $dir)){{"
            f"New-Item -ItemType Directory -Path $dir -Force | Out-Null}}; "
            f'$tmp=Join-Path $dir ".sandbox_mcp_$(Get-Random)"; '
            f"$content=[Console]::In.ReadToEnd(); "
            f"[IO.File]::WriteAllText($tmp, $content, [Text.Encoding]::UTF8); "
            f"Move-Item -Force $tmp $t; "
            f"Remove-Item -Force $tmp -ErrorAction SilentlyContinue; "
            f"Write-Output OK"
        )

    def mkdir_command(self, parent_dir: str) -> str:
        d = self._esc(parent_dir)
        return (
            f"New-Item -ItemType Directory -Path '{d}' -Force "
            f"-ErrorAction SilentlyContinue | Out-Null"
        )

    def cat_command(self, path: str) -> str:
        p = self._esc(path)
        return f"Get-Content '{p}' -Raw 2>$null"

    def list_dir_command(self, dir_: str, limit: int = 50) -> str:
        d = self._esc(dir_)
        return (
            f"Get-ChildItem '{d}' -Name -ErrorAction SilentlyContinue "
            f"| Select-Object -First {limit}"
        )

    def base64_decode_command(self, encoded: str) -> str:
        return (
            f"[Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('{self._esc(encoded)}'))"
        )

    def patch_apply_command(self) -> str:
        return "patch -p0 2>$null; if($LASTEXITCODE -ne 0){exit 2}"

    def search_files_command(self, pattern: str, path: str, limit: int) -> str:
        p = self._esc(path)
        q = self._esc(pattern)
        return f"rg --files --sortr=modified -g '{q}' '{p}' 2>&1 | Select-Object -First {limit}"

    def search_content_command(
        self, pattern: str, path: str, file_glob: str, limit: int, output_mode: str, context: int
    ) -> str:
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

    def prompt_setup_command(self, token: str) -> str:
        return ""

    @property
    def uses_prompt(self) -> bool:
        return False
