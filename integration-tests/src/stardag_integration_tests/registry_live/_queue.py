"""First-in, first-out admission for the registry-live tier (STA-138).

The tier is serialised workspace-wide: one run peaks at 45-65 Modal
containers against a 100-container workspace limit. That used to be a
GitHub concurrency group on the job, and a concurrency group holds **one
running and one pending** job -- a third request cancels the pending one.
With N PRs in flight, N-2 were dropped, each needing a manual re-trigger
that could be dropped again the same way.

So the queue lives somewhere that does not drop: a pending commit status,
``Registry-live tier``, on each PR head that wants a run. Three parties:

- **The request** (``modal-live.yml``) posts ``Queued ...`` on the PR head
  and dispatches the selector. It holds no runner while it waits.
- **The selector** (``registry-live-dispatch.yml``, this module) admits the
  oldest queued heads into free *slots* and dispatches the runner for each.
  Every selector run is interchangeable with every other, so its own
  concurrency group dropping a pending one loses nothing -- which is the
  property the job-level group lacked.
- **The runner** (``registry-live.yml``) runs the tier for one PR head in
  one slot, posts the verdict on the head, and dispatches the selector
  again.

Slots exist so that admitting two runs at once is a variable rather than
a redesign (``REGISTRY_LIVE_SLOTS``, default 1). Each runner's concurrency
group is keyed by its slot, so the group stays as a safety net under the
selector rather than being the queue.

Stdlib only, and run as a plain file rather than through the package:
the selector should start in seconds, not after resolving this project's
dependencies. **Run it with ``python3 -I``**: a script's own directory goes
first on ``sys.path``, and this one holds a ``selectors.py`` that shadows
the stdlib module ``subprocess`` imports.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field

CONTEXT = "Registry-live tier"
RUNNER_WORKFLOW = "registry-live.yml"
LABEL = "modal-live"

# The description prefixes are the state machine. A status is only ever
# written with one of these at the front, by the request (QUEUED), the
# selector (DISPATCHED) and the runner (RUNNING, then a final state).
QUEUED = "Queued"
DISPATCHED = "Dispatched"
RUNNING = "Running"

# Everything a run can be before GitHub calls it `completed`.
ACTIVE_RUN_STATUSES = frozenset(
    {"queued", "in_progress", "waiting", "pending", "requested"}
)

# The runner's `run-name` is a contract with this module: it is the only
# place a dispatched run carries its PR and slot where the runs API shows
# them. Kept in step with `run-name` in `registry-live.yml`.
_RUN_PR = re.compile(r"PR #(\d+)\b")
_RUN_SLOT = re.compile(r"slot (\d+)\b")

# The runner's last job, named as in `registry-live.yml`. A run whose every
# other job has completed is only reporting and handing over: its stack is
# deleted, so its slot is free even though the run is still in progress.
REPORT_JOB = "Report and hand over"

# How long to wait for a dispatched run to show up in the runs list.
# Until it does, the next selector would see the slot as free.
VISIBILITY_TIMEOUT_S = 120.0


@dataclass(frozen=True)
class Request:
    """A PR head and the latest `Registry-live tier` status on it."""

    pr: int
    sha: str
    branch: str
    state: str  # PENDING / SUCCESS / FAILURE / ERROR, as GraphQL spells it
    description: str
    created_at: str  # ISO-8601 UTC; compares correctly as a string
    cross_repo: bool
    author: str
    labels: frozenset[str] = field(default_factory=frozenset)

    @property
    def queued(self) -> bool:
        return self.state == "PENDING" and self.description.startswith(QUEUED)

    @property
    def admitted(self) -> bool:
        return self.state == "PENDING" and self.description.startswith(
            (DISPATCHED, RUNNING)
        )


@dataclass(frozen=True)
class ActiveRun:
    run_id: int
    slot: int
    pr: int | None  # None for a run that is not for a PR: the weekly one
    # In its last job only (see REPORT_JOB): it no longer holds its slot,
    # but it still owns its PR's status until it posts the verdict.
    releasing: bool = False


@dataclass(frozen=True)
class Plan:
    dispatch: list[tuple[Request, int]]
    # Admitted, but no runner is active for the PR: the runner was lost
    # without reporting. Marked as an error rather than re-queued, so a
    # head that kills its runner cannot loop.
    stale: list[Request]
    refused: list[tuple[Request, str]]
    waiting: list[Request]


def is_releasing(jobs: list[dict]) -> bool:
    """Whether a run is past its tier and teardown, only reporting.

    The hand-over is dispatched *from* that last job, so the run is still in
    progress when its selector starts. Read from the run's jobs rather than
    passed along by the dispatching run, because selectors are dropped from
    their group freely: the one that survives must reach the same answer as
    the one that carried the id.
    """
    names = {job.get("name") for job in jobs}
    if REPORT_JOB not in names:
        return False
    return all(
        job.get("status") == "completed"
        for job in jobs
        if job.get("name") != REPORT_JOB
    )


def parse_active_runs(
    runs: list[dict], releasing: frozenset[int] = frozenset()
) -> list[ActiveRun]:
    """The runner runs that are not yet `completed`.

    ``releasing`` names the ones :func:`is_releasing` found in their last
    job; they are listed, but do not hold a slot.
    """
    active = []
    for run in runs:
        if run.get("status") not in ACTIVE_RUN_STATUSES:
            continue
        title = run.get("display_title") or run.get("name") or ""
        pr = _RUN_PR.search(title)
        slot = _RUN_SLOT.search(title)
        active.append(
            ActiveRun(
                run_id=int(run["id"]),
                # The runner's concurrency group defaults to slot 1, so a run
                # whose title says nothing occupies slot 1 as well.
                slot=int(slot.group(1)) if slot else 1,
                pr=int(pr.group(1)) if pr else None,
                releasing=int(run["id"]) in releasing,
            )
        )
    return active


def parse_requests(data: dict) -> list[Request]:
    """Open PR heads carrying a `Registry-live tier` status.

    Only the status on the *current* head counts. A push supersedes a queued
    head by moving the head, so the old SHA's status is simply never read.
    """
    requests = []
    prs = data["data"]["repository"]["pullRequests"]["nodes"]
    for pr in prs:
        commits = pr["commits"]["nodes"]
        if not commits:
            continue
        commit = commits[0]["commit"]
        if commit["oid"] != pr["headRefOid"]:
            continue
        status = (commit.get("status") or {}).get("context")
        if not status:
            continue
        requests.append(
            Request(
                pr=pr["number"],
                sha=pr["headRefOid"],
                branch=pr["headRefName"],
                state=status["state"],
                description=status.get("description") or "",
                created_at=status["createdAt"],
                cross_repo=pr["isCrossRepository"],
                author=(pr.get("author") or {}).get("login") or "",
                labels=frozenset(n["name"] for n in pr["labels"]["nodes"]),
            )
        )
    return requests


def refusal(request: Request) -> str | None:
    """Why this head must not get the Modal credentials, if it must not.

    The same rule `decide` applies in `modal-live.yml`, checked again here
    because this is the side that hands out the secrets: a status is
    writable by anyone with write access, not only by `decide`.
    """
    if request.cross_repo:
        return "PR is from a fork; the tier never runs fork code with credentials."
    if request.author.startswith("dependabot") and LABEL not in request.labels:
        return f"Dependabot PR without the '{LABEL}' label."
    return None


def plan(requests: list[Request], active: list[ActiveRun], slots: int) -> Plan:
    # A releasing run still counts here: it has yet to post its verdict, and
    # calling its head stale would race that verdict with an error.
    running_prs = {run.pr for run in active if run.pr is not None}
    occupied = {run.slot for run in active if not run.releasing}
    free = [s for s in range(1, slots + 1) if s not in occupied]

    stale = [r for r in requests if r.admitted and r.pr not in running_prs]

    refused: list[tuple[Request, str]] = []
    eligible: list[Request] = []
    # Oldest first. The PR number only breaks a tie within one second.
    for request in sorted(
        (r for r in requests if r.queued), key=lambda r: (r.created_at, r.pr)
    ):
        reason = refusal(request)
        if reason:
            refused.append((request, reason))
        else:
            eligible.append(request)

    dispatch = list(zip(eligible, free))
    return Plan(
        dispatch=dispatch,
        stale=stale,
        refused=refused,
        waiting=eligible[len(dispatch) :],
    )


# --- GitHub side ---------------------------------------------------------

_REQUESTS_QUERY = """
query($owner: String!, $name: String!, $context: String!) {
  repository(owner: $owner, name: $name) {
    pullRequests(states: OPEN, first: 100) {
      pageInfo { hasNextPage }
      nodes {
        number
        headRefName
        headRefOid
        isCrossRepository
        author { login }
        labels(first: 50) { nodes { name } }
        commits(last: 1) {
          nodes { commit { oid status { context(name: $context) {
            state description createdAt
          } } } }
        }
      }
    }
  }
}
"""


def _gh(*args: str) -> str:
    return subprocess.run(
        ["gh", *args], check=True, capture_output=True, text=True
    ).stdout


def _gh_ok(*args: str) -> bool:
    return subprocess.run(["gh", *args], capture_output=True).returncode == 0


def _runner_runs(repo: str) -> list[dict]:
    out = _gh(
        "api",
        f"repos/{repo}/actions/workflows/{RUNNER_WORKFLOW}/runs?per_page=50",
    )
    return json.loads(out)["workflow_runs"]


def _run_jobs(repo: str, run_id: int) -> list[dict]:
    out = _gh("api", f"repos/{repo}/actions/runs/{run_id}/jobs?per_page=100")
    return json.loads(out)["jobs"]


def _post_status(repo: str, sha: str, state: str, description: str, url: str) -> None:
    _gh(
        "api",
        f"repos/{repo}/statuses/{sha}",
        "-f",
        f"state={state}",
        "-f",
        f"context={CONTEXT}",
        "-f",
        f"description={description[:140]}",
        "-f",
        f"target_url={url}",
    )


def _dispatch_ref(repo: str, request: Request, default_branch: str) -> str:
    """The ref whose `registry-live.yml` should run this head.

    The PR's own branch, so the tier's steps come from the same commit as
    the code they test: a PR that edits the tier tests its own edit, and the
    merge-ref/head mismatch STA-92 diagnosed cannot arise. A branch older
    than the runner has no such file, and falls back to the default
    branch's runner, which checks out the head by SHA.
    """
    path = f".github/workflows/{RUNNER_WORKFLOW}"
    if _gh_ok("api", f"repos/{repo}/contents/{path}?ref={request.sha}", "--silent"):
        return request.branch
    return default_branch


def _await_visible(repo: str, pr: int, known: set[int]) -> bool:
    """Whether a runner for ``pr`` that was not running before appears.

    ``known`` matters when the PR already has a runner on an older head:
    without it, that run would confirm a dispatch it has nothing to do with.
    """
    deadline = time.monotonic() + VISIBILITY_TIMEOUT_S
    while time.monotonic() < deadline:
        for run in parse_active_runs(_runner_runs(repo)):
            if run.pr == pr and run.run_id not in known:
                return True
        time.sleep(5)
    return False


def _summary(lines: list[str]) -> None:
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    text = "\n".join(lines) + "\n"
    print(text)
    if path:
        with open(path, "a") as f:
            f.write(text)


def dispatch(args: argparse.Namespace) -> int:
    repo = args.repo
    owner, name = repo.split("/")
    run_url = (
        f"{os.environ.get('GITHUB_SERVER_URL', 'https://github.com')}/{repo}"
        f"/actions/runs/{os.environ.get('GITHUB_RUN_ID', '')}"
    )

    runs = _runner_runs(repo)
    releasing = frozenset(
        int(run["id"])
        for run in runs
        if run.get("status") == "in_progress"
        and is_releasing(_run_jobs(repo, int(run["id"])))
    )
    active = parse_active_runs(runs, releasing)
    data = json.loads(
        _gh(
            "api",
            "graphql",
            "-f",
            f"query={_REQUESTS_QUERY}",
            "-F",
            f"owner={owner}",
            "-F",
            f"name={name}",
            "-F",
            f"context={CONTEXT}",
        )
    )
    if data["data"]["repository"]["pullRequests"]["pageInfo"]["hasNextPage"]:
        print(
            "::warning title=Registry-live queue::More than 100 open PRs; "
            "only the first 100 are considered."
        )
    the_plan = plan(parse_requests(data), active, args.slots)

    lines = [
        "### Registry-live queue",
        "",
        f"Slots: {args.slots}; active runners: "
        + (
            ", ".join(
                f"slot {r.slot} ({f'PR #{r.pr}' if r.pr else 'main'}, run {r.run_id}"
                f"{', releasing' if r.releasing else ''})"
                for r in active
            )
            or "none"
        ),
        "",
    ]

    for request in the_plan.stale:
        lines.append(f"- PR #{request.pr}: runner lost without reporting")
        _post_status(
            repo,
            request.sha,
            "error",
            "The runner ended without reporting. Re-run the PR's Modal Live "
            "Tests to queue again.",
            run_url,
        )
    for request, reason in the_plan.refused:
        lines.append(f"- PR #{request.pr}: refused -- {reason}")
        _post_status(repo, request.sha, "error", f"Refused: {reason}", run_url)

    for request, slot in the_plan.dispatch:
        ref = _dispatch_ref(repo, request, args.default_branch)
        _post_status(
            repo,
            request.sha,
            "pending",
            f"{DISPATCHED} to slot {slot}; waiting for the runner to start.",
            run_url,
        )
        known = {run.run_id for run in parse_active_runs(_runner_runs(repo))}
        _gh(
            "workflow",
            "run",
            RUNNER_WORKFLOW,
            "--repo",
            repo,
            "--ref",
            ref,
            "-f",
            f"pr={request.pr}",
            "-f",
            f"sha={request.sha}",
            "-f",
            f"slot={slot}",
        )
        # Not done until the run is listed: the next selector reads the
        # slot as free until then, and would dispatch a second run into it.
        if _await_visible(repo, request.pr, known):
            lines.append(f"- PR #{request.pr}: dispatched to slot {slot} (ref {ref})")
        else:
            lines.append(f"- PR #{request.pr}: dispatch not confirmed")
            _post_status(
                repo,
                request.sha,
                "error",
                "The runner was dispatched but never appeared. Re-run the "
                "PR's Modal Live Tests to queue again.",
                run_url,
            )

    for position, request in enumerate(the_plan.waiting, start=1):
        lines.append(
            f"- PR #{request.pr}: waiting, position {position} "
            f"(queued {request.created_at})"
        )
    if not (
        the_plan.stale or the_plan.refused or the_plan.dispatch or the_plan.waiting
    ):
        lines.append("Nothing queued.")
    _summary(lines)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Registry-live tier queue.")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("dispatch", help="Admit queued heads into free slots.")
    p.add_argument("--repo", required=True)
    p.add_argument("--default-branch", default="main")
    p.add_argument("--slots", type=int, default=1)
    args = parser.parse_args(argv)
    if args.slots < 1:
        parser.error("--slots must be at least 1")
    return dispatch(args)


if __name__ == "__main__":
    sys.exit(main())
