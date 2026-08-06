"""Tests for ShellProvider ABC, BashShellProvider, and PowerShellProvider."""

import pytest

from sandbox_mcp.shell_provider import ShellProvider, ShellProviderFactory


class TestShellProviderABC:
    """Verify the ABC cannot be instantiated and the factory works."""

    def test_cannot_instantiate_abc(self):
        with pytest.raises(TypeError):
            ShellProvider()  # type: ignore

    def test_factory_creates_linux(self):
        provider = ShellProviderFactory.create("linux")
        assert provider.default_shell == "bash"

    def test_factory_creates_windows(self):
        provider = ShellProviderFactory.create("windows")
        assert provider.default_shell == "powershell.exe"

    def test_factory_unknown_os_raises(self):
        with pytest.raises(ValueError, match="Unknown OS type"):
            ShellProviderFactory.create("macos")


class TestBashShellProvider:
    """Verify BashShellProvider generates correct linux commands."""

    @pytest.fixture
    def bash(self):
        return ShellProviderFactory.create("linux")

    def test_properties(self, bash):
        assert bash.default_shell == "bash"
        assert bash.default_shell_args == ["bash"]

    def test_file_read_command_structure(self, bash):
        cmd = bash.file_read_command("/tmp/test.py", 1, 10, 1048576)
        assert "stat -c %s" in cmd
        assert "base64 -w0" in cmd
        assert "sed -n 1,10p" in cmd
        assert "wc -l" in cmd
        assert "exit 2" in cmd

    def test_file_read_too_large_only_emits_size(self, bash):
        """When file_size > max_size, skip the content section."""
        cmd = bash.file_read_command("/tmp/big.log", 1, 10, 1000)
        assert "stat" in cmd
        # The size check is: if (( sz <= ms )); then ...
        assert "if (( sz <= ms ))" in cmd

    def test_file_read_uses_correct_offset(self, bash):
        """Offset and limit are passed correctly to sed."""
        cmd = bash.file_read_command("/tmp/test.py", 5, 20, 1048576)
        assert "sed -n 5,24p" in cmd  # end_line = offset + limit - 1

    def test_cat_command(self, bash):
        cmd = bash.cat_command("/tmp/test.py")
        assert cmd == "cat /tmp/test.py 2>/dev/null"

    def test_cat_quotes_special_chars(self, bash):
        cmd = bash.cat_command("/tmp/my file.py")
        assert "'/tmp/my file.py'" in cmd

    def test_list_dir_command(self, bash):
        cmd = bash.list_dir_command("/workspace")
        assert "ls -1" in cmd
        assert "head -50" in cmd

    def test_mkdir_command(self, bash):
        cmd = bash.mkdir_command("/workspace/sub")
        assert "mkdir -p" in cmd

    def test_atomic_write_script_uses_mktemp(self, bash):
        script = bash.atomic_write_script("/workspace/app.py")
        assert "set -e" in script
        assert "mktemp" in script
        assert "cat >" in script
        assert "mv -f" in script
        assert "rm -f" in script

    def test_atomic_write_script_quotes_path(self, bash):
        script = bash.atomic_write_script("/workspace/my app.py")
        # The path appears inside quotes in set -e; t=...;
        assert "my app.py" in script

    def test_prompt_setup(self, bash):
        cmd = bash.prompt_setup_command("abc123")
        assert "PS1" in cmd
        assert "abc123" in cmd
        assert "SETUP_OK" in cmd

    def test_base64_decode_command(self, bash):
        cmd = bash.base64_decode_command("aGVsbG8=")
        assert "base64 -d" in cmd
        assert "aGVsbG8=" in cmd

    def test_patch_apply_command(self, bash):
        cmd = bash.patch_apply_command()
        assert cmd == "patch -p0"

    def test_search_files_command(self, bash):
        cmd = bash.search_files_command("*.py", "/workspace", 100)
        assert "rg --files" in cmd
        assert "--sortr=modified" in cmd
        assert "*.py" in cmd
        assert "head -n 100" in cmd

    def test_search_content_command(self, bash):
        cmd = bash.search_content_command("TODO", "/workspace", "*.py", 50, "content", 0)
        assert "rg --line-number" in cmd
        assert "TODO" in cmd
        assert "head -n 50" in cmd

    def test_search_content_files_only(self, bash):
        cmd = bash.search_content_command("TODO", "/", "", 10, "files_only", 0)
        assert "-l" in cmd
        assert "TODO" in cmd

    def test_search_content_count_mode(self, bash):
        cmd = bash.search_content_command("TODO", "/", "", 10, "count", 0)
        assert "-c" in cmd
        assert "TODO" in cmd

    def test_search_content_with_context(self, bash):
        cmd = bash.search_content_command("TODO", "/", "", 10, "content", 3)
        assert "-C 3" in cmd


class TestPowerShellProvider:
    """Verify PowerShellProvider generates correct Windows commands."""

    @pytest.fixture
    def ps(self):
        return ShellProviderFactory.create("windows")

    def test_properties(self, ps):
        assert "powershell" in ps.default_shell
        assert "-NoLogo" in ps.default_shell_args

    def test_file_read_command_structure(self, ps):
        cmd = ps.file_read_command("C:\\workspace\\test.py", 1, 10, 1048576)
        assert "Test-Path" in cmd
        assert "Get-Item" in cmd
        assert "Convert]::ToBase64String" in cmd
        assert "Get-Content" in cmd
        assert "Measure-Object -Line" in cmd

    def test_file_read_with_single_quote_in_path(self, ps):
        """Paths with single quotes get doubled."""
        cmd = ps.file_read_command("C:\\workspace\\it's.py", 1, 10, 1048576)
        assert "it''s" in cmd

    def test_prompt_setup(self, ps):
        setup = ps.prompt_setup_command("abc")
        assert "function prompt" in setup
        assert "abc" in setup
        assert "LASTEXITCODE" in setup
        assert "SETUP_OK" in setup

    def test_atomic_write_script(self, ps):
        script = ps.atomic_write_script("C:\\workspace\\app.py")
        assert "Move-Item -Force" in script
        assert "Split-Path" in script
        assert "New-Item" in script
        assert "WriteAllText" in script

    def test_mkdir_command(self, ps):
        cmd = ps.mkdir_command("C:\\workspace\\sub")
        assert "New-Item -ItemType Directory" in cmd

    def test_cat_command(self, ps):
        cmd = ps.cat_command("C:\\workspace\\app.py")
        assert "Get-Content" in cmd
        assert "2>$null" in cmd

    def test_list_dir_command(self, ps):
        cmd = ps.list_dir_command("C:\\workspace")
        assert "Get-ChildItem" in cmd
        assert "-Name" in cmd

    def test_base64_decode_command(self, ps):
        cmd = ps.base64_decode_command("aGVsbG8=")
        assert "Convert]::FromBase64String" in cmd
        assert "aGVsbG8=" in cmd

    def test_patch_apply_command_falls_back(self, ps):
        """Windows doesn't have patch; the command should exit 2."""
        cmd = ps.patch_apply_command()
        assert "exit 2" in cmd

    def test_search_files_command(self, ps):
        cmd = ps.search_files_command("*.py", "C:\\workspace", 100)
        assert "rg --files" in cmd
        assert "Select-Object -First 100" in cmd

    def test_search_content_command(self, ps):
        cmd = ps.search_content_command("TODO", "C:\\workspace", "*.py", 50, "content", 0)
        assert "rg --line-number" in cmd
        assert "Select-Object -First 50" in cmd

    def test_search_content_files_only(self, ps):
        cmd = ps.search_content_command("TODO", "C:\\", "", 10, "files_only", 0)
        assert "-l" in cmd

    def test_search_content_count_mode(self, ps):
        cmd = ps.search_content_command("TODO", "C:\\", "", 10, "count", 0)
        assert "-c" in cmd

    def test_search_content_with_context(self, ps):
        cmd = ps.search_content_command("TODO", "C:\\", "", 10, "content", 3)
        assert "-C 3" in cmd
