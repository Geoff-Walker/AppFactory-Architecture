"""
GitHub REST API helpers for the AppFactory pipeline.

Used by iterative_dev nodes (setup_node, merge_node, batch_close_node) to
create branches, wait for CI, merge ticket PRs into integration branches, and
open the final batch PR against main (open only — never merged by the
pipeline; only Geoff merges to main).

Authentication: ``GITHUB_APPFACTORY_PAT`` environment variable, populated by
Infisical injection on the VM. The PAT is fine-grained, scoped to the three
AppFactory-reachable repos with Contents read/write + Pull requests read/write
+ Metadata read. See project.md → "Session 2026-04-22" for the full PAT story.

Code-level guards: every function that performs a write or merge enforces
that the target is never ``main`` or ``master``. The PAT itself does not
constrain pushable refs (a fine-grained PAT limitation discovered 2026-04-22),
so these assertions plus the workspace pre-push hook are the protection layer.
"""

from __future__ import annotations

import logging
import os
import re
import time
from typing import Optional

import requests

logger = logging.getLogger(__name__)

GITHUB_API = "https://api.github.com"
DEFAULT_TIMEOUT = 30

# CI polling defaults — caller may override.
CI_POLL_INTERVAL_SECONDS = 15
CI_POLL_TIMEOUT_SECONDS = 15 * 60      # 15 minutes — generous; tighten later if needed
CI_NO_CHECKS_GRACE_SECONDS = 60        # how long to wait for CI to register before treating "no checks" as "no CI required"

# Mergeable-state polling defaults — used by wait_for_mergeable.
MERGEABLE_POLL_INTERVAL_SECONDS = 5
MERGEABLE_POLL_TIMEOUT_SECONDS = 10 * 60   # 10 minutes — enough for CI on a typical ticket PR

# mergeable_state values per GitHub docs. Classified by how the pipeline should react.
# https://docs.github.com/en/rest/pulls/pulls#get-a-pull-request
_MERGEABLE_PROCEED_STATES = frozenset({"clean", "has_hooks"})
_MERGEABLE_BLOCKING_STATES = frozenset({"unstable", "blocked", "behind", "dirty", "draft"})
_MERGEABLE_PENDING_STATES = frozenset({"unknown"})


# ---------------------------------------------------------------------------
# Custom exceptions for CI / merge failure paths
# ---------------------------------------------------------------------------

class CIFailed(Exception):
    """Raised when at least one check on the PR head SHA reaches a failing conclusion.

    Retained for backwards-compatibility with ``wait_for_checks`` (which the
    pipeline no longer calls from ``merge_node`` — see ``wait_for_mergeable``).
    """


class CITimeout(Exception):
    """Raised when CI checks are still pending after CI_POLL_TIMEOUT_SECONDS.

    Retained for backwards-compatibility with ``wait_for_checks``.
    """


class MergeBlocked(Exception):
    """Raised when a PR's ``mergeable_state`` is a terminal blocking condition.

    Attributes:
        state: the raw ``mergeable_state`` string from GitHub (e.g. ``"unstable"``,
            ``"blocked"``, ``"behind"``, ``"dirty"``, ``"draft"``).
        pr: the PR detail dict returned by the GitHub API at the point of the block
            (useful for surfacing diagnostic context to the caller / Geoff).
    """

    def __init__(self, state: str, pr: dict, message: str | None = None) -> None:
        self.state = state
        self.pr = pr
        super().__init__(message or f"PR mergeable_state is {state!r}")


class MergeTimeout(Exception):
    """Raised when ``wait_for_mergeable`` polling exceeds its timeout budget.

    Attributes:
        state: the last-seen ``mergeable_state`` value when the timeout fired.
        pr: the most recent PR detail dict (may be empty on repeated ``unknown``).
    """

    def __init__(self, state: str, pr: dict, message: str | None = None) -> None:
        self.state = state
        self.pr = pr
        super().__init__(message or f"mergeable_state still {state!r} after timeout")


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

def gh_headers() -> dict[str, str]:
    """Return GitHub API headers with the AppFactory PAT.

    Raises:
        RuntimeError: if ``GITHUB_APPFACTORY_PAT`` is not set. The pipeline
            cannot operate without it — surfacing the error is correct.
    """
    token = os.environ.get("GITHUB_APPFACTORY_PAT")
    if not token:
        raise RuntimeError(
            "GITHUB_APPFACTORY_PAT is not set in the environment. "
            "Pipeline GitHub API calls cannot proceed without it. "
            "Confirm the secret is present in Infisical (AppFactory prod env) "
            "and that the langgraph-api.service start script injects it."
        )
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


# ---------------------------------------------------------------------------
# Branch helpers — used by setup_node
# ---------------------------------------------------------------------------

_SPRINT_REF_PATTERN = re.compile(r"^refs/heads/batch/sprint-(\d+)$")


def next_sprint_number(repo: str) -> int:
    """Scan existing ``batch/sprint-N`` branches in ``repo`` and return ``max(N) + 1``.

    If no batch/sprint-* branches exist, returns 1.

    Args:
        repo: full owner/repo slug, e.g. ``"Geoff-Walker/FamilyCookbook"``.

    Raises:
        requests.HTTPError: if the GitHub API returns a non-success status
            (other than 404, which is treated as "no matching refs" — clean
            slate, returns 1).
    """
    url = f"{GITHUB_API}/repos/{repo}/git/matching-refs/heads/batch/sprint-"
    response = requests.get(url, headers=gh_headers(), timeout=DEFAULT_TIMEOUT)

    # The matching-refs endpoint returns [] for no matches, not 404 — but
    # be defensive: treat 404 as "no matches" too.
    if response.status_code == 404:
        return 1
    response.raise_for_status()

    refs = response.json()
    highest = 0
    for ref in refs:
        match = _SPRINT_REF_PATTERN.match(ref.get("ref", ""))
        if match:
            n = int(match.group(1))
            if n > highest:
                highest = n
    return highest + 1


def get_branch_sha(repo: str, branch: str) -> str:
    """Return the current commit SHA of ``branch`` in ``repo``.

    Args:
        repo: ``owner/repo`` slug.
        branch: branch name without the ``refs/heads/`` prefix.

    Raises:
        requests.HTTPError: if the API call fails (e.g. branch not found → 404).
    """
    url = f"{GITHUB_API}/repos/{repo}/git/refs/heads/{branch}"
    response = requests.get(url, headers=gh_headers(), timeout=DEFAULT_TIMEOUT)
    response.raise_for_status()
    return response.json()["object"]["sha"]


def create_branch(repo: str, branch: str, base_sha: str) -> None:
    """Create a new branch ``branch`` in ``repo`` pointing at ``base_sha``.

    Args:
        repo: ``owner/repo`` slug.
        branch: new branch name (without ``refs/heads/`` prefix).
        base_sha: full commit SHA the new ref will point at.

    Raises:
        ValueError: if ``branch`` is ``main`` or ``master`` — the pipeline
            never creates or overwrites these.
        requests.HTTPError: on API failure (e.g. 422 if branch already exists).
    """
    if branch in ("main", "master"):
        raise ValueError(
            f"create_branch refuses to target protected branch '{branch}'. "
            "This is a code-level guard; should never trigger in normal flow."
        )

    url = f"{GITHUB_API}/repos/{repo}/git/refs"
    body = {"ref": f"refs/heads/{branch}", "sha": base_sha}
    response = requests.post(url, headers=gh_headers(), json=body, timeout=DEFAULT_TIMEOUT)
    response.raise_for_status()
    logger.info("create_branch: created %s in %s at %s", branch, repo, base_sha[:8])


# ---------------------------------------------------------------------------
# PR helpers — used by merge_node
# ---------------------------------------------------------------------------

_PR_URL_PATTERN = re.compile(r"github\.com/[^/]+/[^/]+/pull/(\d+)")


def parse_pr_number(pr_url: str) -> Optional[int]:
    """Extract the PR number from a GitHub PR URL.

    Returns ``None`` if the URL does not match the expected pattern — caller
    is expected to handle that as a failure mode.
    """
    if not pr_url:
        return None
    match = _PR_URL_PATTERN.search(pr_url)
    if not match:
        return None
    return int(match.group(1))


def get_pr(repo: str, pr_number: int) -> dict:
    """Fetch full PR metadata for ``repo`` PR ``pr_number``.

    Returns the raw GitHub PR object dict (includes ``head.sha``, ``base.ref``,
    ``title``, ``body``, etc.).

    Raises:
        requests.HTTPError: on API failure (e.g. 404 if PR doesn't exist).
    """
    url = f"{GITHUB_API}/repos/{repo}/pulls/{pr_number}"
    response = requests.get(url, headers=gh_headers(), timeout=DEFAULT_TIMEOUT)
    response.raise_for_status()
    return response.json()


def _classify_check_runs(check_runs: list) -> str:
    """Inspect a check-runs list and classify the overall state.

    Returns one of: ``"all_pass"``, ``"some_failed"``, ``"still_running"``,
    ``"none"`` (no checks present at all).
    """
    if not check_runs:
        return "none"

    passing_conclusions = {"success", "neutral", "skipped"}
    failing_conclusions = {"failure", "cancelled", "timed_out", "action_required", "stale"}

    any_running = False
    for run in check_runs:
        status = run.get("status")
        if status in ("queued", "in_progress", "waiting", "pending", "requested"):
            any_running = True
            continue
        # status == "completed" → look at conclusion
        conclusion = run.get("conclusion")
        if conclusion in failing_conclusions:
            return "some_failed"
        if conclusion not in passing_conclusions:
            # Unknown conclusion — be safe, treat as still running so we wait
            any_running = True

    return "still_running" if any_running else "all_pass"


def wait_for_checks(
    repo: str,
    sha: str,
    *,
    poll_interval: float = CI_POLL_INTERVAL_SECONDS,
    timeout: float = CI_POLL_TIMEOUT_SECONDS,
    no_checks_grace: float = CI_NO_CHECKS_GRACE_SECONDS,
    sleep=time.sleep,
    now=time.monotonic,
) -> None:
    """Poll the check-runs endpoint until all checks pass, any fails, or timeout.

    If no check-runs are registered at all after ``no_checks_grace`` seconds,
    the function returns successfully — this models repos with no CI configured
    (e.g. the sandbox repo for T2 smoke tests).

    Raises:
        CIFailed: if at least one check completed with a failing conclusion.
        CITimeout: if checks are still pending after ``timeout`` seconds.

    Args:
        sleep / now: injectable for tests so polling can be exercised without
            real wall-clock waits.
    """
    started = now()
    no_checks_started_at: float | None = None

    while True:
        url = f"{GITHUB_API}/repos/{repo}/commits/{sha}/check-runs"
        response = requests.get(url, headers=gh_headers(), timeout=DEFAULT_TIMEOUT)
        response.raise_for_status()
        check_runs = response.json().get("check_runs", [])

        state = _classify_check_runs(check_runs)
        if state == "all_pass":
            logger.info("wait_for_checks: all %d checks passed on %s", len(check_runs), sha[:8])
            return
        if state == "some_failed":
            raise CIFailed(
                f"one or more checks failed on {sha[:8]}: "
                f"{[r.get('name') for r in check_runs if r.get('conclusion') not in ('success', 'neutral', 'skipped', None)]}"
            )

        elapsed = now() - started
        if state == "none":
            # Nothing has registered. Give CI a window to wake up; if it's still
            # absent after the grace period, treat as "no CI required" and proceed.
            if no_checks_started_at is None:
                no_checks_started_at = now()
            if (now() - no_checks_started_at) >= no_checks_grace:
                logger.info(
                    "wait_for_checks: no checks registered after %ss grace — treating as no-CI",
                    no_checks_grace,
                )
                return
        else:
            # state == "still_running" — checks exist but not all settled.
            no_checks_started_at = None

        if elapsed >= timeout:
            raise CITimeout(
                f"checks still pending after {timeout}s on {sha[:8]} — "
                f"last seen {len(check_runs)} runs"
            )

        sleep(poll_interval)


# NOTE: We poll PR.mergeable_state rather than /check-runs because fine-grained
# PATs on personal GitHub accounts cannot grant "Checks: Read" — the permission
# is not in the dropdown (verified 2026-04-23). mergeable_state is computed by
# GitHub from the same check data but exposed on the PR endpoint, which only
# needs "Pull requests: Read". If we migrate to a GitHub App or org-level PAT
# with Checks access, we could augment this with direct check-run inspection
# for finer-grained diagnostics — but mergeable_state is sufficient for gating.
def wait_for_mergeable(
    repo: str,
    pr_number: int,
    *,
    poll_interval: float = MERGEABLE_POLL_INTERVAL_SECONDS,
    timeout: float = MERGEABLE_POLL_TIMEOUT_SECONDS,
    sleep=time.sleep,
    now=time.monotonic,
) -> dict:
    """Poll ``pr_number`` until GitHub reports a terminal ``mergeable_state``.

    GitHub computes ``mergeable_state`` server-side from the combined state of
    the PR's checks, branch-protection rules, and merge-conflict status. It is
    the same signal the web UI uses to enable or disable the green "Merge" button.

    Decision matrix (per GitHub REST docs, see module-level note for why we use
    this endpoint instead of ``/check-runs``):

      - ``clean``     → return the PR dict (CI passed or unconfigured, no blockers)
      - ``has_hooks`` → return the PR dict (checks pass, post-merge hooks succeeded)
      - ``unstable``  → raise ``MergeBlocked`` — non-required checks failed; caller
                        (``merge_node``) surfaces this to Geoff for park/abort
      - ``blocked``   → raise ``MergeBlocked`` — required checks failing or branch
                        protection blocking merge
      - ``behind``    → raise ``MergeBlocked`` — head is out of date with base;
                        pipeline cannot auto-update, Geoff decides
      - ``dirty``     → raise ``MergeBlocked`` — merge conflict; not resolvable by
                        the pipeline
      - ``draft``     → raise ``MergeBlocked`` — PR is still a draft
      - ``unknown``   → keep polling — GitHub is still computing mergeability
                        (often seen on the first call after PR creation)

    The API may return ``mergeable_state`` as ``None`` transiently; this is
    treated the same as ``"unknown"`` (re-poll).

    Args:
        repo: ``owner/repo`` slug, e.g. ``"Geoff-Walker/FamilyCookbook"``.
        pr_number: the PR number to poll.
        poll_interval: seconds between API calls.
        timeout: total seconds allowed before ``MergeTimeout`` fires.
        sleep / now: injectable for tests so polling can be exercised without
            real wall-clock waits.

    Returns:
        The full PR detail dict once a proceed-state is observed.

    Raises:
        MergeBlocked: with ``state`` and ``pr`` attached, for any terminal
            blocking state.
        MergeTimeout: with the last-seen ``state`` attached, if polling exceeds
            ``timeout`` seconds (e.g. GitHub is stuck returning ``unknown``).
        requests.HTTPError: on non-200 PR detail API responses.
    """
    started = now()
    last_state: str = "unknown"
    last_pr: dict = {}

    while True:
        pr = get_pr(repo, pr_number)
        state = pr.get("mergeable_state") or "unknown"
        last_state = state
        last_pr = pr

        if state in _MERGEABLE_PROCEED_STATES:
            logger.info(
                "wait_for_mergeable: %s#%d reached terminal state %r — proceeding",
                repo, pr_number, state,
            )
            return pr

        if state in _MERGEABLE_BLOCKING_STATES:
            logger.warning(
                "wait_for_mergeable: %s#%d reached blocking state %r — raising MergeBlocked",
                repo, pr_number, state,
            )
            raise MergeBlocked(
                state=state,
                pr=pr,
                message=f"PR #{pr_number} in {repo} is {state!r}",
            )

        # state in _MERGEABLE_PENDING_STATES (or an unrecognised value) — log and re-poll.
        logger.info(
            "wait_for_mergeable: %s#%d state=%r — re-polling in %ss",
            repo, pr_number, state, poll_interval,
        )

        elapsed = now() - started
        if elapsed >= timeout:
            raise MergeTimeout(
                state=last_state,
                pr=last_pr,
                message=(
                    f"PR #{pr_number} in {repo} still {last_state!r} after {timeout}s — "
                    "GitHub did not settle on a terminal mergeable_state"
                ),
            )

        sleep(poll_interval)


def open_pr(
    repo: str,
    head: str,
    base: str,
    title: str,
    body: str,
) -> str:
    """Open a PR. Returns the new PR's ``html_url``.

    Used by ``batch_close_node`` to open the integration-branch → ``main``
    review PR. Note: this function does NOT refuse a ``main`` target —
    opening a PR targeting main from a batch branch is the whole point.
    The "no merges to main" rule is enforced by ``merge_pr``, not here.

    Args:
        repo: ``owner/repo`` slug.
        head: source branch (the integration branch). Must NOT be main/master.
        base: target branch (typically ``main``).
        title: PR title.
        body: PR body (markdown supported).

    Raises:
        ValueError: if ``head`` is ``main`` or ``master`` — defence against an
            inverted call that would open a PR whose source is main itself.
        requests.HTTPError: on API failure.
    """
    if head in ("main", "master"):
        raise ValueError(
            f"open_pr refuses head={head!r} — PRs are never sourced from main/master."
        )

    url = f"{GITHUB_API}/repos/{repo}/pulls"
    payload = {"title": title, "head": head, "base": base, "body": body}
    response = requests.post(url, headers=gh_headers(), json=payload, timeout=DEFAULT_TIMEOUT)
    response.raise_for_status()
    pr_url = response.json()["html_url"]
    logger.info("open_pr: opened %s (head=%s base=%s) at %s", repo, head, base, pr_url)
    return pr_url


def merge_pr(
    repo: str,
    pr_number: int,
    base_branch: str,
    commit_title: str | None = None,
) -> str:
    """Squash-merge ``pr_number`` in ``repo``. Returns the resulting merge commit SHA.

    Args:
        repo: ``owner/repo`` slug.
        pr_number: PR number.
        base_branch: the branch the PR targets. **Must not be main/master.**
            Passed in (rather than re-fetched) so the guard runs at the call
            site without an extra round trip.
        commit_title: optional override for the squash-merge commit message.
            Defaults to GitHub's default (PR title + "(#PR_NUMBER)").

    Raises:
        ValueError: if ``base_branch`` is ``main`` or ``master`` — this is the
            primary code-level enforcement of the "no merges to main" rule.
        requests.HTTPError: on API failure (e.g. 405 if not mergeable).
    """
    if base_branch in ("main", "master"):
        raise ValueError(
            f"merge_pr refuses to merge into protected branch '{base_branch}'. "
            "This is the primary code-level guard for the 'no merges to main' rule."
        )

    url = f"{GITHUB_API}/repos/{repo}/pulls/{pr_number}/merge"
    body: dict = {"merge_method": "squash"}
    if commit_title:
        body["commit_title"] = commit_title

    response = requests.put(url, headers=gh_headers(), json=body, timeout=DEFAULT_TIMEOUT)
    response.raise_for_status()
    sha = response.json().get("sha", "")
    logger.info("merge_pr: merged %s#%d into %s as %s", repo, pr_number, base_branch, sha[:8])
    return sha
