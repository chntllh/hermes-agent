"""Git worktree concurrency and branch safety (plan Priority 2).

Four confirmed defects, all of which only bite once two agents share a repo:

1. TOCTOU in ``_ensure_git_worktree`` -- ``_git_branch_exists`` then
   ``git worktree add -b``; a sibling creating the branch in between made git
   exit 255 and took down the dispatch with no fallback.
2. ``refs/stash`` is repository-GLOBAL. Agent 1 stashes, agent 2 pops, and
   agent 1's uncommitted work is gone.
3. Slash branch names: git stores refs as files, so ``refs/heads/wt`` and the
   directory ``refs/heads/wt/`` are a fatal D/F conflict.
4. ``gitlock`` skipped its stale-lock sweep whenever ANY git process was
   running anywhere on the machine.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from hermes_cli import gitlock
from hermes_cli import kanban_db_workspace as kbw


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=str(repo), capture_output=True, text=True, check=False,
    )


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "T")
    (root / "README.md").write_text("hi\n", encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "init")
    return root


def _branch_exists(repo: Path, branch: str) -> bool:
    return _git(repo, "show-ref", "--verify", f"refs/heads/{branch}").returncode == 0


# ---------------------------------------------------------------------------
# 1. TOCTOU branch collision
# ---------------------------------------------------------------------------


def test_worktree_add_falls_back_when_the_branch_appeared_mid_flight(repo, monkeypatch):
    """Plan acceptance: concurrent creation of the same branch must not exit 255.

    The race is simulated the only way it can be made deterministic: the branch
    exists on disk while the existence probe reports it does not -- exactly the
    window a sibling worker creates it in.
    """
    _git(repo, "branch", "wt-t_race")
    monkeypatch.setattr(kbw, "_git_branch_exists", lambda *_a, **_k: False)

    target = repo / ".worktrees" / "t_race"
    kbw._ensure_git_worktree(repo, target, "wt-t_race")

    assert target.is_dir()
    assert _git(target, "branch", "--show-current").stdout.strip() == "wt-t_race"


def test_worktree_add_still_raises_on_an_unrelated_failure(repo):
    """The fallback must not swallow real errors."""
    with pytest.raises(RuntimeError, match="git worktree add failed"):
        kbw._ensure_git_worktree(repo, repo / ".worktrees" / "bad", "refs/heads/../../evil")


def test_worktree_add_reports_the_second_failure_when_the_fallback_also_fails(repo):
    """A branch already checked out elsewhere is a genuine conflict, not a race."""
    first = repo / ".worktrees" / "first"
    kbw._ensure_git_worktree(repo, first, "wt-shared")
    with pytest.raises(RuntimeError, match="git worktree add failed"):
        kbw._ensure_git_worktree(repo, repo / ".worktrees" / "second", "wt-shared")


# ---------------------------------------------------------------------------
# 3. Flat leaf branch naming
# ---------------------------------------------------------------------------


def test_generated_branches_are_flat():
    assert kbw.auto_branch_name("t_abc123") == "wt-t_abc123"
    assert "/" not in kbw.auto_branch_name("t_abc123")


def test_slash_naming_is_a_real_git_df_conflict(repo):
    """Pins why the rename matters rather than asserting a string."""
    assert _git(repo, "branch", "wt").returncode == 0
    # refs/heads/wt is a FILE; git cannot also create the directory refs/heads/wt/.
    assert _git(repo, "branch", "wt/t_child").returncode != 0
    # The flat name coexists with it.
    assert _git(repo, "branch", kbw.auto_branch_name("t_child")).returncode == 0


def test_auto_branch_recognition_covers_the_legacy_slash_prefix():
    """Teardown may only delete branches Hermes generated -- including in-flight
    boards created before the rename."""
    assert kbw.is_auto_branch("wt-t_new")
    assert kbw.is_auto_branch("wt/t_legacy")
    assert not kbw.is_auto_branch("main")
    assert not kbw.is_auto_branch("feature/wt-thing")


def test_worktree_teardown_deletes_both_branch_generations(repo):
    for branch, name in (("wt-t_new", "new"), ("wt/t_legacy", "legacy")):
        target = repo / ".worktrees" / name
        kbw._ensure_git_worktree(repo, target, branch)
        assert _branch_exists(repo, branch)
        kbw._cleanup_worktree_workspace("t_x", str(target), branch_name=branch)
        assert not _branch_exists(repo, branch), f"{branch} survived teardown"


def test_teardown_never_deletes_a_branch_hermes_did_not_generate(repo):
    target = repo / ".worktrees" / "custom"
    kbw._ensure_git_worktree(repo, target, "feature/mine")
    kbw._cleanup_worktree_workspace("t_x", str(target), branch_name="feature/mine")
    assert _branch_exists(repo, "feature/mine")


# ---------------------------------------------------------------------------
# 2. git stash prohibition
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("command", [
    "git stash",
    "git stash push -u -m wip",
    "git stash pop",
    "git stash apply stash@{0}",
    "git stash drop",
    "git stash clear",
    "git -C /srv/repo stash save wip",
    "make build && git stash pop",
])
def test_mutating_stash_commands_are_detected(command):
    from tools.terminal_tool_guards import detect_forbidden_git_stash

    assert detect_forbidden_git_stash(command) is True


@pytest.mark.parametrize("command", [
    "git stash list",
    "git stash show -p",
    "git commit -m 'wip: partial'",
    "git status",
    "git worktree list",
])
def test_read_only_and_unrelated_git_commands_pass(command):
    from tools.terminal_tool_guards import detect_forbidden_git_stash

    assert detect_forbidden_git_stash(command) is False


@pytest.mark.parametrize("command", [
    "git log --grep=stash",
    "grep -rn 'git stash' docs/",
    "echo 'never use git stash'",
    "python -c \"print('git stash')\"",
])
def test_merely_mentioning_stash_is_not_running_it(command):
    """A guard that blocks `git log --grep=stash` or a grep through the docs costs more
    than it saves, so detection is tokenized: shlex collapses quoted text into one
    token, and a mention is never an invocation."""
    from tools.terminal_tool_guards import detect_forbidden_git_stash

    assert detect_forbidden_git_stash(command) is False


def test_git_invoked_by_path_is_still_caught():
    from tools.terminal_tool_guards import detect_forbidden_git_stash

    assert detect_forbidden_git_stash("/usr/bin/git stash pop") is True


def test_an_unparseable_command_falls_back_to_over_blocking():
    """Unbalanced quotes defeat shlex; a command we cannot parse that still mentions
    git stash is refused rather than waved through."""
    from tools.terminal_tool_guards import detect_forbidden_git_stash

    assert detect_forbidden_git_stash("git stash pop 'unterminated") is True


def test_stash_is_blocked_inside_a_linked_worktree(repo):
    """Plan acceptance: `git stash` in an agent worktree is rejected with an
    explanation that names the alternative."""
    from tools.terminal_tool_guards import worktree_stash_block

    worktree = repo / ".worktrees" / "agent1"
    kbw._ensure_git_worktree(repo, worktree, "wt-t_agent1")

    blocked = worktree_stash_block(
        command="git stash pop", cwd=str(worktree), workdir=None, session_key="s",
    )
    assert blocked is not None
    assert "repository-global" in blocked
    assert "git commit" in blocked


def test_stash_is_allowed_in_a_repo_with_no_linked_worktrees(repo):
    """The hazard is a SHARED refs/stash; a lone checkout has no sibling to rob."""
    from tools.terminal_tool_guards import worktree_stash_block

    assert worktree_stash_block(
        command="git stash pop", cwd=str(repo), workdir=None, session_key="s",
    ) is None


def test_stash_listing_is_allowed_even_inside_a_worktree(repo):
    from tools.terminal_tool_guards import worktree_stash_block

    worktree = repo / ".worktrees" / "agent2"
    kbw._ensure_git_worktree(repo, worktree, "wt-t_agent2")
    assert worktree_stash_block(
        command="git stash list", cwd=str(worktree), workdir=None, session_key="s",
    ) is None


def test_non_stash_commands_skip_the_git_probe_entirely(repo, monkeypatch):
    """Cost contract: the pre-exec guard must not shell out on every command."""
    from tools import terminal_tool_guards as guards

    def _explode(*_a, **_k):
        raise AssertionError("probed git for a command that never mentions stash")

    monkeypatch.setattr(guards, "_in_linked_worktree", _explode)
    assert guards.worktree_stash_block(
        command="pytest -q", cwd=str(repo), workdir=None, session_key="s",
    ) is None


# ---------------------------------------------------------------------------
# 4. gitlock scope
# ---------------------------------------------------------------------------


def test_stale_lock_sweep_is_not_starved_by_another_repos_git(repo, tmp_path, monkeypatch):
    """A git process working elsewhere must not protect THIS repo's dead lock."""
    other = tmp_path / "elsewhere"
    other.mkdir()
    monkeypatch.setattr(gitlock, "_running_git_cwds", lambda: [other])

    lock = repo / ".git" / "index.lock"
    lock.write_text("", encoding="utf-8")
    assert gitlock.clear_stale_git_locks(repo, min_age_seconds=0) == [str(lock)]
    assert not lock.exists()


def test_a_live_git_in_this_repo_still_protects_its_lock(repo, monkeypatch):
    monkeypatch.setattr(gitlock, "_running_git_cwds", lambda: [repo])

    lock = repo / ".git" / "index.lock"
    lock.write_text("", encoding="utf-8")
    assert gitlock.clear_stale_git_locks(repo, min_age_seconds=0) == []
    assert lock.exists()


def test_a_git_process_inside_a_linked_worktree_protects_the_shared_git_dir(repo, monkeypatch):
    """Worktrees share one object store; a fetch in one must protect all of them."""
    worktree = repo / ".worktrees" / "agent3"
    kbw._ensure_git_worktree(repo, worktree, "wt-t_agent3")
    monkeypatch.setattr(gitlock, "_running_git_cwds", lambda: [worktree])

    lock = repo / ".git" / "index.lock"
    lock.write_text("", encoding="utf-8")
    assert gitlock.clear_stale_git_locks(repo, min_age_seconds=0) == []


def test_unattributable_git_processes_stay_conservative(repo, monkeypatch):
    """A process whose cwd we cannot read (AccessDenied) is reported as None and
    must keep the old protective behaviour rather than silently unlocking."""
    monkeypatch.setattr(gitlock, "_running_git_cwds", lambda: [None])

    lock = repo / ".git" / "index.lock"
    lock.write_text("", encoding="utf-8")
    assert gitlock.clear_stale_git_locks(repo, min_age_seconds=0) == []


def test_no_git_running_sweeps_normally(repo, monkeypatch):
    monkeypatch.setattr(gitlock, "_running_git_cwds", lambda: [])
    lock = repo / ".git" / "shallow.lock"
    lock.write_text("", encoding="utf-8")
    assert gitlock.clear_stale_git_locks(repo, min_age_seconds=0) == [str(lock)]


def test_running_git_cwds_is_safe_on_a_live_host():
    """Smoke the real probe: it must answer without raising, whatever is running."""
    assert isinstance(gitlock._running_git_cwds(), list)
