"""
Unit tests for graphs/staging_deploy.py — whitelisted SSH deploy to TrueNAS.

All SSH calls are mocked via the ssh_runner injection point.

Coverage:
  - Whitelist pattern: accepts, rejects correctly
  - Path derivation from repo slug
  - deploy_staging: invokes ssh with the correct command, raises on bad path,
    raises DeployFailed on non-zero exit / timeout / missing ssh binary
"""

from __future__ import annotations

import subprocess
from unittest.mock import MagicMock

import pytest


# ---------------------------------------------------------------------------
# Whitelist
# ---------------------------------------------------------------------------

class TestIsWhitelistedStagingPath:
    def test_accepts_canonical_staging_path(self):
        from graphs.staging_deploy import is_whitelisted_staging_path
        assert is_whitelisted_staging_path("/mnt/pool/apps/familycookbook-staging")

    def test_accepts_with_trailing_slash(self):
        from graphs.staging_deploy import is_whitelisted_staging_path
        assert is_whitelisted_staging_path("/mnt/pool/apps/tom-staging/")

    def test_rejects_non_staging_suffix(self):
        from graphs.staging_deploy import is_whitelisted_staging_path
        assert not is_whitelisted_staging_path("/mnt/pool/apps/familycookbook")

    def test_rejects_prod_stack(self):
        """Critical — the whitelist must not accept prod stacks."""
        from graphs.staging_deploy import is_whitelisted_staging_path
        assert not is_whitelisted_staging_path("/mnt/pool/apps/familycookbook")
        assert not is_whitelisted_staging_path("/mnt/pool/apps/familycookbook-prod")

    def test_rejects_outside_apps_dir(self):
        from graphs.staging_deploy import is_whitelisted_staging_path
        assert not is_whitelisted_staging_path("/etc/something-staging")
        assert not is_whitelisted_staging_path("/root/rogue-staging")
        assert not is_whitelisted_staging_path("/mnt/nothing-staging")

    def test_rejects_empty_name(self):
        """`/mnt/pool/apps/-staging` must not match."""
        from graphs.staging_deploy import is_whitelisted_staging_path
        assert not is_whitelisted_staging_path("/mnt/pool/apps/-staging")

    def test_rejects_path_traversal_attempts(self):
        from graphs.staging_deploy import is_whitelisted_staging_path
        assert not is_whitelisted_staging_path(
            "/mnt/pool/apps/foo-staging/../../../etc"
        )
        assert not is_whitelisted_staging_path(
            "/mnt/pool/apps/foo-staging; rm -rf /"
        )

    def test_rejects_non_string(self):
        from graphs.staging_deploy import is_whitelisted_staging_path
        assert not is_whitelisted_staging_path(None)  # type: ignore[arg-type]
        assert not is_whitelisted_staging_path(123)   # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# staging_path_for_repo
# ---------------------------------------------------------------------------

class TestStagingPathForRepo:
    def test_lowercases_repo_name(self):
        from graphs.staging_deploy import staging_path_for_repo
        assert (
            staging_path_for_repo("Geoff-Walker/FamilyCookbook")
            == "/mnt/pool/apps/familycookbook-staging"
        )

    def test_bare_repo_name(self):
        from graphs.staging_deploy import staging_path_for_repo
        assert (
            staging_path_for_repo("tom")
            == "/mnt/pool/apps/tom-staging"
        )

    def test_empty_repo_raises(self):
        from graphs.staging_deploy import staging_path_for_repo
        with pytest.raises(ValueError):
            staging_path_for_repo("")


# ---------------------------------------------------------------------------
# deploy_staging
# ---------------------------------------------------------------------------

class TestDeployStaging:
    def _ok_result(self) -> MagicMock:
        r = MagicMock()
        r.returncode = 0
        r.stderr = ""
        return r

    def test_rejects_non_whitelisted_path(self):
        from graphs.staging_deploy import deploy_staging
        with pytest.raises(ValueError, match="non-whitelisted"):
            deploy_staging("/root/evil")

    def test_invokes_ssh_with_canonical_command(self):
        from graphs.staging_deploy import deploy_staging, TRUENAS_HOST

        runner = MagicMock(return_value=self._ok_result())
        deploy_staging(
            "/mnt/pool/apps/familycookbook-staging",
            ssh_runner=runner,
        )

        args = runner.call_args[0][0]
        assert args[0] == "ssh"
        # StrictHostKeyChecking=accept-new so a headless pipeline doesn't
        # hang on an interactive host-key prompt on first connect.
        assert "-o" in args
        assert "StrictHostKeyChecking=accept-new" in args
        assert TRUENAS_HOST in args
        # Remote command is fixed-template: cd <path> && docker compose pull && docker compose up -d
        remote_command = args[-1]
        assert "cd /mnt/pool/apps/familycookbook-staging" in remote_command
        assert "docker compose pull" in remote_command
        assert "docker compose up -d" in remote_command

    def test_strips_trailing_slash_before_cd(self):
        from graphs.staging_deploy import deploy_staging

        runner = MagicMock(return_value=self._ok_result())
        deploy_staging(
            "/mnt/pool/apps/tom-staging/",
            ssh_runner=runner,
        )
        remote = runner.call_args[0][0][-1]
        assert "cd /mnt/pool/apps/tom-staging " in remote  # no trailing slash

    def test_nonzero_exit_raises_deploy_failed(self):
        from graphs.staging_deploy import deploy_staging, DeployFailed

        bad = MagicMock()
        bad.returncode = 1
        bad.stderr = "docker: command not found"
        runner = MagicMock(return_value=bad)

        with pytest.raises(DeployFailed, match="exited 1"):
            deploy_staging(
                "/mnt/pool/apps/familycookbook-staging",
                ssh_runner=runner,
            )

    def test_timeout_raises_deploy_failed(self):
        from graphs.staging_deploy import deploy_staging, DeployFailed

        def timeout_runner(*_args, **_kwargs):
            raise subprocess.TimeoutExpired(cmd="ssh", timeout=600)

        with pytest.raises(DeployFailed, match="timed out"):
            deploy_staging(
                "/mnt/pool/apps/tom-staging",
                ssh_runner=timeout_runner,
            )

    def test_ssh_binary_missing_raises_deploy_failed(self):
        from graphs.staging_deploy import deploy_staging, DeployFailed

        def missing_ssh(*_args, **_kwargs):
            raise FileNotFoundError("ssh")

        with pytest.raises(DeployFailed, match="ssh binary not found"):
            deploy_staging(
                "/mnt/pool/apps/tom-staging",
                ssh_runner=missing_ssh,
            )
