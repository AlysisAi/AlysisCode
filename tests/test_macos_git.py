from __future__ import annotations

import shlex

import pytest

from alysis_code.macos_git import GIT_EXECUTABLE, literal_git_argv, literal_git_process


@pytest.mark.parametrize(
    "arguments",
    [
        ["switch", "-c", "δοκιμή"],
        ["-C", "/project with spaces", "commit", "-m", "'quoted' $() `stuff`;\n世界"],
        ["-c", "alias.example=!echo hello", "example"],
        ["add", "--", "[literal].txt", "", "a\\b"],
    ],
)
def test_literal_arguments_round_trip(arguments):
    assert literal_git_argv(shlex.join(["git", *arguments])) == [GIT_EXECUTABLE, *arguments]


def test_double_quote_escapes_follow_posix_shell_rules():
    assert literal_git_argv(r'git commit -m "a\$b\`c\"d\\e\q"') == [
        GIT_EXECUTABLE,
        "commit",
        "-m",
        'a$b`c"d\\e\\q',
    ]


@pytest.mark.parametrize(
    "command",
    [
        "git add .; touch .git/changed",
        "git status && echo yes",
        "git status | cat",
        "git add *",
        'git commit -m "$SECRET"',
        "git -C $ROOT status",
        "git status > .git/changed",
        "git $(echo status)",
        "git `echo status`",
        "git status\ntrue",
        "git status # comment",
        "git status &",
        "git status < input",
        "git status || true",
        "git status " + "\\" + "\n",
        "git 'unclosed",
        "./git add .",
        "env git add .",
        "GIT_DIR=elsewhere git status",
        "git status\0",
    ],
)
def test_shell_syntax_never_receives_git_metadata_permission(command):
    assert literal_git_argv(command) is None


def test_git_lists_quote_every_argument_and_preserve_operators():
    assert literal_git_process(
        "git add . && git commit -m '世界 ; $(touch BAD)' || git status"
    ) == [
        "/bin/sh",
        "-c",
        "/usr/bin/git add . && /usr/bin/git commit -m '世界 ; $(touch BAD)' || /usr/bin/git status",
    ]


@pytest.mark.parametrize(
    "command",
    [
        "git add . && touch .git/evil",
        "echo x; git add .",
        "git add . | git status",
        "git add . &&",
        "git status;",
        'git status && git commit -m "$SECRET"',
        "git status && /tmp/git add .",
        "git status && git status > .git/HEAD",
    ],
)
def test_mixed_or_dynamic_lists_do_not_get_git_metadata_access(command):
    assert literal_git_process(command) is None
