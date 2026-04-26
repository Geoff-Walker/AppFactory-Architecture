"""
Staging deploy helper for AppFactory pipeline.

``batch_close_node`` calls into here at the end of an iterative_dev run to
update the staging environment for the target project. The deploy SSHes from
the AppFactory VM to the server as root and runs the canonical
``docker compose pull && docker compose up -d`` against the project's staging
stack directory.

**Trust model:**
- The VM SSH key is added to ``/root/.ssh/authorized_keys`` on the server.
- The blast-radius concern is addressed by code-level guards in this module:
  - The remote path is constrained to ``$APPFACTORY_STAGING_BASE/*-staging/``
  - The remote command is constrained to the literal string template
    ``cd <path> && docker compose pull && docker compose up -d`` — no shell
    interpolation from any caller input.

Future hardening: replace root SSH with a dedicated ``appfactory-deploy``
user scoped via sudoers to docker compose in staging paths only.

Configuration (environment variables):
  APPFACTORY_DEPLOY_HOST   — SSH target, e.g. ``root@<server>``
  APPFACTORY_STAGING_BASE  — base path for staging stacks,
                             e.g. ``/mnt/<pool>/apps``
"""

from __future__ import annotations

import logging
import os
import re
import subprocess

logger = logging.getLogger(__name__)

# SSH target for the server — set via env var, never derived from caller input.
TRUENAS_HOST = os.environ.get("APPFACTORY_DEPLOY_HOST", "root@<server>")

# Base path for staging stacks on the server.
_STAGING_BASE = os.environ.get("APPFACTORY_STAGING_BASE", "/mnt/pool/apps").rstrip("/")

# Per-stack deploy timeout. docker compose pull on a chunky image can take
# a few minutes; 10 minutes is the upper bound for "something is wrong".
DEPLOY_TIMEOUT_SECONDS = 10 * 60

# Whitelist pattern for staging paths. Anything outside STAGING_BASE is rejected.
# Pattern: <base>/<repo-name>-staging[/]
_STAGING_PATH_PATTERN = re.compile(
    r"^" + re.escape(_STAGING_BASE) + r"/[a-z0-9][a-z0-9._-]*-staging/?$"
)


class DeployFailed(Exception):
    """Raised when the SSH deploy command exits non-zero or otherwise fails."""


def is_whitelisted_staging_path(path: str) -> bool:
    """True iff ``path`` matches the staging-stack whitelist pattern.

    Acceptable shape: ``$APPFACTORY_STAGING_BASE/<name>-staging`` with an
    optional trailing slash. ``<name>`` must be a non-empty lowercase
    identifier (alphanumeric, dots, underscores, hyphens).
    """
    if not isinstance(path, str):
        return False
    return _STAGING_PATH_PATTERN.match(path) is not None


def staging_path_for_repo(repo: str) -> str:
    """Derive the canonical staging stack path for a repo slug.

    ``owner/my-project`` → ``<APPFACTORY_STAGING_BASE>/my-project-staging``.

    Convention: lowercase the repo name, append ``-staging``, drop into
    ``$APPFACTORY_STAGING_BASE``. Caller should pass the result to
    ``deploy_staging`` which re-validates against the whitelist.
    """
    if not repo:
        raise ValueError("staging_path_for_repo requires a non-empty repo slug")
    repo_name = repo.rstrip("/").split("/")[-1].lower()
    return f"{_STAGING_BASE}/{repo_name}-staging"


def deploy_staging(staging_path: str, *, ssh_runner=subprocess.run) -> None:
    """Run ``docker compose pull && docker compose up -d`` at ``staging_path`` on TrueNAS.

    The remote command is built from a fixed template. The path is validated
    against the whitelist before any SSH call is made. The command itself
    is constructed only from the validated path — no caller-controlled
    string ever reaches the remote shell uninterpolated.

    Args:
        staging_path: absolute path to the project's staging compose stack
            on TrueNAS. Must satisfy ``is_whitelisted_staging_path``.
        ssh_runner: injectable for tests; defaults to ``subprocess.run``.

    Raises:
        ValueError: if ``staging_path`` is not whitelisted.
        DeployFailed: if the SSH command exits non-zero, times out, or the
            ssh binary is unavailable.
    """
    if not is_whitelisted_staging_path(staging_path):
        raise ValueError(
            f"deploy_staging refuses non-whitelisted path: {staging_path!r}. "
            f"Must match {_STAGING_BASE}/<name>-staging."
        )

    # Strip any trailing slash so the cd command is clean.
    path_clean = staging_path.rstrip("/")
    remote_command = f"cd {path_clean} && docker compose pull && docker compose up -d"

    logger.info("deploy_staging: ssh %s '%s'", TRUENAS_HOST, remote_command)

    try:
        result = ssh_runner(
            [
                "ssh",
                # Accept the host key on first connect so a headless pipeline
                # doesn't hang on an interactive yes/no prompt. The VM's
                # authorized_keys on TrueNAS is the real trust boundary;
                # TOFU on a private virbr0 bridge is acceptable.
                "-o", "StrictHostKeyChecking=accept-new",
                TRUENAS_HOST,
                remote_command,
            ],
            capture_output=True,
            text=True,
            timeout=DEPLOY_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as exc:
        raise DeployFailed(
            f"ssh deploy timed out after {DEPLOY_TIMEOUT_SECONDS}s for {staging_path}"
        ) from exc
    except FileNotFoundError as exc:
        raise DeployFailed(
            "ssh binary not found in PATH on the AppFactory VM"
        ) from exc

    if result.returncode != 0:
        stderr = (result.stderr or "").strip()[:500] or "no stderr"
        raise DeployFailed(
            f"ssh deploy to {staging_path} exited {result.returncode}: {stderr}"
        )

    logger.info("deploy_staging: completed successfully for %s", staging_path)
