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
from datetime import datetime, timedelta, timezone

CONTEXT = "Registry-live tier"
# The weekly run's status, on a default-branch commit. A context of its own
# rather than a marker in the description: with merge-commit merges, PR heads
# carrying `Registry-live tier` statuses end up in the default branch's
# history, and must never read as the weekly request.
WEEKLY_CONTEXT = "Registry-live tier (weekly)"
RUNNER_WORKFLOW = "registry-live.yml"
LABEL = "modal-live"

# The description prefixes are the state machine. A status is only ever
# written with one of these at the front, by the request (QUEUED), the
# selector (DISPATCHED) and the runner (RUNNING, then a final state).
QUEUED = "Queued"
DISPATCHED = "Dispatched"
RUNNING = "Running"

# Everything a run can be before GitHub calls it `completed`, in the order a
# run moves through them. `_runner_runs` queries them in this order, so a run
# that advances between two queries is caught by a later one.
ACTIVE_RUN_LIFECYCLE = ("requested", "pending", "waiting", "queued", "in_progress")
ACTIVE_RUN_STATUSES = frozenset(ACTIVE_RUN_LIFECYCLE)

# The runner's `run-name` is a contract with this module: it is the only
# place a dispatched run carries its PR and slot where the runs API shows
# them. Kept in step with `run-name` in `registry-live.yml`.
_RUN_PR = re.compile(r"PR #(\d+)\b")
_RUN_SLOT = re.compile(r"slot (\d+)\b")

# The runner's last job, named as in `registry-live.yml`. A run whose every
# other job has completed is only reporting and handing over: its stack is
# deleted, so its slot is free even though the run is still in progress.
REPORT_JOB = "Report and hand over"

# A run listed as not completed for longer than this is *suspected* of being
# a ghost, and checked (see `is_ghost`). GitHub has kept one in this
# repository listed as `queued` for weeks, with no jobs at all, while
# `gh run cancel` called it completed (run 34748665026, 2026-09-13); in the
# runs list it would hold a slot forever.
GHOST_AFTER = timedelta(hours=3)

# How far back the default branch is searched for a pending weekly request
# (see `parse_default_branch`). Fifty merges between a Monday enqueue and its
# admission would be a queue wedged for other reasons.
DEFAULT_BRANCH_DEPTH = 50

# How long to wait for a dispatched run to show up in the runs list.
# Until it does, the next selector would see the slot as free.
VISIBILITY_TIMEOUT_S = 120.0


@dataclass(frozen=True)
class Request:
    """A head and the latest `Registry-live tier` status on it.

    ``pr`` is None for the default branch's head, which the weekly run
    queues through the same status so that it is admitted like everything
    else rather than joining a slot's concurrency group unannounced.
    """

    pr: int | None
    sha: str
    branch: str
    state: str  # PENDING / SUCCESS / FAILURE / ERROR, as GraphQL spells it
    description: str
    created_at: str  # ISO-8601 UTC; compares correctly as a string
    cross_repo: bool
    author: str
    labels: frozenset[str] = field(default_factory=frozenset)

    @property
    def context(self) -> str:
        return CONTEXT if self.pr is not None else WEEKLY_CONTEXT

    @property
    def label(self) -> str:
        return (
            f"PR #{self.pr}" if self.pr is not None else f"{self.branch} {self.sha[:7]}"
        )

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


def _parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def is_aged(run: dict, now: datetime | None = None) -> bool:
    """Whether a not-completed run is old enough to be checked for a ghost.

    From `run_started_at`, not `created_at`: a re-run is a new attempt of an
    old run, and only the former moves with the attempt.
    """
    now = now or datetime.now(timezone.utc)
    started = run.get("run_started_at") or run.get("created_at")
    return bool(started) and now - _parse_time(started) > GHOST_AFTER


def is_ghost(jobs: list[dict]) -> bool:
    """Whether an aged run is a ghost: listed as not completed, with no jobs.

    Age alone is not enough. A run can wait hours for a GitHub runner, and
    the tier's own timeout does not bound that wait -- but such a run has
    its jobs, listed as `queued`. A run pending in the runner's concurrency
    group may have none yet, but it waits behind at most one run of about
    half an hour, so it never ages into the check.
    """
    return not jobs


def parse_active_runs(
    runs: list[dict],
    releasing: frozenset[int] = frozenset(),
    ghosts: frozenset[int] = frozenset(),
) -> list[ActiveRun]:
    """The runner runs that are not yet `completed`.

    ``releasing`` names the ones :func:`is_releasing` found in their last
    job; they are listed, but do not hold a slot. ``ghosts`` names the ones
    :func:`is_ghost` found to be no run at all; they are not listed.
    """
    active = []
    for run in runs:
        if run.get("status") not in ACTIVE_RUN_STATUSES:
            continue
        if int(run["id"]) in ghosts:
            continue
        title = run.get("display_title") or run.get("name") or ""
        pr = _RUN_PR.search(title)
        slot = _RUN_SLOT.search(title)
        active.append(
            ActiveRun(
                run_id=int(run["id"]),
                # A hand-dispatched run says `slot manual`. It has a
                # concurrency group of its own, but it is real load on the
                # workspace, so it is counted as holding slot 1.
                slot=int(slot.group(1)) if slot else 1,
                pr=int(pr.group(1)) if pr else None,
                releasing=int(run["id"]) in releasing,
            )
        )
    return active


def should_enqueue(state: str | None) -> bool:
    """Whether to post `Queued` on a head whose latest status is this.

    Not over a pending status of any kind. Over `Queued` it would reset the
    timestamp the queue is ordered by and send the head to the back. Over
    `Dispatched`/`Running` it would be erased by that run's verdict, or with
    more than one slot admit the same head twice; the run in flight already
    answers for this head. ``state`` is the REST spelling, lower case, or
    None for a head with no such status. Mirrored in bash by the request
    step in `modal-live.yml`.
    """
    return state != "pending"


def parse_default_branch(data: dict) -> Request | None:
    """The weekly request: a pending status among the default branch's
    recent commits, newest first.

    Not only on the head. The weekly run queues the head it found, and the
    branch can move before that request is admitted -- a merge while PRs
    are ahead of it is the normal case -- after which nothing would read the
    old head again and the weekly run would vanish without a trace. Testing
    the commit it was queued on is fine for its purpose (drift underneath
    us, not the newest code). Bounded by ``DEFAULT_BRANCH_DEPTH``.
    """
    ref = data["data"]["repository"].get("defaultBranchRef")
    if not ref or not ref.get("target"):
        return None
    for commit in ref["target"]["history"]["nodes"]:
        status = (commit.get("status") or {}).get("context")
        if not status or status["state"] != "PENDING":
            continue
        return Request(
            pr=None,
            sha=commit["oid"],
            branch=ref["name"],
            state=status["state"],
            description=status.get("description") or "",
            created_at=status["createdAt"],
            cross_repo=False,
            author="",
        )
    return None


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
    # Keyed by PR, with None standing for the default branch.
    #
    # A releasing run still counts for staleness: it has yet to post its
    # verdict, and calling its head stale would race that verdict with an
    # error.
    reporting = {run.pr for run in active}
    # But not for admission: its stack is gone. A PR with a run that still
    # holds a slot waits, so two slots never run one PR twice (a push while
    # its previous head runs queues the new head behind it).
    holding = {run.pr for run in active if not run.releasing}
    occupied = {run.slot for run in active if not run.releasing}
    free = [s for s in range(1, slots + 1) if s not in occupied]

    # One commit status per SHA and context, however many PRs carry it: two
    # open PRs can share a head (a branch and a branch stacked on it, before
    # the second gets its own commit). So a head is answered for by any of
    # its PRs' runs, and is admitted once.
    by_head: dict[tuple[str, str], list[Request]] = {}
    for r in requests:
        by_head.setdefault((r.sha, r.context), []).append(r)

    def head_prs(r: Request) -> set[int | None]:
        return {other.pr for other in by_head[(r.sha, r.context)]}

    stale = []
    seen_stale: set[tuple[str, str]] = set()
    for r in requests:
        key = (r.sha, r.context)
        if r.admitted and not head_prs(r) & reporting and key not in seen_stale:
            seen_stale.add(key)
            stale.append(r)

    refused: list[tuple[Request, str]] = []
    eligible: list[Request] = []
    # Oldest first. The PR number only breaks a tie within one second, and
    # picks which PR of a shared head is the one dispatched.
    seen: set[tuple[str, str]] = set()
    for request in sorted(
        (r for r in requests if r.queued),
        key=lambda r: (r.created_at, r.pr if r.pr is not None else 0),
    ):
        if (request.sha, request.context) in seen:
            continue
        seen.add((request.sha, request.context))
        reason = refusal(request)
        if reason:
            refused.append((request, reason))
        else:
            eligible.append(request)

    admissible = [r for r in eligible if not head_prs(r) & holding]
    dispatch = list(zip(admissible, free))
    admitted = {id(r) for r, _ in dispatch}
    return Plan(
        dispatch=dispatch,
        stale=stale,
        refused=refused,
        waiting=[r for r in eligible if id(r) not in admitted],
    )


# --- GitHub side ---------------------------------------------------------

_REQUESTS_QUERY = """
query(
  $owner: String!, $name: String!, $context: String!, $weekly: String!,
  $depth: Int!, $after: String
) {
  repository(owner: $owner, name: $name) {
    defaultBranchRef {
      name
      target { ... on Commit { history(first: $depth) { nodes {
        oid status { context(name: $weekly) { state description createdAt } }
      } } } }
    }
    pullRequests(states: OPEN, first: 100, after: $after) {
      pageInfo { hasNextPage endCursor }
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
    """The runner's runs that are not completed, however old.

    Asked for by status rather than read off the newest runs: the list is
    ordered by creation, so a re-run of an older run could sit behind a page
    of newer completed ones, and its slot would read as free. Few runs are
    ever active, so each status is one short page.
    """
    base = f"repos/{repo}/actions/workflows/{RUNNER_WORKFLOW}/runs"
    runs: dict[int, dict] = {}
    # In lifecycle order: a run that moves on between two queries moves to
    # a status queried later, so it is not missed.
    for status in ACTIVE_RUN_LIFECYCLE:
        out = _gh("api", f"{base}?status={status}&per_page=100")
        for run in json.loads(out)["workflow_runs"]:
            runs[int(run["id"])] = run
    # Then the newest runs whatever their status, last, as the current view
    # of every recent run: it settles any run whose status changed during
    # the queries above, including one that has since completed.
    for run in json.loads(_gh("api", f"{base}?per_page=100"))["workflow_runs"]:
        runs[int(run["id"])] = run
    return list(runs.values())


def _run_jobs(repo: str, run_id: int) -> list[dict]:
    out = _gh("api", f"repos/{repo}/actions/runs/{run_id}/jobs?per_page=100")
    return json.loads(out)["jobs"]


def _post_status(
    repo: str,
    sha: str,
    state: str,
    description: str,
    url: str,
    context: str = CONTEXT,
) -> None:
    _gh(
        "api",
        f"repos/{repo}/statuses/{sha}",
        "-f",
        f"state={state}",
        "-f",
        f"context={context}",
        "-f",
        f"description={description[:140]}",
        "-f",
        f"target_url={url}",
    )


def _query_pages(owner: str, name: str) -> list[dict]:
    """Every page of the request query.

    All of them, not the first: a queued head on page two would otherwise
    never be admitted, however free the slots, until enough PRs closed.
    """
    pages = []
    after: str | None = None
    while True:
        args = [
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
            "-F",
            f"weekly={WEEKLY_CONTEXT}",
            "-F",
            f"depth={DEFAULT_BRANCH_DEPTH}",
        ]
        if after:
            args += ["-F", f"after={after}"]
        page = json.loads(_gh(*args))
        pages.append(page)
        info = page["data"]["repository"]["pullRequests"]["pageInfo"]
        if not info["hasNextPage"]:
            return pages
        after = info["endCursor"]


def _default_branch(repo: str, given: str) -> str:
    # A `schedule` event's payload carries no repository, so the workflow
    # may pass an empty default branch.
    return given or _gh("api", f"repos/{repo}", "-q", ".default_branch").strip()


def _run_url(repo: str) -> str:
    return (
        f"{os.environ.get('GITHUB_SERVER_URL', 'https://github.com')}/{repo}"
        f"/actions/runs/{os.environ.get('GITHUB_RUN_ID', '')}"
    )


def enqueue_weekly(args: argparse.Namespace) -> int:
    """Record the weekly request, and nothing else.

    Its own job, outside the selector's concurrency group: a pending
    selector can be cancelled by the next one, and only the Monday run
    knows to record this, so recording it there would lose the week's run
    whenever the Monday selector happened to queue behind another.
    """
    repo = args.repo
    owner, name = repo.split("/")
    branch = _default_branch(repo, args.default_branch)
    # Coalesced over the whole searched history, not just the head: a weekly
    # request still pending on an older commit is the same request.
    pending = parse_default_branch(_query_pages(owner, name)[0])
    if pending is not None:
        line = f"- {pending.label}: already {pending.description}; not queued again"
    else:
        sha = _gh("api", f"repos/{repo}/commits/{branch}", "-q", ".sha").strip()
        _post_status(
            repo,
            sha,
            "pending",
            f"{QUEUED} by the weekly schedule.",
            _run_url(repo),
            context=WEEKLY_CONTEXT,
        )
        line = f"- {branch} {sha[:7]}: queued by the weekly schedule"
    _summary(["### Registry-live weekly request", "", line])
    return 0


def _dispatch_ref(repo: str, request: Request, default_branch: str) -> str:
    """The ref whose `registry-live.yml` should run this head.

    The PR's own branch, so the tier's steps come from the same commit as
    the code they test: a PR that edits the tier tests its own edit, and the
    merge-ref/head mismatch STA-92 diagnosed cannot arise. A branch older
    than the runner has no such file, and falls back to the default
    branch's runner, which checks out the head by SHA.
    """
    if request.pr is None:
        return default_branch
    path = f".github/workflows/{RUNNER_WORKFLOW}"
    if _gh_ok("api", f"repos/{repo}/contents/{path}?ref={request.sha}", "--silent"):
        return request.branch
    return default_branch


def _await_visible(repo: str, pr: int | None, known: set[int]) -> bool:
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
    run_url = _run_url(repo)
    default_branch = _default_branch(repo, args.default_branch)

    lines = ["### Registry-live queue", ""]

    runs = _runner_runs(repo)
    releasing = frozenset(
        int(run["id"])
        for run in runs
        if run.get("status") == "in_progress"
        and is_releasing(_run_jobs(repo, int(run["id"])))
    )
    ghosts = frozenset(
        int(run["id"])
        for run in runs
        if run.get("status") in ACTIVE_RUN_STATUSES
        and is_aged(run)
        and is_ghost(_run_jobs(repo, int(run["id"])))
    )
    active = parse_active_runs(runs, releasing, ghosts)
    pages = _query_pages(owner, name)
    requests = [r for page in pages for r in parse_requests(page)]
    default_head = parse_default_branch(pages[0])
    if default_head is not None:
        requests.append(default_head)
    the_plan = plan(requests, active, args.slots)

    lines += [
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
        lines.append(f"- {request.label}: runner lost without reporting")
        _post_status(
            repo,
            request.sha,
            "error",
            "The runner ended without reporting. Re-run the PR's Modal Live "
            "Tests to queue again.",
            run_url,
            context=request.context,
        )
    for request, reason in the_plan.refused:
        lines.append(f"- {request.label}: refused -- {reason}")
        _post_status(
            repo,
            request.sha,
            "error",
            f"Refused: {reason}",
            run_url,
            context=request.context,
        )

    for request, slot in the_plan.dispatch:
        ref = _dispatch_ref(repo, request, default_branch)
        known = {run.run_id for run in parse_active_runs(_runner_runs(repo))}
        # No `pr` for the default branch's head: the runner reads "a SHA and
        # no PR" as the weekly run, in the permanent `ci-main`.
        pr_input = [] if request.pr is None else ["-f", f"pr={request.pr}"]

        # `Dispatched` only once the dispatch call has succeeded. Marked
        # first, a failed call would leave a head admitted with no runner,
        # which the next selector rightly reads as a lost run and errors.
        # Left `Queued`, the head keeps its original timestamp, so its place,
        # and the next selector simply tries again.
        def run_on(ref: str) -> None:
            _gh(
                "workflow",
                "run",
                RUNNER_WORKFLOW,
                "--repo",
                repo,
                "--ref",
                ref,
                *pr_input,
                "-f",
                f"sha={request.sha}",
                "-f",
                f"slot={slot}",
            )

        try:
            try:
                run_on(ref)
            except subprocess.CalledProcessError as e:
                if ref == default_branch:
                    raise
                # A PR branch's copy of the runner can be unrunnable -- its
                # `workflow_dispatch` removed, the file invalid -- and that
                # fails the same way every time. Left queued, it would sit at
                # the head of the queue and block every PR behind it. The
                # default branch's runner tests the head by SHA instead, as
                # it does for a branch older than the runner.
                print(
                    f"::warning title=Registry-live dispatch fell back::"
                    f"{request.label}: dispatch on {ref} failed "
                    f"({(e.stderr or '').strip()[:200]}); using {default_branch}."
                )
                ref = default_branch
                run_on(ref)
        except subprocess.CalledProcessError as e:
            # The default branch's runner failing to dispatch is not this
            # head's fault, and is the transient case: leave it queued.
            print(
                f"::warning title=Registry-live dispatch failed::{request.label}: "
                f"{(e.stderr or '').strip()[:300]} -- left queued; the next "
                "selector retries."
            )
            lines.append(f"- {request.label}: dispatch failed, left queued")
            continue
        # The runner can in principle post `Running` before this lands, and
        # be set back to `Dispatched`. Harmless: both read as admitted, and
        # the run is listed, so neither is taken for a lost runner.
        _post_status(
            repo,
            request.sha,
            "pending",
            f"{DISPATCHED} to slot {slot}; waiting for the runner to start.",
            run_url,
            context=request.context,
        )
        # Not done until the run is listed: the next selector reads the
        # slot as free until then, and would dispatch a second run into it.
        if _await_visible(repo, request.pr, known):
            lines.append(f"- {request.label}: dispatched to slot {slot} (ref {ref})")
        else:
            lines.append(f"- {request.label}: dispatch not confirmed")
            _post_status(
                repo,
                request.sha,
                "error",
                "The runner was dispatched but never appeared. Re-run the "
                "PR's Modal Live Tests to queue again.",
                run_url,
                context=request.context,
            )

    for position, request in enumerate(the_plan.waiting, start=1):
        lines.append(
            f"- {request.label}: waiting, position {position} "
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
    p.add_argument("--default-branch", default="", help="Looked up when empty.")
    p.add_argument("--slots", type=int, default=1)
    w = sub.add_parser("enqueue-weekly", help="Queue the default branch's head.")
    w.add_argument("--repo", required=True)
    w.add_argument("--default-branch", default="", help="Looked up when empty.")
    args = parser.parse_args(argv)
    if args.command == "enqueue-weekly":
        return enqueue_weekly(args)
    if args.slots < 1:
        parser.error("--slots must be at least 1")
    return dispatch(args)


if __name__ == "__main__":
    sys.exit(main())
