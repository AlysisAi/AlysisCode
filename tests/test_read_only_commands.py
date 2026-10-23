"""The commands every mode may run without asking: they only look."""

from __future__ import annotations

import pytest

from alysis_code.read_only_commands import is_read_only_command
from alysis_code.shell_command_lexer import Redirect, split_simple_commands

READ_ONLY = [
    ("posix", "ls -la"),
    ("posix", "pwd"),
    ("posix", "git status"),
    ("posix", "git status --short 2>&1"),
    ("posix", "git log --oneline -5"),
    ("posix", "git diff HEAD~1 -- src"),
    ("posix", "git -C sub --no-pager log -p"),
    ("posix", "git branch -a"),
    ("posix", "git branch --contains abc123"),
    ("posix", "git tag -l 'v*'"),
    ("posix", "git remote -v"),
    ("posix", "git stash list"),
    ("posix", "git ls-files -o --exclude-standard"),
    ("posix", "git config --get user.email"),
    ("posix", "git --version"),
    ("posix", "rg -n 'foo|bar' src"),
    ("posix", "grep -rn TODO ."),
    ("posix", "find . -name '*.py' -type f"),
    ("posix", "cat README.md | head -20"),
    ("posix", "ls 2>/dev/null"),
    ("posix", "ls &>/dev/null"),
    ("posix", "sed -n '1,80p' src/app.py"),
    ("posix", "wc -l src/*.py"),
    ("posix", "node --version"),
    ("posix", "go version"),
    ("posix", "cd src && ls"),
    ("posix", "pip list"),
    ("posix", "npm ls"),
    ("posix", "echo 'a & rm x'"),
    ("posix", "date +%Y-%m-%d"),
    ("posix", "sort -n data.txt | uniq -c"),
    ("posix", "bash -c 'git status && ls'"),
    ("cmd", "dir /s"),
    ("cmd", "where python"),
    ("cmd", "type README.md"),
    ("cmd", "findstr /n TODO *.py"),
    ("cmd", "git status 2>nul"),
    ("cmd", 'cmd /c "dir"'),
    ("cmd", "date /t"),
    ("cmd", 'powershell -NoProfile -Command "Get-ChildItem -Recurse | Select-Object -First 5"'),
    ("cmd", "git.exe log -1"),
    ("powershell", "Get-ChildItem | Sort-Object Length"),
    ("powershell", "Get-Content README.md -TotalCount 20"),
    ("powershell", "Select-String -Path *.py -Pattern 'def '"),
    ("powershell", "gci -Recurse 2>$null"),
]

NOT_READ_ONLY = [
    ("posix", ""),
    ("posix", "rm -rf build"),
    ("posix", "ls > files.txt"),
    ("posix", "echo hi >> log.txt"),
    ("posix", "git push"),
    ("posix", "git commit -m x"),
    ("posix", "git branch -D feature"),
    ("posix", "git branch new-branch"),
    ("posix", "git tag v1"),
    ("posix", "git stash"),
    ("posix", "git remote add origin url"),
    ("posix", "git config user.name x"),
    ("posix", "git -c core.pager=less log"),
    ("posix", "git log --output=out.txt"),
    ("posix", "git grep -Ovim foo"),
    ("posix", "git reflog expire --all"),
    ("posix", "find . -delete"),
    ("posix", "find . -name x -exec rm {} \\;"),
    ("posix", "sed -i s/a/b/ f"),
    ("posix", "sed -n 'w out' f"),
    ("posix", "sort -o out.txt in.txt"),
    ("posix", "uniq in.txt out.txt"),
    ("posix", "rg --pre ./x foo"),
    ("posix", "date -s 2020-01-01"),
    ("posix", "env bash"),
    ("posix", "cat .env"),
    ("posix", "ls ~/.ssh"),
    ("posix", "cat < ~/.aws/credentials"),
    ("posix", "FOO=1 ls"),
    ("posix", "./ls"),
    ("posix", "/usr/local/bin/ls"),
    ("posix", "echo $(rm -rf x)"),
    ("posix", "echo `rm x`"),
    ("posix", 'echo "$(rm -rf x)"'),
    ("posix", "ls; rm x"),
    ("posix", "ls && npm install"),
    ("posix", "cat x | sh"),
    ("posix", "python script.py"),
    ("posix", "node -e \"require('fs').rmSync('x')\""),
    ("posix", "make version"),
    ("posix", "npm version patch"),
    ("posix", "fc -e - 1"),
    ("posix", "bash -c 'rm x'"),
    ("posix", "bash"),
    ("posix", "cat <<EOF\nx\nEOF"),
    ("posix", "(cd / && ls)"),
    ("posix", "xargs rm < list.txt"),
    ("posix", "tee out.txt"),
    ("cmd", "del x.txt"),
    ("cmd", 'cmd /c "dir & del x"'),
    ("cmd", "echo 'a & del x'"),
    ("cmd", "git show HEAD^"),
    ("cmd", "date"),
    ("cmd", "dir > files.txt"),
    ("cmd", "cmd"),
    ("cmd", "powershell -File script.ps1"),
    ("cmd", 'powershell -Command "Get-ChildItem; Remove-Item x"'),
    ("cmd", "C:\\tools\\ls.exe"),
    ("cmd", "attrib +r file.txt"),
    ("cmd", "set PATH=C:\\x"),
    ("powershell", "Get-ChildItem | ForEach-Object { Remove-Item $_ }"),
    ("powershell", "(Get-Content x).Length"),
    ("powershell", "Remove-Item x"),
    ("powershell", "Get-Content x | Set-Content y"),
    ("powershell", "Get-ChildItem > out.txt"),
    ("powershell", "& 'C:\\x.exe'"),
    ("powershell", "Invoke-WebRequest https://example.com -OutFile x"),
]


@pytest.mark.parametrize(("dialect", "command"), READ_ONLY)
def test_read_only_commands_are_recognized(dialect, command) -> None:  # type: ignore[no-untyped-def]
    assert is_read_only_command(command, dialect=dialect)


@pytest.mark.parametrize(("dialect", "command"), NOT_READ_ONLY)
def test_everything_else_keeps_the_mode_gate(dialect, command) -> None:  # type: ignore[no-untyped-def]
    assert not is_read_only_command(command, dialect=dialect)


def test_stream_duplication_is_a_redirect_not_a_background_job() -> None:
    commands = split_simple_commands("git status 2>&1 | head", dialect="posix")
    assert commands is not None
    assert [c.words for c in commands] == [("git", "status"), ("head",)]
    assert commands[0].redirects == (Redirect(">", "2", "&1"),)


def test_cmd_treats_single_quotes_as_ordinary_characters() -> None:
    commands = split_simple_commands("echo 'a & del x'", dialect="cmd")
    assert commands is not None
    assert [c.words for c in commands] == [("echo", "'a"), ("del", "x'")]
