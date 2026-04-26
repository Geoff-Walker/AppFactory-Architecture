"""
Unit tests for graphs/workspace.py — clone-per-run workspaces with pre-push hook.

Tests cover:
  - install_pre_push_hook: hook file present, executable, refuses main/master
  - prepare_workspace: invokes git clone with the correct args and branch,
    installs the hook, returns the workspace path
  - archive_artefacts: copies present files, skips missing ones, never raises
  - cleanup: removes tree, handles already-absent gracefully

Filesystem operations run against a tempdir set via APPFACTORY_WORKSPACE_ROOT
and APPFACTORY_ARTEFACT_ROOT environment variables. Subprocess calls
(git clone, git diff) are mocked — no actual git operations occur.
"""

from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest


# ---------------------------------------------------------------------------
# Fixtures — confine workspace + artefact roots to a tempdir per test
# ---------------------------------------------------------------------------

@pytest.fixture
def temp_roots(tmp_path, monkeypatch):
    """Override the workspace and artefact roots to a per-test tempdir."""
    workspace_root = tmp_path / "workspaces"
    artefact_root = tmp_path / "artefacts"
    monkeypatch.setenv("APPFACTORY_WORKSPACE_ROOT", str(workspace_root))
    monkeypatch.setenv("APPFACTORY_ARTEFACT_ROOT", str(artefact_root))
    return {"workspace_root": workspace_root, "artefact_root": artefact_root}


# ---------------------------------------------------------------------------
# install_pre_push_hook
# ---------------------------------------------------------------------------

class TestInstallPrePushHook:
    def test_hook_file_created(self, tmp_path):
        from graphs.workspace import install_pre_push_hook

        # Simulate a workspace that has a .git directory
        (tmp_path / ".git").mkdir()
        hook_path = install_pre_push_hook(tmp_path)

        assert hook_path.exists()
        assert hook_path == tmp_path / ".git" / "hooks" / "pre-push"

    @pytest.mark.skipif(
        os.name == "nt",
        reason="NTFS does not honour POSIX execute bits; this assertion is meaningful only on Linux (the VM)",
    )
    def test_hook_is_executable(self, tmp_path):
        from graphs.workspace import install_pre_push_hook

        (tmp_path / ".git").mkdir()
        hook_path = install_pre_push_hook(tmp_path)

        # Owner execute bit must be set (the critical bit on Linux — VM target)
        assert hook_path.stat().st_mode & stat.S_IXUSR

    def test_hook_content_blocks_main_and_master(self, tmp_path):
        from graphs.workspace import install_pre_push_hook

        (tmp_path / ".git").mkdir()
        hook_path = install_pre_push_hook(tmp_path)
        content = hook_path.read_text()

        assert "refs/heads/main" in content
        assert "refs/heads/master" in content
        assert "exit 1" in content

    def test_hooks_dir_created_if_absent(self, tmp_path):
        """If .git exists but .git/hooks does not, the hook installer creates it."""
        from graphs.workspace import install_pre_push_hook

        (tmp_path / ".git").mkdir()
        # Note: .git/hooks intentionally not created
        hook_path = install_pre_push_hook(tmp_path)

        assert hook_path.parent.is_dir()
        assert hook_path.exists()


# ---------------------------------------------------------------------------
# prepare_workspace
# ---------------------------------------------------------------------------

class TestPrepareWorkspace:
    def test_invokes_git_clone_with_correct_args(self, temp_roots):
        from graphs import workspace as ws_mod

        with patch("graphs.workspace.subprocess.run") as mock_run, \
             patch("graphs.workspace.install_pre_push_hook") as mock_hook:
            # Simulate git clone creating the directory
            def fake_run(args, **kwargs):
                # The clone destination is the last positional arg before kwargs
                dest = Path(args[-1])
                dest.mkdir(parents=True, exist_ok=True)
                (dest / ".git").mkdir()
                return MagicMock(returncode=0)

            mock_run.side_effect = fake_run

            result = ws_mod.prepare_workspace(
                run_id="run-abc",
                repo_url="https://github.com/Geoff-Walker/FamilyCookbook.git",
                integration_branch="batch/sprint-3",
            )

        assert mock_run.called
        call_args = mock_run.call_args[0][0]
        assert call_args[0:3] == ["git", "clone", "--branch"]
        assert "batch/sprint-3" in call_args
        assert "https://github.com/Geoff-Walker/FamilyCookbook.git" in call_args
        assert mock_hook.called

        # Workspace path should sit under the configured root and have .git suffix stripped
        assert "FamilyCookbook" in str(result)
        assert "run-abc" in str(result)
        assert ".git" not in result.name

    def test_strips_dot_git_suffix_from_repo_name(self, temp_roots):
        from graphs.workspace import _repo_name_from_url

        assert _repo_name_from_url("https://github.com/Geoff-Walker/tom.git") == "tom"
        assert _repo_name_from_url("https://github.com/Geoff-Walker/tom") == "tom"
        assert _repo_name_from_url("https://github.com/Geoff-Walker/appfactory.git/") == "appfactory"

    def test_empty_run_id_raises(self):
        from graphs.workspace import prepare_workspace

        with pytest.raises(ValueError, match="run_id"):
            prepare_workspace(run_id="", repo_url="x", integration_branch="batch/sprint-1")

    def test_existing_workspace_is_removed_first(self, temp_roots):
        """If the workspace path already exists, it is wiped before re-cloning — ensures clean state."""
        from graphs import workspace as ws_mod

        run_dir = temp_roots["workspace_root"] / "run-1"
        run_dir.mkdir(parents=True)
        stale = run_dir / "tom"
        stale.mkdir()
        (stale / "stale_file.txt").write_text("leftover from previous run")

        with patch("graphs.workspace.subprocess.run") as mock_run, \
             patch("graphs.workspace.install_pre_push_hook"):
            def fake_run(args, **kwargs):
                dest = Path(args[-1])
                dest.mkdir(parents=True, exist_ok=True)
                (dest / ".git").mkdir()
                return MagicMock(returncode=0)
            mock_run.side_effect = fake_run

            ws_mod.prepare_workspace(
                run_id="run-1",
                repo_url="https://github.com/Geoff-Walker/tom.git",
                integration_branch="batch/sprint-1",
            )

        # Stale file should be gone (the directory was removed and re-created by the fake clone)
        assert not (stale / "stale_file.txt").exists()

    def test_clone_failure_propagates(self, temp_roots):
        """Caller is expected to handle git failures — prepare_workspace does not swallow."""
        from graphs.workspace import prepare_workspace

        with patch(
            "graphs.workspace.subprocess.run",
            side_effect=subprocess.CalledProcessError(128, "git clone"),
        ):
            with pytest.raises(subprocess.CalledProcessError):
                prepare_workspace(
                    run_id="run-fail",
                    repo_url="https://github.com/Geoff-Walker/nonexistent.git",
                    integration_branch="main",
                )


# ---------------------------------------------------------------------------
# archive_artefacts
# ---------------------------------------------------------------------------

class TestArchiveArtefacts:
    def _make_workspace(self, tmp_path: Path) -> Path:
        """Create a workspace with a .git dir so subprocess git diff doesn't error catastrophically."""
        ws = tmp_path / "workspace"
        ws.mkdir()
        (ws / ".git").mkdir()
        return ws

    def test_copies_output_json_when_present(self, temp_roots, tmp_path):
        from graphs.workspace import archive_artefacts

        ws = self._make_workspace(tmp_path)
        (ws / "output.json").write_text('{"status": "COMPLETED", "pr_url": "https://x"}')

        with patch("graphs.workspace.subprocess.run", return_value=MagicMock(returncode=0, stdout="")):
            artefact_dir = archive_artefacts(ws, "run-1")

        assert (artefact_dir / "output.json").exists()
        assert "COMPLETED" in (artefact_dir / "output.json").read_text()

    def test_copies_prompt_when_present(self, temp_roots, tmp_path):
        from graphs.workspace import archive_artefacts

        ws = self._make_workspace(tmp_path)
        (ws / "prompt.txt").write_text("Implement WAL-42")

        with patch("graphs.workspace.subprocess.run", return_value=MagicMock(returncode=0, stdout="")):
            artefact_dir = archive_artefacts(ws, "run-1")

        assert (artefact_dir / "prompt.txt").exists()

    def test_skips_missing_files_silently(self, temp_roots, tmp_path):
        """If output.json / prompt.txt aren't present, archive_artefacts does not raise."""
        from graphs.workspace import archive_artefacts

        ws = self._make_workspace(tmp_path)
        # No output.json, no prompt.txt

        with patch("graphs.workspace.subprocess.run", return_value=MagicMock(returncode=0, stdout="")):
            artefact_dir = archive_artefacts(ws, "run-2")

        assert artefact_dir.exists()
        assert not (artefact_dir / "output.json").exists()
        assert not (artefact_dir / "prompt.txt").exists()

    def test_captures_diff_when_git_diff_succeeds(self, temp_roots, tmp_path):
        from graphs.workspace import archive_artefacts

        ws = self._make_workspace(tmp_path)
        diff_text = "diff --git a/file.py b/file.py\n+added line"

        with patch(
            "graphs.workspace.subprocess.run",
            return_value=MagicMock(returncode=0, stdout=diff_text),
        ):
            artefact_dir = archive_artefacts(ws, "run-3")

        diff_file = artefact_dir / "diff.patch"
        assert diff_file.exists()
        assert "added line" in diff_file.read_text()

    def test_no_diff_file_when_git_diff_empty(self, temp_roots, tmp_path):
        from graphs.workspace import archive_artefacts

        ws = self._make_workspace(tmp_path)

        with patch("graphs.workspace.subprocess.run", return_value=MagicMock(returncode=0, stdout="")):
            artefact_dir = archive_artefacts(ws, "run-4")

        assert not (artefact_dir / "diff.patch").exists()

    def test_diff_failure_does_not_raise(self, temp_roots, tmp_path):
        from graphs.workspace import archive_artefacts

        ws = self._make_workspace(tmp_path)
        (ws / "output.json").write_text('{"status": "COMPLETED"}')

        # git diff times out — must not bring down the artefact archive
        with patch(
            "graphs.workspace.subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd="git diff", timeout=30),
        ):
            artefact_dir = archive_artefacts(ws, "run-5")

        # output.json was copied before diff was attempted, so it should still be there
        assert (artefact_dir / "output.json").exists()
        assert not (artefact_dir / "diff.patch").exists()


# ---------------------------------------------------------------------------
# cleanup
# ---------------------------------------------------------------------------

class TestCleanup:
    def test_removes_existing_workspace(self, tmp_path):
        from graphs.workspace import cleanup

        ws = tmp_path / "workspace"
        ws.mkdir()
        (ws / "file.txt").write_text("data")

        cleanup(ws)
        assert not ws.exists()

    def test_handles_missing_workspace_gracefully(self, tmp_path):
        from graphs.workspace import cleanup

        # Workspace was never created — cleanup must not raise
        cleanup(tmp_path / "never_existed")
