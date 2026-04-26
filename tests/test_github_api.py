"""
Unit tests for graphs/github_api.py — GitHub REST API helpers.

All HTTP calls are mocked. No network. The point of these tests is to lock in
the URL shapes, header structure, payload format, and error semantics that
the iterative_dev pipeline nodes depend on.

Coverage:
  - gh_headers: PAT loading, missing-PAT raises
  - next_sprint_number: scans matching-refs, picks max+1, handles empty + 404
  - get_branch_sha: returns object.sha from refs response
  - create_branch: refuses main/master (code-level guard), POSTs correct body
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest
import requests


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def pat_set(monkeypatch):
    """Set the AppFactory PAT env var so gh_headers() succeeds."""
    monkeypatch.setenv("GITHUB_APPFACTORY_PAT", "ghp_test_token_12345")


def _mock_response(status_code: int = 200, json_data=None) -> MagicMock:
    """Build a mock requests.Response."""
    response = MagicMock(spec=requests.Response)
    response.status_code = status_code
    response.json.return_value = json_data if json_data is not None else {}
    if status_code >= 400:
        response.raise_for_status.side_effect = requests.HTTPError(f"{status_code}")
    else:
        response.raise_for_status.return_value = None
    return response


# ---------------------------------------------------------------------------
# gh_headers
# ---------------------------------------------------------------------------

class TestGhHeaders:
    def test_returns_bearer_token_from_env(self, pat_set):
        from graphs.github_api import gh_headers

        headers = gh_headers()
        assert headers["Authorization"] == "Bearer ghp_test_token_12345"
        assert headers["Accept"] == "application/vnd.github+json"
        assert headers["X-GitHub-Api-Version"] == "2022-11-28"

    def test_missing_pat_raises_runtime_error(self, monkeypatch):
        from graphs.github_api import gh_headers

        monkeypatch.delenv("GITHUB_APPFACTORY_PAT", raising=False)
        with pytest.raises(RuntimeError, match="GITHUB_APPFACTORY_PAT"):
            gh_headers()


# ---------------------------------------------------------------------------
# next_sprint_number
# ---------------------------------------------------------------------------

class TestNextSprintNumber:
    def test_returns_one_when_no_batch_branches_exist(self, pat_set):
        from graphs.github_api import next_sprint_number

        # Empty list response — no matching refs
        with patch("graphs.github_api.requests.get", return_value=_mock_response(200, [])) as mock_get:
            assert next_sprint_number("Geoff-Walker/tom") == 1

        # Verify the URL hits the matching-refs endpoint with the right prefix
        called_url = mock_get.call_args[0][0]
        assert "matching-refs/heads/batch/sprint-" in called_url
        assert "Geoff-Walker/tom" in called_url

    def test_returns_one_on_404(self, pat_set):
        """Be defensive — if GitHub returns 404 for matching-refs, treat as 'no matches'."""
        from graphs.github_api import next_sprint_number

        with patch("graphs.github_api.requests.get", return_value=_mock_response(404)):
            assert next_sprint_number("Geoff-Walker/tom") == 1

    def test_picks_max_plus_one_from_existing_refs(self, pat_set):
        from graphs.github_api import next_sprint_number

        refs = [
            {"ref": "refs/heads/batch/sprint-1", "object": {"sha": "a"}},
            {"ref": "refs/heads/batch/sprint-3", "object": {"sha": "b"}},
            {"ref": "refs/heads/batch/sprint-2", "object": {"sha": "c"}},
        ]
        with patch("graphs.github_api.requests.get", return_value=_mock_response(200, refs)):
            assert next_sprint_number("Geoff-Walker/FamilyCookbook") == 4

    def test_ignores_unrelated_batch_refs(self, pat_set):
        """A ref like 'batch/sprint-3-hotfix' must NOT be parsed as sprint 3."""
        from graphs.github_api import next_sprint_number

        refs = [
            {"ref": "refs/heads/batch/sprint-2", "object": {"sha": "a"}},
            {"ref": "refs/heads/batch/sprint-5-hotfix", "object": {"sha": "b"}},
            {"ref": "refs/heads/batch/something-else", "object": {"sha": "c"}},
        ]
        with patch("graphs.github_api.requests.get", return_value=_mock_response(200, refs)):
            assert next_sprint_number("Geoff-Walker/tom") == 3  # sprint-2 was the only match

    def test_http_error_propagates(self, pat_set):
        from graphs.github_api import next_sprint_number

        with patch("graphs.github_api.requests.get", return_value=_mock_response(500)):
            with pytest.raises(requests.HTTPError):
                next_sprint_number("Geoff-Walker/tom")


# ---------------------------------------------------------------------------
# get_branch_sha
# ---------------------------------------------------------------------------

class TestGetBranchSha:
    def test_returns_object_sha_from_response(self, pat_set):
        from graphs.github_api import get_branch_sha

        response_data = {
            "ref": "refs/heads/main",
            "object": {"sha": "abc123def456789", "type": "commit"},
        }
        with patch("graphs.github_api.requests.get", return_value=_mock_response(200, response_data)) as mock_get:
            sha = get_branch_sha("Geoff-Walker/tom", "main")

        assert sha == "abc123def456789"
        called_url = mock_get.call_args[0][0]
        assert "/repos/Geoff-Walker/tom/git/refs/heads/main" in called_url

    def test_404_propagates(self, pat_set):
        """Branch not found → HTTPError. Caller decides handling."""
        from graphs.github_api import get_branch_sha

        with patch("graphs.github_api.requests.get", return_value=_mock_response(404)):
            with pytest.raises(requests.HTTPError):
                get_branch_sha("Geoff-Walker/tom", "nonexistent-branch")


# ---------------------------------------------------------------------------
# create_branch
# ---------------------------------------------------------------------------

class TestCreateBranch:
    def test_main_target_raises_value_error(self, pat_set):
        """Code-level guard: never create or overwrite main."""
        from graphs.github_api import create_branch

        with pytest.raises(ValueError, match="main"):
            create_branch("Geoff-Walker/tom", "main", "abc123")

    def test_master_target_raises_value_error(self, pat_set):
        """Same guard for master."""
        from graphs.github_api import create_branch

        with pytest.raises(ValueError, match="master"):
            create_branch("Geoff-Walker/tom", "master", "abc123")

    def test_posts_correct_body_for_batch_branch(self, pat_set):
        from graphs.github_api import create_branch

        with patch("graphs.github_api.requests.post", return_value=_mock_response(201)) as mock_post:
            create_branch("Geoff-Walker/FamilyCookbook", "batch/sprint-3", "deadbeef123")

        call_kwargs = mock_post.call_args.kwargs
        body = call_kwargs["json"]
        assert body == {"ref": "refs/heads/batch/sprint-3", "sha": "deadbeef123"}

        called_url = mock_post.call_args[0][0]
        assert "/repos/Geoff-Walker/FamilyCookbook/git/refs" in called_url

    def test_http_error_propagates(self, pat_set):
        """422 (branch already exists) or any other API error surfaces to caller."""
        from graphs.github_api import create_branch

        with patch("graphs.github_api.requests.post", return_value=_mock_response(422)):
            with pytest.raises(requests.HTTPError):
                create_branch("Geoff-Walker/tom", "batch/sprint-1", "abc123")


# ---------------------------------------------------------------------------
# parse_pr_number
# ---------------------------------------------------------------------------

class TestParsePrNumber:
    def test_extracts_number_from_standard_url(self):
        from graphs.github_api import parse_pr_number
        assert parse_pr_number("https://github.com/Geoff-Walker/tom/pull/42") == 42

    def test_extracts_number_from_url_with_trailing_slash(self):
        from graphs.github_api import parse_pr_number
        assert parse_pr_number("https://github.com/x/y/pull/123/") == 123

    def test_extracts_number_from_url_with_files_suffix(self):
        from graphs.github_api import parse_pr_number
        assert parse_pr_number("https://github.com/x/y/pull/7/files") == 7

    def test_returns_none_for_non_pr_url(self):
        from graphs.github_api import parse_pr_number
        assert parse_pr_number("https://github.com/x/y/issues/42") is None

    def test_returns_none_for_empty_string(self):
        from graphs.github_api import parse_pr_number
        assert parse_pr_number("") is None
        assert parse_pr_number(None) is None  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# get_pr
# ---------------------------------------------------------------------------

class TestGetPr:
    def test_returns_pr_data(self, pat_set):
        from graphs.github_api import get_pr

        pr_data = {"head": {"sha": "abc"}, "base": {"ref": "batch/sprint-1"}, "title": "x"}
        with patch("graphs.github_api.requests.get", return_value=_mock_response(200, pr_data)) as mock_get:
            result = get_pr("Geoff-Walker/tom", 42)

        assert result == pr_data
        called_url = mock_get.call_args[0][0]
        assert "/repos/Geoff-Walker/tom/pulls/42" in called_url


# ---------------------------------------------------------------------------
# _classify_check_runs
# ---------------------------------------------------------------------------

class TestClassifyCheckRuns:
    def test_empty_list_returns_none(self):
        from graphs.github_api import _classify_check_runs
        assert _classify_check_runs([]) == "none"

    def test_all_success_returns_all_pass(self):
        from graphs.github_api import _classify_check_runs
        runs = [
            {"status": "completed", "conclusion": "success"},
            {"status": "completed", "conclusion": "success"},
        ]
        assert _classify_check_runs(runs) == "all_pass"

    def test_neutral_and_skipped_count_as_pass(self):
        from graphs.github_api import _classify_check_runs
        runs = [
            {"status": "completed", "conclusion": "neutral"},
            {"status": "completed", "conclusion": "skipped"},
        ]
        assert _classify_check_runs(runs) == "all_pass"

    def test_any_failure_returns_some_failed(self):
        from graphs.github_api import _classify_check_runs
        runs = [
            {"status": "completed", "conclusion": "success"},
            {"status": "completed", "conclusion": "failure"},
        ]
        assert _classify_check_runs(runs) == "some_failed"

    def test_cancelled_counts_as_failed(self):
        from graphs.github_api import _classify_check_runs
        runs = [{"status": "completed", "conclusion": "cancelled"}]
        assert _classify_check_runs(runs) == "some_failed"

    def test_in_progress_returns_still_running(self):
        from graphs.github_api import _classify_check_runs
        runs = [
            {"status": "completed", "conclusion": "success"},
            {"status": "in_progress", "conclusion": None},
        ]
        assert _classify_check_runs(runs) == "still_running"


# ---------------------------------------------------------------------------
# wait_for_checks (uses injectable sleep / now to avoid wall-clock waits)
# ---------------------------------------------------------------------------

class TestWaitForChecks:
    def _checks_response(self, runs: list) -> MagicMock:
        return _mock_response(200, {"total_count": len(runs), "check_runs": runs})

    def test_returns_when_all_pass(self, pat_set):
        from graphs.github_api import wait_for_checks

        runs = [{"status": "completed", "conclusion": "success"}]
        with patch("graphs.github_api.requests.get", return_value=self._checks_response(runs)):
            wait_for_checks("repo", "sha", sleep=lambda *_: None, now=lambda: 0.0)

    def test_raises_ci_failed_on_failure(self, pat_set):
        from graphs.github_api import wait_for_checks, CIFailed

        runs = [{"status": "completed", "conclusion": "failure", "name": "build"}]
        with patch("graphs.github_api.requests.get", return_value=self._checks_response(runs)):
            with pytest.raises(CIFailed, match="build"):
                wait_for_checks("repo", "sha", sleep=lambda *_: None, now=lambda: 0.0)

    def test_polls_until_settled(self, pat_set):
        """First poll returns in_progress, second returns success — function loops until stable."""
        from graphs.github_api import wait_for_checks

        responses = [
            self._checks_response([{"status": "in_progress", "conclusion": None}]),
            self._checks_response([{"status": "completed", "conclusion": "success"}]),
        ]
        with patch("graphs.github_api.requests.get", side_effect=responses):
            wait_for_checks("repo", "sha", sleep=lambda *_: None, now=lambda: 0.0, poll_interval=0)

    def test_raises_ci_timeout_when_still_running_past_deadline(self, pat_set):
        from graphs.github_api import wait_for_checks, CITimeout

        # Always returns in_progress; injected clock advances past timeout immediately.
        clock = [0.0]

        def fake_now():
            clock[0] += 100  # each call advances 100s; with timeout=10 we exceed quickly
            return clock[0]

        runs = [{"status": "in_progress", "conclusion": None}]
        with patch("graphs.github_api.requests.get", return_value=self._checks_response(runs)):
            with pytest.raises(CITimeout):
                wait_for_checks(
                    "repo", "sha",
                    sleep=lambda *_: None,
                    now=fake_now,
                    timeout=10,
                    poll_interval=0,
                )

    def test_no_checks_after_grace_returns_successfully(self, pat_set):
        """Repo has no CI configured — wait_for_checks returns after grace period elapses."""
        from graphs.github_api import wait_for_checks

        # Simulated clock that advances past the grace window between iterations
        clock = [0.0]

        def fake_now():
            current = clock[0]
            clock[0] += 100  # each call jumps forward 100s
            return current

        with patch("graphs.github_api.requests.get", return_value=self._checks_response([])):
            wait_for_checks(
                "repo", "sha",
                sleep=lambda *_: None,
                now=fake_now,
                no_checks_grace=60,  # 60s grace; advances past on second check
                poll_interval=0,
            )


# ---------------------------------------------------------------------------
# wait_for_mergeable — polls PR.mergeable_state (replaces wait_for_checks on the
# merge_node gate because personal-account fine-grained PATs cannot grant Checks)
# ---------------------------------------------------------------------------

class TestWaitForMergeable:
    def _pr_response(self, mergeable_state: str | None) -> MagicMock:
        payload = {
            "number": 42,
            "head": {"sha": "abcdef0"},
            "base": {"ref": "batch/sprint-3"},
            "mergeable_state": mergeable_state,
        }
        return _mock_response(200, payload)

    # --- proceed states ---

    def test_returns_pr_when_clean(self, pat_set):
        from graphs.github_api import wait_for_mergeable

        with patch("graphs.github_api.requests.get", return_value=self._pr_response("clean")):
            pr = wait_for_mergeable(
                "Geoff-Walker/FamilyCookbook", 42,
                sleep=lambda *_: None, now=lambda: 0.0,
            )
        assert pr["mergeable_state"] == "clean"
        assert pr["number"] == 42

    def test_returns_pr_when_has_hooks(self, pat_set):
        from graphs.github_api import wait_for_mergeable

        with patch("graphs.github_api.requests.get", return_value=self._pr_response("has_hooks")):
            pr = wait_for_mergeable(
                "Geoff-Walker/FamilyCookbook", 42,
                sleep=lambda *_: None, now=lambda: 0.0,
            )
        assert pr["mergeable_state"] == "has_hooks"

    # --- blocking states ---

    def test_unstable_raises_merge_blocked(self, pat_set):
        from graphs.github_api import wait_for_mergeable, MergeBlocked

        with patch("graphs.github_api.requests.get", return_value=self._pr_response("unstable")):
            with pytest.raises(MergeBlocked) as excinfo:
                wait_for_mergeable(
                    "Geoff-Walker/FamilyCookbook", 42,
                    sleep=lambda *_: None, now=lambda: 0.0,
                )
        assert excinfo.value.state == "unstable"
        assert excinfo.value.pr["number"] == 42

    def test_blocked_raises_merge_blocked(self, pat_set):
        from graphs.github_api import wait_for_mergeable, MergeBlocked

        with patch("graphs.github_api.requests.get", return_value=self._pr_response("blocked")):
            with pytest.raises(MergeBlocked) as excinfo:
                wait_for_mergeable(
                    "Geoff-Walker/FamilyCookbook", 42,
                    sleep=lambda *_: None, now=lambda: 0.0,
                )
        assert excinfo.value.state == "blocked"

    def test_behind_raises_merge_blocked(self, pat_set):
        from graphs.github_api import wait_for_mergeable, MergeBlocked

        with patch("graphs.github_api.requests.get", return_value=self._pr_response("behind")):
            with pytest.raises(MergeBlocked) as excinfo:
                wait_for_mergeable(
                    "Geoff-Walker/FamilyCookbook", 42,
                    sleep=lambda *_: None, now=lambda: 0.0,
                )
        assert excinfo.value.state == "behind"

    def test_dirty_raises_merge_blocked(self, pat_set):
        from graphs.github_api import wait_for_mergeable, MergeBlocked

        with patch("graphs.github_api.requests.get", return_value=self._pr_response("dirty")):
            with pytest.raises(MergeBlocked) as excinfo:
                wait_for_mergeable(
                    "Geoff-Walker/FamilyCookbook", 42,
                    sleep=lambda *_: None, now=lambda: 0.0,
                )
        assert excinfo.value.state == "dirty"

    def test_draft_raises_merge_blocked(self, pat_set):
        from graphs.github_api import wait_for_mergeable, MergeBlocked

        with patch("graphs.github_api.requests.get", return_value=self._pr_response("draft")):
            with pytest.raises(MergeBlocked) as excinfo:
                wait_for_mergeable(
                    "Geoff-Walker/FamilyCookbook", 42,
                    sleep=lambda *_: None, now=lambda: 0.0,
                )
        assert excinfo.value.state == "draft"

    # --- pending state → re-poll ---

    def test_unknown_polls_until_terminal(self, pat_set):
        """First call returns 'unknown' (GitHub still computing), second returns 'clean'."""
        from graphs.github_api import wait_for_mergeable

        responses = [
            self._pr_response("unknown"),
            self._pr_response("clean"),
        ]
        with patch("graphs.github_api.requests.get", side_effect=responses) as mock_get:
            pr = wait_for_mergeable(
                "Geoff-Walker/FamilyCookbook", 42,
                sleep=lambda *_: None, now=lambda: 0.0, poll_interval=0,
            )
        assert pr["mergeable_state"] == "clean"
        assert mock_get.call_count == 2

    def test_none_mergeable_state_treated_as_unknown(self, pat_set):
        """GitHub may return mergeable_state=null transiently — treat as unknown and re-poll."""
        from graphs.github_api import wait_for_mergeable

        responses = [
            self._pr_response(None),
            self._pr_response("clean"),
        ]
        with patch("graphs.github_api.requests.get", side_effect=responses) as mock_get:
            pr = wait_for_mergeable(
                "Geoff-Walker/FamilyCookbook", 42,
                sleep=lambda *_: None, now=lambda: 0.0, poll_interval=0,
            )
        assert pr["mergeable_state"] == "clean"
        assert mock_get.call_count == 2

    # --- timeout ---

    def test_timeout_raises_merge_timeout_with_last_state(self, pat_set):
        from graphs.github_api import wait_for_mergeable, MergeTimeout

        # Injected clock advances past the timeout window between polls.
        clock = [0.0]

        def fake_now():
            clock[0] += 100  # each call jumps 100s; with timeout=10 we exceed immediately
            return clock[0]

        with patch("graphs.github_api.requests.get", return_value=self._pr_response("unknown")):
            with pytest.raises(MergeTimeout) as excinfo:
                wait_for_mergeable(
                    "Geoff-Walker/FamilyCookbook", 42,
                    sleep=lambda *_: None, now=fake_now, timeout=10, poll_interval=0,
                )
        assert excinfo.value.state == "unknown"

    # --- URL shape: uses the PR detail endpoint (NOT /check-runs) ---

    def test_polls_the_pr_detail_endpoint(self, pat_set):
        """Sanity check: wait_for_mergeable must hit GET /repos/{owner}/{repo}/pulls/{n}, not /check-runs."""
        from graphs.github_api import wait_for_mergeable

        with patch(
            "graphs.github_api.requests.get",
            return_value=self._pr_response("clean"),
        ) as mock_get:
            wait_for_mergeable(
                "Geoff-Walker/FamilyCookbook", 42,
                sleep=lambda *_: None, now=lambda: 0.0,
            )

        called_url = mock_get.call_args[0][0]
        assert "/pulls/42" in called_url
        assert "/check-runs" not in called_url


# ---------------------------------------------------------------------------
# merge_pr — primary code-level guard for "no merges to main"
# ---------------------------------------------------------------------------

class TestMergePr:
    def test_main_target_raises_value_error(self, pat_set):
        from graphs.github_api import merge_pr
        with pytest.raises(ValueError, match="main"):
            merge_pr("Geoff-Walker/tom", 42, base_branch="main")

    def test_master_target_raises_value_error(self, pat_set):
        from graphs.github_api import merge_pr
        with pytest.raises(ValueError, match="master"):
            merge_pr("Geoff-Walker/tom", 42, base_branch="master")

    def test_squash_merge_into_batch_branch(self, pat_set):
        from graphs.github_api import merge_pr

        with patch(
            "graphs.github_api.requests.put",
            return_value=_mock_response(200, {"sha": "merge-sha-789"}),
        ) as mock_put:
            sha = merge_pr(
                "Geoff-Walker/FamilyCookbook",
                42,
                base_branch="batch/sprint-3",
                commit_title="WAL-42 add cuisine",
            )

        assert sha == "merge-sha-789"
        body = mock_put.call_args.kwargs["json"]
        assert body["merge_method"] == "squash"
        assert body["commit_title"] == "WAL-42 add cuisine"

        called_url = mock_put.call_args[0][0]
        assert "/pulls/42/merge" in called_url

    def test_http_error_propagates(self, pat_set):
        from graphs.github_api import merge_pr

        with patch("graphs.github_api.requests.put", return_value=_mock_response(405)):
            with pytest.raises(requests.HTTPError):
                merge_pr("repo", 1, base_branch="batch/sprint-1")


# ---------------------------------------------------------------------------
# open_pr — used by batch_close_node
# ---------------------------------------------------------------------------

class TestOpenPr:
    def test_opens_pr_with_correct_payload(self, pat_set):
        from graphs.github_api import open_pr

        with patch(
            "graphs.github_api.requests.post",
            return_value=_mock_response(
                201, {"html_url": "https://github.com/Geoff-Walker/FamilyCookbook/pull/123"}
            ),
        ) as mock_post:
            url = open_pr(
                "Geoff-Walker/FamilyCookbook",
                head="batch/sprint-3",
                base="main",
                title="AppFactory batch — sprint 3",
                body="completed: ...",
            )

        assert url == "https://github.com/Geoff-Walker/FamilyCookbook/pull/123"
        payload = mock_post.call_args.kwargs["json"]
        assert payload == {
            "title": "AppFactory batch — sprint 3",
            "head": "batch/sprint-3",
            "base": "main",
            "body": "completed: ...",
        }

    def test_main_as_head_raises(self, pat_set):
        """PRs are never sourced FROM main — defensive guard."""
        from graphs.github_api import open_pr

        with pytest.raises(ValueError, match="main"):
            open_pr("repo", head="main", base="batch/sprint-1", title="x", body="y")

    def test_master_as_head_raises(self, pat_set):
        from graphs.github_api import open_pr

        with pytest.raises(ValueError, match="master"):
            open_pr("repo", head="master", base="batch/sprint-1", title="x", body="y")

    def test_main_as_base_is_allowed(self, pat_set):
        """Unlike merge_pr, open_pr targeting main is valid — this is batch_close_node's pattern."""
        from graphs.github_api import open_pr

        with patch(
            "graphs.github_api.requests.post",
            return_value=_mock_response(201, {"html_url": "https://x/pull/1"}),
        ):
            url = open_pr("repo", head="batch/sprint-1", base="main", title="t", body="b")
        assert url == "https://x/pull/1"

    def test_http_error_propagates(self, pat_set):
        from graphs.github_api import open_pr

        with patch("graphs.github_api.requests.post", return_value=_mock_response(422)):
            with pytest.raises(requests.HTTPError):
                open_pr("repo", head="batch/sprint-1", base="main", title="t", body="b")
