"""Longest first: the order the tier's scenarios are handed to xdist.

The tier's wall clock is its critical path, and under alphabetical order
that path started late: ``test_s7`` was the 24th of 34 items and began ~4.5
minutes into the attempt, behind short scenarios that could have run any
time. Each scenario declares a **budget** (``pytest.mark.budget(seconds)``,
its expected wall clock), and the collection is reordered so the long ones
start at t=0.

**Plain longest-first is not enough under ``--dist load``, and this module
exists because of how.** ``LoadScheduling`` hands each worker a *chunk* of
consecutive items up front (at least two, since a worker only runs an item
once it holds the next one), and refills a worker only when it drops below
two pending. So with longest-first order the two longest scenarios both land
on ``gw0``, back to back, and the second starts when the first ends -- the
critical path is their sum. ``worksteal`` has the same shape (contiguous
initial split, steals from the tail).

So the order is *planned* against a model of ``LoadScheduling``
(``simulate_load``, which mirrors ``xdist/scheduler/load.py`` as of 3.8):
start from longest-first, then swap and move items while the modelled
makespan improves. The result is not "longest first" in the literal sense:
the short scenarios go early so their workers turn over quickly, and each
long one is placed where it starts soonest without leaving work stranded
behind it. The plan is only as good as the budgets, which is why the run
prints budget against actual for every scenario.

The model is deterministic and the order a pure function of the collected
items, their budgets and the worker count -- which xdist requires, since
every worker collects independently and must arrive at the same order.
"""

from __future__ import annotations

import heapq
import random
from collections import deque
from collections.abc import Sequence


def controller_dist_mode(workerinput: dict | None) -> str | None:
    """The xdist controller's ``--dist`` mode, as seen from a worker.

    Not ``config.getoption("dist")``: xdist resets it to ``"no"`` in every
    worker (``remote.setup_config``), so a worker reading it would always
    conclude it is not distributed. The controller's command line is in
    ``workerinput["mainargv"]``; ``-n`` alone means ``load``.
    """
    if not workerinput:
        return None
    argv = list(workerinput.get("mainargv") or [])
    mode = "load"
    for index, arg in enumerate(argv):
        if arg == "--dist" and index + 1 < len(argv):
            mode = argv[index + 1]
        elif arg.startswith("--dist="):
            mode = arg.split("=", 1)[1]
    return mode


def initial_chunk(n_items: int, n_workers: int, maxschedchunk: int | None) -> int:
    """How many items ``LoadScheduling.schedule`` sends each worker up front.

    0 means the round-robin branch: fewer than two items per worker, so every
    item is sent at once, one per worker in turn.
    """
    if n_items < 2 * n_workers:
        return 0
    chunk_cap = n_items if maxschedchunk is None else maxschedchunk
    return max(min((n_items // n_workers) // 4, chunk_cap), 2)


def simulate_load(
    durations: Sequence[float],
    n_workers: int,
    *,
    maxschedchunk: int | None = None,
) -> tuple[float, list[float]]:
    """``(makespan, start time of each item)`` under ``--dist load``.

    Items are in collection order. A worker runs its queue in order; the
    controller refills it on each completion by the rules of
    ``LoadScheduling.check_schedule``.
    """
    n = len(durations)
    starts = [0.0] * n
    if n == 0:
        return 0.0, starts
    workers = max(1, min(n_workers, n)) if n_workers > 0 else 1
    cap = n if maxschedchunk is None else maxschedchunk

    pending: deque[int] = deque(range(n))
    queues: list[deque[int]] = [deque() for _ in range(workers)]
    chunk = initial_chunk(n, workers, maxschedchunk)
    if chunk == 0:
        worker = 0
        while pending:
            queues[worker].append(pending.popleft())
            worker = (worker + 1) % workers
    else:
        for queue in queues:
            for _ in range(chunk):
                if pending:
                    queue.append(pending.popleft())

    # (finish time, worker) of each worker's running item.
    running: list[tuple[float, int]] = []
    for worker, queue in enumerate(queues):
        if queue:
            starts[queue[0]] = 0.0
            heapq.heappush(running, (durations[queue[0]], worker))
    makespan = 0.0
    while running:
        now, worker = heapq.heappop(running)
        makespan = max(makespan, now)
        queue = queues[worker]
        queue.popleft()
        if pending:
            low = max(2, len(pending) // workers // 4)
            high = max(2, len(pending) // workers // 2)
            # xdist skips a node that still holds two items after a test of
            # 0.1 s or more -- every scenario here -- as "long-running".
            if len(queue) < low and len(queue) < 2:
                send = min(high - len(queue), max(2 - len(queue), cap))
                for _ in range(send):
                    if pending:
                        queue.append(pending.popleft())
        if queue:
            starts[queue[0]] = now
            heapq.heappush(running, (now + durations[queue[0]], worker))
    return makespan, starts


def plan_order(
    budgets: Sequence[float],
    n_workers: int,
    *,
    maxschedchunk: int | None = None,
    max_passes: int = 20,
    samples: int = 8,
    spread: float = 0.2,
) -> list[int]:
    """A permutation of ``range(len(budgets))`` to hand xdist: longest first,
    then improved by swaps and moves against ``simulate_load``.

    **Scored on perturbed budgets, not the budgets themselves.** A plan tuned
    to exact durations is knife-edge: two workers freeing at the same second
    decide by tie-break which of them is handed the next item, and the loser
    of that tie in real life queues it behind a long scenario -- measured, it
    turned a modelled 390 s into 540 s. Budgets are estimates anyway. So a
    candidate's cost is its worst makespan over ``samples`` copies of the
    budgets each scaled by a factor in ``1 +/- spread`` (then the mean, to
    break ties). The perturbations come from a fixed seed, so the plan stays
    deterministic, as xdist requires.
    """
    order = sorted(range(len(budgets)), key=lambda i: (-budgets[i], i))
    if n_workers <= 1 or len(order) <= 1:
        return order

    rng = random.Random(0)
    scenarios = [list(budgets)] + [
        [b * rng.uniform(1 - spread, 1 + spread) for b in budgets]
        for _ in range(max(0, samples - 1))
    ]

    def cost(candidate: list[int]) -> tuple[float, float]:
        makespans = [
            simulate_load(
                [durations[i] for i in candidate],
                n_workers,
                maxschedchunk=maxschedchunk,
            )[0]
            for durations in scenarios
        ]
        return max(makespans), sum(makespans) / len(makespans)

    best = cost(order)
    for _ in range(max_passes):
        improved = False
        # Swaps, then moves: a swap exchanges two slots' work, a move shifts
        # every item between them by one -- which is what re-pairs a whole
        # run of initial chunks at once.
        for i in range(len(order)):
            for j in range(i + 1, len(order)):
                if budgets[order[i]] == budgets[order[j]]:
                    continue
                order[i], order[j] = order[j], order[i]
                candidate = cost(order)
                if candidate < best:
                    best = candidate
                    improved = True
                else:
                    order[i], order[j] = order[j], order[i]
        for i in range(len(order)):
            for j in range(len(order)):
                if i == j:
                    continue
                moved = order[:]
                moved.insert(j, moved.pop(i))
                candidate = cost(moved)
                if candidate < best:
                    best = candidate
                    order = moved
                    improved = True
        if not improved:
            break
    return order
