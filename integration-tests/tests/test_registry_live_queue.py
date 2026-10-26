"""The registry-live queue's admission logic (STA-138).

Pure logic, so it lives here for the reason ``test_registry_live_gates``
gives: this directory runs on every pull request. A queue that admitted out
of order, filled a slot twice or handed credentials to a fork head would do
it silently -- the tier would still go green.

Needs none of this directory's docker-compose services, and no GitHub.
"""

from __future__ import annotations

from datetime import datetime, timezone

from stardag_integration_tests.registry_live._queue import (
    CONTEXT,
    REPORT_JOB,
    WEEKLY_CONTEXT,
    ActiveRun,
    Request,
    is_releasing,
    parse_active_runs,
    parse_default_branch,
    parse_requests,
    plan,
    should_enqueue,
)


def _request(
    pr: int,
    created_at: str,
    description: str = "Queued for the workspace-wide registry-live tier.",
    *,
    state: str = "PENDING",
    cross_repo: bool = False,
    author: str = "andhus",
    labels: frozenset[str] = frozenset(),
) -> Request:
    return Request(
        pr=pr,
        sha=f"sha{pr}",
        branch=f"branch-{pr}",
        state=state,
        description=description,
        created_at=created_at,
        cross_repo=cross_repo,
        author=author,
        labels=labels,
    )


def _main(
    created_at: str, description: str = "Queued by the weekly schedule."
) -> Request:
    return Request(
        pr=None,
        sha="mainsha",
        branch="main",
        state="PENDING",
        description=description,
        created_at=created_at,
        cross_repo=False,
        author="",
    )


def _pr_node(
    number: int, head: str, status: dict | None, last_commit: str | None = None
):
    return {
        "number": number,
        "headRefName": f"branch-{number}",
        "headRefOid": head,
        "isCrossRepository": False,
        "author": {"login": "andhus"},
        "labels": {"nodes": [{"name": "modal-live"}]},
        "commits": {
            "nodes": [
                {
                    "commit": {
                        "oid": last_commit or head,
                        "status": {"context": status} if status is not None else None,
                    }
                }
            ]
        },
    }


def _graphql(*nodes, default_branch=None):
    return {
        "data": {
            "repository": {
                "defaultBranchRef": default_branch,
                "pullRequests": {
                    "pageInfo": {"hasNextPage": False},
                    "nodes": list(nodes),
                },
            }
        }
    }


class TestParseActiveRuns:
    def test_reads_pr_and_slot_from_the_run_name(self):
        runs = [
            {
                "id": 1,
                "status": "in_progress",
                "display_title": "Registry-live tier · PR #482 · slot 2",
            },
            {
                "id": 2,
                "status": "queued",
                "display_title": "Registry-live tier · main · slot 1",
            },
        ]
        assert parse_active_runs(runs) == [
            ActiveRun(run_id=1, slot=2, pr=482),
            ActiveRun(run_id=2, slot=1, pr=None),
        ]

    def test_completed_runs_hold_no_slot(self):
        runs = [{"id": 1, "status": "completed", "display_title": "… · PR #1 · slot 1"}]
        assert parse_active_runs(runs) == []

    def test_a_releasing_run_is_listed_but_flagged(self):
        runs = [
            {"id": 7, "status": "in_progress", "display_title": "… · PR #1 · slot 1"}
        ]
        assert parse_active_runs(runs, releasing=frozenset({7})) == [
            ActiveRun(run_id=7, slot=1, pr=1, releasing=True)
        ]

    def test_a_ghost_run_holds_no_slot(self):
        # GitHub can keep a run listed as queued for weeks after it ended;
        # counting it would wedge its slot for good.
        now = datetime(2026, 10, 26, 16, 0, tzinfo=timezone.utc)
        runs = [
            {
                "id": 1,
                "status": "queued",
                "created_at": "2026-09-13T10:00:00Z",
                "display_title": "… · PR #1 · slot 1",
            },
            {
                "id": 2,
                "status": "in_progress",
                "created_at": "2026-10-26T15:40:00Z",
                "display_title": "… · PR #2 · slot 1",
            },
        ]
        assert [r.run_id for r in parse_active_runs(runs, now=now)] == [2]

    def test_a_hand_dispatched_run_holds_slot_one(self):
        # Its own concurrency group, but real load on the workspace.
        runs = [
            {
                "id": 4,
                "status": "in_progress",
                "display_title": "Registry-live tier · main · slot manual",
            }
        ]
        assert parse_active_runs(runs) == [ActiveRun(run_id=4, slot=1, pr=None)]

    def test_a_title_without_a_slot_occupies_slot_one(self):
        runs = [{"id": 3, "status": "waiting", "display_title": "Registry-live tier"}]
        assert parse_active_runs(runs) == [ActiveRun(run_id=3, slot=1, pr=None)]


class TestIsReleasing:
    def _jobs(self, **statuses: str) -> list[dict]:
        names = {
            "resolve": "Resolve the head to test",
            "tier": "Registry-live tier",
            "teardown": "Delete the run's Modal environment",
            "report": REPORT_JOB,
        }
        return [{"name": names[k], "status": v} for k, v in statuses.items()]

    def test_only_the_report_job_left_is_releasing(self):
        # The hand-over is dispatched from that job, so the run is still in
        # progress when its selector reads it -- and its stack is gone.
        jobs = self._jobs(
            resolve="completed",
            tier="completed",
            teardown="completed",
            report="in_progress",
        )
        assert is_releasing(jobs)

    def test_a_run_still_tearing_down_holds_its_slot(self):
        jobs = self._jobs(
            resolve="completed",
            tier="completed",
            teardown="in_progress",
            report="queued",
        )
        assert not is_releasing(jobs)

    def test_a_run_whose_jobs_are_not_listed_yet_holds_its_slot(self):
        # Before GitHub has created the report job, nothing says the run is
        # finishing; reading it as releasing would fill its slot twice.
        assert not is_releasing(self._jobs(resolve="in_progress"))
        assert not is_releasing([])


class TestParseRequests:
    def test_reads_the_status_on_the_current_head(self):
        status = {
            "state": "PENDING",
            "description": "Queued …",
            "createdAt": "2026-10-26T14:00:00Z",
        }
        [request] = parse_requests(_graphql(_pr_node(5, "abc", status)))
        assert (request.pr, request.sha, request.queued) == (5, "abc", True)
        assert request.labels == frozenset({"modal-live"})

    def test_a_head_without_the_status_is_not_a_request(self):
        assert parse_requests(_graphql(_pr_node(5, "abc", None))) == []

    def test_a_status_on_a_superseded_commit_is_ignored(self):
        # The PR's last listed commit is not its head: whatever status it
        # carries belongs to a head that a push has since replaced.
        status = {
            "state": "PENDING",
            "description": "Queued …",
            "createdAt": "2026-10-26T14:00:00Z",
        }
        node = _pr_node(5, "new", status, last_commit="old")
        assert parse_requests(_graphql(node)) == []


def _history(*commits):
    return {"name": "main", "target": {"history": {"nodes": list(commits)}}}


def _commit(oid, state=None, description="Queued by the weekly schedule."):
    status = (
        {
            "context": {
                "state": state,
                "description": description,
                "createdAt": "2026-10-26T03:17:05Z",
            }
        }
        if state
        else None
    )
    return {"oid": oid, "status": status}


class TestParseDefaultBranch:
    def test_reads_the_weekly_request_on_the_head(self):
        request = parse_default_branch(
            _graphql(default_branch=_history(_commit("m1", "PENDING")))
        )
        assert request is not None
        assert (request.pr, request.sha, request.queued) == (None, "m1", True)

    def test_finds_the_request_after_the_branch_moved(self):
        # Queued on m1; a merge made m2 the head before it was admitted.
        # Reading only the head would drop the weekly run without a trace.
        data = _graphql(
            default_branch=_history(_commit("m2"), _commit("m1", "PENDING"))
        )
        request = parse_default_branch(data)
        assert request is not None and request.sha == "m1"

    def test_the_weekly_request_reports_under_its_own_context(self):
        # PR heads merged with a merge commit bring their `Registry-live
        # tier` statuses into the default branch's history; a separate
        # context is what keeps those from reading as the weekly request.
        assert _main("2026-10-26T03:17:05Z").context == WEEKLY_CONTEXT
        assert _request(1, "2026-10-26T14:00:00Z").context == CONTEXT

    def test_finished_weekly_runs_are_not_requests(self):
        data = _graphql(
            default_branch=_history(_commit("m2"), _commit("m1", "SUCCESS", "Passed."))
        )
        assert parse_default_branch(data) is None


class TestShouldEnqueue:
    def test_a_head_with_no_status_or_a_verdict_is_queued(self):
        for state in (None, "success", "failure", "error"):
            assert should_enqueue(state)

    def test_a_pending_head_is_not_queued_again(self):
        # Re-queueing a queued head would send it to the back; re-queueing a
        # running one would be erased by that run's verdict.
        assert not should_enqueue("pending")


class TestPlan:
    def test_admits_oldest_first_into_the_free_slot(self):
        requests = [
            _request(3, "2026-10-26T14:02:00Z"),
            _request(1, "2026-10-26T14:00:00Z"),
            _request(2, "2026-10-26T14:01:00Z"),
        ]
        result = plan(requests, active=[], slots=1)
        assert [(r.pr, slot) for r, slot in result.dispatch] == [(1, 1)]
        assert [r.pr for r in result.waiting] == [2, 3]

    def test_a_burst_of_ten_drains_in_order_one_at_a_time(self):
        # The issue's "done when", simulated: ten heads queued within a
        # minute, each hand-over admitting exactly the next one.
        queue = [_request(pr, f"2026-10-26T14:00:{pr:02d}Z") for pr in range(10, 0, -1)]
        order = []
        while queue:
            result = plan(queue, active=[], slots=1)
            assert len(result.dispatch) == 1
            admitted, _ = result.dispatch[0]
            order.append(admitted.pr)
            queue = [r for r in queue if r.pr != admitted.pr]
        assert order == list(range(1, 11))

    def test_no_free_slot_admits_nothing(self):
        result = plan(
            [_request(1, "2026-10-26T14:00:00Z")],
            active=[ActiveRun(run_id=9, slot=1, pr=99)],
            slots=1,
        )
        assert result.dispatch == []
        assert [r.pr for r in result.waiting] == [1]

    def test_the_weekly_run_holds_its_slot_too(self):
        result = plan(
            [_request(1, "2026-10-26T14:00:00Z")],
            active=[ActiveRun(run_id=9, slot=1, pr=None)],
            slots=1,
        )
        assert result.dispatch == []

    def test_two_slots_admit_two_and_skip_the_occupied_one(self):
        requests = [_request(pr, f"2026-10-26T14:00:0{pr}Z") for pr in (1, 2, 3)]
        result = plan(requests, active=[ActiveRun(run_id=9, slot=2, pr=99)], slots=3)
        assert [(r.pr, slot) for r, slot in result.dispatch] == [(1, 1), (2, 3)]
        assert [r.pr for r in result.waiting] == [3]

    def test_a_fork_head_is_refused_not_admitted(self):
        requests = [
            _request(1, "2026-10-26T14:00:00Z", cross_repo=True),
            _request(2, "2026-10-26T14:01:00Z"),
        ]
        result = plan(requests, active=[], slots=1)
        assert [r.pr for r, _ in result.refused] == [1]
        assert [r.pr for r, _ in result.dispatch] == [2]

    def test_dependabot_needs_the_label(self):
        requests = [
            _request(1, "2026-10-26T14:00:00Z", author="dependabot"),
            _request(
                2,
                "2026-10-26T14:01:00Z",
                author="dependabot",
                labels=frozenset({"modal-live"}),
            ),
        ]
        result = plan(requests, active=[], slots=1)
        assert [r.pr for r, _ in result.refused] == [1]
        assert [r.pr for r, _ in result.dispatch] == [2]

    def test_an_admitted_head_with_no_runner_is_stale(self):
        running = _request(1, "2026-10-26T14:00:00Z", "Running in slot 1.")
        lost = _request(2, "2026-10-26T14:00:00Z", "Dispatched to slot 1; …")
        result = plan(
            [running, lost], active=[ActiveRun(run_id=9, slot=1, pr=1)], slots=1
        )
        assert [r.pr for r in result.stale] == [2]

    def test_a_releasing_run_frees_its_slot_but_its_head_is_not_stale(self):
        # Its verdict is still to come, so its own "Running" head must not be
        # marked lost; the next request may take the slot meanwhile.
        own = _request(1, "2026-10-26T14:00:00Z", "Running in slot 1.")
        nxt = _request(2, "2026-10-26T14:01:00Z")
        result = plan(
            [own, nxt],
            active=[ActiveRun(run_id=9, slot=1, pr=1, releasing=True)],
            slots=1,
        )
        assert result.stale == []
        assert [(r.pr, slot) for r, slot in result.dispatch] == [(2, 1)]

    def test_a_pr_with_a_run_holding_a_slot_waits_for_it(self):
        # Two slots must not run one PR twice: a push while the previous head
        # runs queues the new head behind it, rather than beside it.
        result = plan(
            [_request(1, "2026-10-26T14:00:00Z"), _request(2, "2026-10-26T14:01:00Z")],
            active=[ActiveRun(run_id=9, slot=1, pr=1)],
            slots=2,
        )
        assert [(r.pr, slot) for r, slot in result.dispatch] == [(2, 2)]
        assert [r.pr for r in result.waiting] == [1]

    def test_the_weekly_request_queues_in_order_with_prs(self):
        result = plan(
            [_request(1, "2026-10-26T14:00:00Z"), _main("2026-10-26T03:17:05Z")],
            active=[],
            slots=1,
        )
        assert [(r.pr, slot) for r, slot in result.dispatch] == [(None, 1)]
        assert [r.pr for r in result.waiting] == [1]

    def test_an_admitted_weekly_head_with_no_runner_is_stale(self):
        lost = _main("2026-10-26T03:17:05Z", "Running in slot 1.")
        assert [r.pr for r in plan([lost], active=[], slots=1).stale] == [None]
        live = [ActiveRun(run_id=9, slot=1, pr=None)]
        assert plan([lost], active=live, slots=1).stale == []

    def test_a_releasing_run_and_its_pending_successor_hold_one_slot(self):
        # The hand-over: run 9 reports, run 10 was admitted into the same
        # slot and waits in its group. The slot is taken, by run 10.
        result = plan(
            [_request(3, "2026-10-26T14:02:00Z")],
            active=[
                ActiveRun(run_id=9, slot=1, pr=1, releasing=True),
                ActiveRun(run_id=10, slot=1, pr=2),
            ],
            slots=1,
        )
        assert result.dispatch == []
        assert [r.pr for r in result.waiting] == [3]

    def test_finished_heads_are_not_requests(self):
        done = _request(1, "2026-10-26T14:00:00Z", "Passed.", state="SUCCESS")
        result = plan([done], active=[], slots=1)
        assert (result.dispatch, result.stale, result.waiting) == ([], [], [])
