"""The registry-live tier's gates and its longest-first ordering.

Pure logic, so it lives here rather than under ``tests_registry_live/``,
for the reason ``test_registry_live_diagnostics`` gives: this directory runs
on every pull request, and both of these can go wrong *silently*. A gate
that opened on a read fault would let a scenario stop testing anything
while still passing; an ordering that put the two longest scenarios on one
worker would only make the tier slow again.

Needs none of this directory's docker-compose services, and no Modal.
"""

from __future__ import annotations

import pytest

from stardag_integration_tests.registry_live._gates import hold
from stardag_integration_tests.registry_live._ordering import (
    controller_dist_mode,
    initial_chunk,
    plan_order,
    simulate_load,
)


class _Clock:
    """A fake monotonic clock that ``sleep`` advances."""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


def _hold(gate: str, bound: float, read, *, records=None):
    clock = _Clock()
    written = [] if records is None else records
    outcome = hold(
        gate,
        bound,
        read=read,
        record=lambda key, value: written.append(value),
        clock=clock,
        sleep=clock.sleep,
        poll_interval=1.0,
    )
    return outcome, clock.now, written


class TestHold:
    def test_an_empty_gate_is_the_old_sleep(self):
        reads = []
        outcome, waited, records = _hold("", 150, reads.append)
        assert (outcome, waited) == ("ungated", 150)
        assert reads == [] and records == []

    def test_a_released_gate_ends_the_hold_at_the_next_poll(self):
        outcome, waited, records = _hold("g", 150, _released_on_read(4))
        assert outcome == "released"
        assert waited == 3
        assert records[0]["outcome"] == "released"
        assert records[0]["gate"] == "g"

    def test_a_gate_released_before_the_hold_does_not_wait(self):
        outcome, waited, _ = _hold("g", 150, lambda key: True)
        assert (outcome, waited) == ("released", 0)

    def test_a_gate_never_released_holds_for_its_bound_and_no_longer(self):
        outcome, waited, records = _hold("g", 150, lambda key: False)
        assert outcome == "bound"
        assert waited == 150
        assert records[0]["outcome"] == "bound"

    def test_a_read_fault_is_a_closed_gate_never_an_open_one(self):
        def read(key: str) -> bool:
            raise ConnectionError("control plane unavailable")

        outcome, waited, records = _hold("g", 20, read)
        assert outcome == "bound"
        assert waited == 20
        assert records[0]["failed_reads"] == 21

    def test_reads_recover_after_a_fault(self):
        answers = iter([ConnectionError("blip"), False, True])

        def read(key: str) -> bool:
            answer = next(answers)
            if isinstance(answer, Exception):
                raise answer
            return answer

        outcome, waited, records = _hold("g", 150, read)
        assert (outcome, waited) == ("released", 2)
        assert records[0]["failed_reads"] == 1

    def test_a_failed_record_does_not_change_the_outcome(self):
        clock = _Clock()

        def record(key, value):
            raise ConnectionError("cannot write")

        outcome = hold(
            "g",
            10,
            read=lambda key: True,
            record=record,
            clock=clock,
            sleep=clock.sleep,
        )
        assert outcome == "released"


def _released_on_read(n: int):
    """A gate that reads as released from its ``n``-th read on."""
    reads = iter(range(1, 10_000))
    return lambda key: next(reads) >= n


class TestInitialChunk:
    def test_fewer_than_two_items_per_worker_is_round_robin(self):
        assert initial_chunk(23, 12, None) == 0

    def test_otherwise_at_least_two_each(self):
        assert initial_chunk(34, 12, None) == 2

    def test_a_large_collection_gets_a_quarter_of_its_share(self):
        assert initial_chunk(400, 10, None) == 10


class TestSimulateLoad:
    def test_round_robin_starts_every_item_at_once(self):
        makespan, starts = simulate_load([5, 4, 3], 4)
        assert makespan == 5 and starts == [0, 0, 0]

    def test_a_worker_runs_its_chunk_back_to_back(self):
        # Two workers, chunk two: items 0,1 on one worker, 2,3 on the other.
        makespan, starts = simulate_load([10, 10, 1, 1], 2)
        assert starts == [0, 10, 0, 1]
        assert makespan == 20


# The shape of the tier: a few long scenarios, many short ones, 12 workers.
TIER = [330, 300, 250, 250, 240, 200, 200, 200, 180, 180, 150, 150]
TIER += [150, 150, 120, 120, 120, 100, 100, 100, 90, 90, 90, 80, 80]
TIER += [60, 60, 60, 50, 40, 30, 30, 20, 20]


class TestPlanOrder:
    def test_it_is_a_permutation(self):
        order = plan_order(TIER, 12)
        assert sorted(order) == list(range(len(TIER)))

    def test_it_is_deterministic(self):
        assert plan_order(TIER, 12) == plan_order(list(TIER), 12)

    def test_plain_longest_first_serialises_the_two_longest(self):
        """The reason the order is planned rather than sorted."""
        longest_first = sorted(TIER, reverse=True)
        makespan, starts = simulate_load(longest_first, 12)
        assert starts[1] == longest_first[0]  # the 2nd waits for the 1st
        assert makespan >= longest_first[0] + longest_first[1]

    def test_the_plan_is_close_to_the_lower_bound(self):
        order = plan_order(TIER, 12)
        planned, _ = simulate_load([TIER[i] for i in order], 12)
        bound = max(max(TIER), sum(TIER) / 12)
        assert planned <= 1.2 * bound, (planned, bound)

    def test_the_plan_beats_longest_first(self):
        order = plan_order(TIER, 12)
        planned, _ = simulate_load([TIER[i] for i in order], 12)
        naive, _ = simulate_load(sorted(TIER, reverse=True), 12)
        assert planned < naive
        # Never better than the lower bounds, which keeps the model honest.
        assert planned >= max(max(TIER), sum(TIER) / 12)

    def test_the_plan_survives_budgets_that_are_off(self):
        """Budgets are estimates, and a plan tuned to exact ones is knife-edge:
        a tie between two freeing workers decides which one queues the next
        item behind a long scenario."""
        import random

        order = plan_order(TIER, 12)
        bound = max(max(TIER), sum(TIER) / 12)
        rng = random.Random(42)
        for _ in range(10):
            actual = [b * rng.uniform(0.9, 1.1) for b in TIER]
            makespan, _ = simulate_load([actual[i] for i in order], 12)
            assert makespan <= 1.3 * bound, (makespan, bound)

    @pytest.mark.parametrize("workers", [0, 1])
    def test_serial_is_longest_first(self, workers):
        assert plan_order([1, 3, 2], workers) == [1, 2, 0]


class TestControllerDistMode:
    """xdist resets ``--dist`` to "no" in every worker, so a worker reads the
    controller's from its command line."""

    def test_not_under_xdist(self):
        assert controller_dist_mode(None) is None

    def test_n_alone_is_load(self):
        assert controller_dist_mode({"mainargv": ["pytest", "-n", "12"]}) == "load"

    @pytest.mark.parametrize(
        "argv",
        [["pytest", "-n", "4", "--dist", "worksteal"], ["pytest", "--dist=worksteal"]],
    )
    def test_an_explicit_mode_is_read(self, argv):
        assert controller_dist_mode({"mainargv": argv}) == "worksteal"
