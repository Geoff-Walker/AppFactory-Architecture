"""
Workspace management for AppFactory pipeline runs.

Each pipeline dispatch gets a fresh workspace: a clone of the target repo into
``$APPFACTORY_WORKSPACE_ROOT/<run_id>/<repo_name>/`` (default
``/tmp/appfactory-runs/<run_id>/<repo_name>/`` on the VM), with a pre-push hook
installed that refuses pushes to ``main``/``master``. After the subprocess
exits, key artefacts (the spawn prompt, ``output.json``, the diff) are
archived to ``$APPFACTORY_ARTEFACT_ROOT/<run_id>/`` (default
``~/run-artefacts/<run_id>/``) for 7-day retention. The temp
workspace is then deleted.

Workspace pattern: Option B (clone-per-run, ephemeral). Future upgrade:
Option C (bare clone + git worktree) — see project.md.

This module is laptop-side code that executes on the VM during pipeline runs.
All default paths assume Linux (the VM); tests override via environment
variables to keep the filesystem operations confined to a tempdir.

Pre-push hook is part of the Option 1 main-push protection (see project.md
"Session 2026-04-22"). The hook is the workspace-level enforcement layer
sitting alongside the pipeline-level code guards in setup_node / merge_node /
batch_close_node. Bypassing it requires ``--no-verify``, which the Dev agent
CLAUDE.md (Mode B) explicitly bans.
"""

from __future__ import annotations

import logging
import os
import shutil
import stat
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

_DEFAULT_WORKSPACE_ROOT = "/tmp/appfactory-runs"
_DEFAULT_ARTEFACT_ROOT = os.path.expanduser("~/run-artefacts")


def _workspace_root() -> Path:
    return Path(os.environ.get("APPFACTORY_WORKSPACE_ROOT", _DEFAULT_WORKSPACE_ROOT))


def _artefact_root() -> Path:
    return Path(os.environ.get("APPFACTORY_ARTEFACT_ROOT", _DEFAULT_ARTEFACT_ROOT))


# ---------------------------------------------------------------------------
# Pre-push hook
# ---------------------------------------------------------------------------

PRE_PUSH_HOOK = """#!/usr/bin/env bash
# Installed by graphs/workspace.py — refuses pushes to main/master.
# Bypassing this hook (e.g. with --no-verify) is a deliberate breach of the
# AppFactory pipeline trust model. The Dev agent CLAUDE.md (Mode B) bans it.
while read local_ref local_sha remote_ref remote_sha; do
    if [[ "$remote_ref" == "refs/heads/main" || "$remote_ref" == "refs/heads/master" ]]; then
        echo "ERROR: appfactory pre-push hook — push to '$remote_ref' is not permitted." >&2
        echo "Branch must target an integration branch (batch/*) or a feature branch." >&2
        exit 1
    fi
done
exit 0
"""


def install_pre_push_hook(workspace: str | Path) -> Path:
    """Install the pre-push hook in ``<workspace>/.git/hooks/pre-push``.

    Returns the path to the installed hook. Caller must ensure ``<workspace>``
    is a git working tree (i.e. has a ``.git`` directory).
    """
    hooks_dir = Path(workspace) / ".git" / "hooks"
    hooks_dir.mkdir(parents=True, exist_ok=True)
    hook_path = hooks_dir / "pre-push"
    hook_path.write_text(PRE_PUSH_HOOK)
    # Make executable for owner / group / other
    hook_path.chmod(hook_path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    logger.info("install_pre_push_hook: installed at %s", hook_path)
    return hook_path


# ---------------------------------------------------------------------------
# Workspace lifecycle
# ---------------------------------------------------------------------------

def _repo_name_from_url(repo_url: str) -> str:
    """Derive the repo directory name from a clone URL (strips trailing .git)."""
    return repo_url.rstrip("/").split("/")[-1].removesuffix(".git")


def prepare_workspace(
    run_id: str,
    repo_url: str,
    integration_branch: str,
) -> Path:
    """Clone ``repo_url`` into a per-run workspace and install the pre-push hook.

    Args:
        run_id: pipeline dispatch identifier — namespaces the workspace.
        repo_url: HTTPS or SSH clone URL for the target repo.
        integration_branch: branch to check out after clone (e.g. ``batch/sprint-3``).
            Created by ``setup_node`` before this is called.

    Returns:
        Absolute path to the cloned repo root (the workspace directory).

    Raises:
        ValueError: if ``run_id`` is empty.
        subprocess.CalledProcessError: if ``git clone`` fails. Caller decides
            whether to retry, interrupt, or surface to Geoff.
    """
    if not run_id:
        raise ValueError("run_id must be a non-empty string")

    run_dir = _workspace_root() / run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    repo_name = _repo_name_from_url(repo_url)
    workspace = run_dir / repo_name

    if workspace.exists():
        logger.warning(
            "prepare_workspace: workspace %s already exists — removing for clean clone",
            workspace,
        )
        shutil.rmtree(workspace)

    logger.info(
        "prepare_workspace: cloning %s (branch %s) into %s",
        repo_url, integration_branch, workspace,
    )
    subprocess.run(
        ["git", "clone", "--branch", integration_branch, repo_url, str(workspace)],
        check=True,
        capture_output=True,
        text=True,
    )

    install_pre_push_hook(workspace)
    return workspace


def archive_artefacts(workspace: str | Path, run_id: str) -> Path:
    """Copy key artefacts from a workspace to the per-run artefact directory.

    Artefacts copied (best-effort — missing files are skipped, never raise):
        - ``output.json``  — the agent's structured status report (always wanted)
        - ``prompt.txt``   — the spawn prompt, if the caller wrote one
        - ``diff.patch``   — captured via ``git diff HEAD --no-color``

    Returns:
        Path to the artefact directory. Returned even if some copies failed —
        partial preservation is better than none for debugging.
    """
    workspace = Path(workspace)
    artefact_dir = _artefact_root() / run_id

    try:
        artefact_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        logger.warning(
            "archive_artefacts: could not create %s — %s. Artefacts will not be preserved.",
            artefact_dir, exc,
        )
        return artefact_dir

    # Copy output.json and prompt.txt if present
    for filename in ("output.json", "prompt.txt"):
        src = workspace / filename
        if src.exists():
            try:
                shutil.copy2(src, artefact_dir / filename)
                logger.info("archive_artefacts: copied %s", filename)
            except OSError as exc:
                logger.warning("archive_artefacts: could not copy %s — %s", filename, exc)

    # Capture diff against HEAD for visibility on what the agent changed
    try:
        diff = subprocess.run(
            ["git", "-C", str(workspace), "diff", "HEAD", "--no-color"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if diff.returncode == 0 and diff.stdout:
            (artefact_dir / "diff.patch").write_text(diff.stdout)
            logger.info("archive_artefacts: captured diff.patch")
    except (subprocess.TimeoutExpired, OSError) as exc:
        logger.warning("archive_artefacts: could not capture diff — %s", exc)

    return artefact_dir


def cleanup(workspace: str | Path) -> None:
    """Remove the workspace directory tree. Failures logged but never raised.

    Call after ``archive_artefacts`` to free disk. The workspace root
    (``$APPFACTORY_WORKSPACE_ROOT``) is intentionally not removed — only the
    per-run subdirectory.
    """
    workspace = Path(workspace)
    if not workspace.exists():
        logger.debug("cleanup: %s already absent — nothing to do", workspace)
        return
    try:
        shutil.rmtree(workspace)
        logger.info("cleanup: removed %s", workspace)
    except OSError as exc:
        logger.warning("cleanup: could not remove %s — %s", workspace, exc)
