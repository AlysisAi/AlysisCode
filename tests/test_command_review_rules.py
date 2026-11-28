"""Commands a workspace sandbox does not make harmless get a decision in "Approve for me"."""

from __future__ import annotations

import pytest

from alysis_code.command_review_rules import network_command_reviewable, review_reasons
from alysis_code.policy import evaluate_shell_command


@pytest.mark.parametrize("dialect", ["posix", "powershell"])
@pytest.mark.parametrize(
    ("command", "reviewable"),
    [
        ("node scripts/check.mjs", True),
        ("python -m pytest tests", True),
        ("curl -I https://example.com", True),
        ("curl --max-time 10 -I https://example.com", True),
        ("curl --max-time bogus -I https://example.com", False),
        ("curl --max-time 10 -X POST https://example.com", False),
        ("curl -X POST https://example.com", False),
        ("git push origin main", False),
        ("python ../another-project/script.py", False),
        ("npm publish", False),
        ("npm test; git push", False),
        ("npm --prefix web run check", True),
        ("npm --prefix web publish", False),
        ("npm --prefix ../other run check", False),
    ],
)
def test_network_commands_eligible_for_review(dialect, command, reviewable):
    assert network_command_reviewable(command, dialect) is reviewable


@pytest.mark.parametrize(
    ("command", "reviewable"),
    [
        ("New-Item -ItemType Directory -Path out", True),
        ("New-Item -Path out -itemtype directory -Force", True),
        ("New-Item -ItemType SymbolicLink -Path out -Value elsewhere", False),
        ("npm.cmd run check", True),
        ("npm.cmd publish", False),
        ("npm.cmd run check; npm.cmd publish", False),
    ],
)
def test_windows_network_command_forms(command, reviewable):
    assert network_command_reviewable(command, "powershell") is reviewable
    assert not network_command_reviewable(command, "posix")


@pytest.mark.parametrize(
    ("dialect", "command"),
    [
        (
            "powershell",
            "Get-ChildItem -Force | Select-Object Name, Mode; Write-Output '--- web ---'; "
            "Get-ChildItem -Force web | Select-Object Name, Mode; Write-Output '--- build candidates ---'; "
            "if (Test-Path build) { Get-ChildItem -Recurse -Force build | Select-Object FullName }; "
            r"if (Test-Path web\build) { Get-ChildItem -Recurse -Force web\build | Select-Object FullName }",
        ),
        (
            "powershell",
            "IF (Test-Path -LiteralPath 'build output') { Get-ChildItem 'build output' }\r\n"
            "ELSE { Write-Output 'missing' }",
        ),
        ("posix", "ls -a; if [ -d build ]; then ls build; else echo absent; fi"),
        ("posix", "if test -f notes.txt\nthen\ncat notes.txt\nfi"),
    ],
)
def test_literal_conditional_inspection_routes_only_to_reviewer(dialect, command):
    from alysis_code.read_only_commands import is_read_only_command

    assert network_command_reviewable(command, dialect)
    assert not is_read_only_command(command, dialect=dialect)


@pytest.mark.parametrize(
    ("dialect", "command"),
    [
        ("powershell", "if (Test-Path build) { Get-ChildItem build; Remove-Item notes.txt }"),
        (
            "powershell",
            "if (Test-Path build) { Get-ChildItem build } else { Set-Content notes.txt x }",
        ),
        ("powershell", "if (Test-Path build) { Get-ChildItem build }; npm publish"),
        ("powershell", "if (Test-Path build) { Get-Content .env }"),
        ("powershell", "if (Test-Path build) { Get-ChildItem > listing.txt }"),
        ("powershell", "if (Test-Path build) { Get-ChildItem"),
        ("posix", "if [ -d build ]; then ls build; else rm notes.txt; fi"),
        ("posix", "if test -f notes.txt; then cat notes.txt; fi; npm publish"),
        ("posix", "if [ -f .env ]; then echo exists; fi"),
    ],
)
def test_conditional_with_unresolved_or_mutating_effects_keeps_existing_gate(dialect, command):
    assert not network_command_reviewable(command, dialect)


@pytest.mark.parametrize(
    ("dialect", "command", "fragment"),
    [
        ("posix", "git reset --hard HEAD~1", "git reset --hard"),
        ("posix", "git -C repo reset --hard", "git reset --hard"),
        ("posix", "git clean -fdx", "git clean -f"),
        ("posix", "git checkout -- .", "git checkout"),
        ("posix", "git checkout .", "git checkout"),
        ("posix", "git restore src/app.py", "git restore"),
        ("posix", "git switch --discard-changes main", "git switch"),
        ("posix", "git branch -D feature", "git branch -D"),
        ("posix", "git stash drop", "git stash"),
        ("posix", "git filter-branch --tree-filter x HEAD", "rewrites history"),
        ("posix", "npm publish", "npm publish"),
        ("posix", "python -m twine upload dist/*", "twine upload"),
        ("posix", "docker push registry/app:1", "docker push"),
        ("posix", "terraform apply -auto-approve", "terraform apply"),
        ("posix", "kubectl delete pod web-1", "kubectl delete"),
        ("posix", "gh release create v1.0", "changes GitHub"),
        ("posix", "gh pr merge 12 --squash", "changes GitHub"),
        ("posix", "aws s3 rm s3://bucket/key", "changes cloud resources"),
        ("posix", "vercel", "deploys"),
        ("posix", "cat .env", "credential file"),
        ("posix", "scp ~/.ssh/id_rsa host:", "credential file"),
        ("posix", "echo KEY=1 > .env", "writes a credential file"),
        ("cmd", 'cmd /c "git clean -fdx"', "git clean -f"),
        ("cmd", "type %USERPROFILE%\\.aws\\credentials", "credential file"),
    ],
)
def test_reasons(dialect, command, fragment) -> None:  # type: ignore[no-untyped-def]
    reasons = review_reasons(command, dialect=dialect)
    assert any(fragment in reason for reason in reasons), reasons


@pytest.mark.parametrize(
    ("dialect", "command"),
    [
        ("posix", "git status"),
        ("posix", "git reset HEAD file.py"),
        ("posix", "git restore --staged file.py"),
        ("posix", "git checkout -b feature"),
        ("posix", "git checkout main"),
        ("posix", "git branch -d merged"),
        ("posix", "git stash pop"),
        ("posix", 'echo "git reset --hard"'),
        ("posix", "npm run build"),
        ("posix", "docker build -t app ."),
        ("posix", "terraform plan"),
        ("posix", "kubectl get pods"),
        ("posix", "aws s3 ls"),
        ("posix", "gh pr view 12"),
        ("posix", "cat .env.example"),
        ("posix", "grep -rn secret src"),
    ],
)
def test_ordinary_commands_have_no_reason(dialect, command) -> None:  # type: ignore[no-untyped-def]
    assert review_reasons(command, dialect=dialect) == []


def test_policy_marks_them_for_a_decision() -> None:
    decision = evaluate_shell_command("git reset --hard")
    assert decision.allowed and decision.needs_confirm
    assert "git reset --hard" in decision.reason
    assert evaluate_shell_command("git status") == evaluate_shell_command("git status")
    assert not evaluate_shell_command("git status").needs_confirm
