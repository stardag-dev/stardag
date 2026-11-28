"""A yield larger than one registration chunk registers and runs (STA-182).

A request registers at most ``MAX_CHUNK_ITEMS`` tasks, so a yield whose
children and static closure exceed that is sent in several requests. The
plan is sealed by the time anything runs, and a sealed plan takes new
structure only through ``/yield``: a closure sent anywhere else is refused,
and the yielding task fails with it. Only a yield that fit in one request
ever worked against a real registry, and the in-process fakes did not
model the seal, so nothing short of a real registry shows the split is
sent where the registry accepts it.

The DAG: a root yields ``CHILDREN`` children, each requiring leaves of its
own, so the yield is about 2.5 chunks. The first child's leaves alone span
more than a chunk, so the first request carries closure only: a registry
that accepts it gets no child in it, an older one is sent a stand-in (the
SDK covers both; this tier runs the branch's registry). The driver is
a resident build in this test process, with in-memory targets, so the
subject is the registration and not Modal: no worker container is involved.
The leaves are complete before the build (their targets written up front),
so the registry is told of them as observed complete and only the children
and the root run -- the closure still has to be registered, which is the
point, without a run per leaf. The closure is wide rather than the children
many for the same reason: every run is a claim and a report over the
network, and a first version with 1,250 children (one leaf each) took
nearly six minutes.

Observables: the build succeeds and the root's output is the sum over the
children; the build's plan is sealed with every member completed, yielded
children and leaves among them; and the root's yield arrived as more than
one applied ``/yield``, the first of them yielding nothing.
"""

from __future__ import annotations

import uuid

import pytest

import stardag as sd
from stardag_integration_tests.registry_live._events import (
    describe_events,
    task_events,
)
from stardag_integration_tests.registry_live._guard import registry_live_guard
from stardag_integration_tests.registry_live._harness import Deployment
from stardag_integration_tests.registry_live._wait import describe

registry_live_guard()

pytestmark = [
    pytest.mark.registry_live,
    pytest.mark.timeout(900),
]

# Children plus their leaves: 2,461 items, about 2.5 times the registry's
# per-request cap of 1000, so the yield spans three requests. The first
# child's leaves fill the first request on their own.
CHILDREN = 11
FIRST_CHILD_LEAVES = 1200
LEAVES_PER_CHILD = 125
LEAVES = FIRST_CHILD_LEAVES + (CHILDREN - 1) * LEAVES_PER_CHILD


def leaf_indices(child: int) -> range:
    """The indices of one child's own leaves."""
    if child == 0:
        return range(FIRST_CHILD_LEAVES)
    start = FIRST_CHILD_LEAVES + (child - 1) * LEAVES_PER_CHILD
    return range(start, start + LEAVES_PER_CHILD)


# How many tasks the driver runs at once. The children finish instantly;
# this bounds the claims in flight against the one registry container.
MAX_CONCURRENT_TASKS = 32


class ChunkLeaf(sd.Task[int]):
    """A child's own leaf; written before the build, never run by it."""

    salt: str
    index: int

    def run(self):
        self._save(self.index)


class ChunkChild(sd.Task[int]):
    salt: str
    index: int

    def requires(self):
        return [ChunkLeaf(salt=self.salt, index=j) for j in leaf_indices(self.index)]

    def run(self):
        self._save(sum(leaf.load() for leaf in self.requires()))


class ChunkRoot(sd.Task[int]):
    """Yields every child at once, then sums them."""

    salt: str
    children: int

    def run(self):
        kids = [ChunkChild(salt=self.salt, index=i) for i in range(self.children)]
        yield kids
        self._save(sum(kid.load() for kid in kids))


@pytest.mark.budget(60)
def test_a_yield_larger_than_a_chunk_registers_and_runs(
    deployment: Deployment,
) -> None:
    from stardag.build import BuildExitStatus, ConcurrencyConfig
    from stardag.registry import registry_provider
    from stardag.target import (
        InMemoryFileTarget,
        TargetFactory,
        target_factory_provider,
    )

    salt = uuid.uuid4().hex
    root = ChunkRoot(salt=salt, children=CHILDREN)
    prefix = "in-memory://"
    with (
        target_factory_provider.override(
            TargetFactory(
                target_roots={"default": prefix},
                prefix_to_target_prototype={prefix: InMemoryFileTarget},
            )
        ),
        InMemoryFileTarget.cleared(),
    ):
        for index in range(LEAVES):
            ChunkLeaf(salt=salt, index=index).run()
        summary = sd.build(
            root,
            concurrency_config=ConcurrencyConfig(
                max_concurrent_tasks=MAX_CONCURRENT_TASKS
            ),
            description="yield over the chunk cap",
        )
        build_id = summary.build_id
        assert build_id is not None
        assert summary.status == BuildExitStatus.SUCCESS, (
            f"{summary}\n{describe(build_id)}"
        )
        assert root.load() == sum(range(LEAVES))

    registry = registry_provider.get()
    assert registry.build_get(build_id).status == "completed", describe(build_id)
    # Sealed, and every member COMPLETED -- the yielded ones included.
    frontier = registry.build_get_frontier(build_id)
    assert frontier.sealed and frontier.plan_complete, frontier
    # Spot checks across the yield's three requests: the first and last
    # child and leaf, and one from the middle.
    for index in (0, CHILDREN // 2, CHILDREN - 1):
        for task in (
            ChunkChild(salt=salt, index=index),
            ChunkLeaf(salt=salt, index=leaf_indices(index)[0]),
        ):
            assert registry.task_get(str(task.id)).status == "completed", task

    events = task_events(deployment, root.id)
    yields = [
        e
        for e in events
        if e.get("event_type") == "task_yielded"
        and e.get("report_applied")
        and str(e.get("build_id")) == str(build_id)
    ]
    assert len(yields) >= 3, (
        "The root's yield should have arrived as several applied /yield "
        "requests.\n" + describe_events(events)
    )
    # The registry under test accepts a closure-only request: the first one
    # yielded nothing, rather than a stand-in.
    first_yielded = (yields[0].get("event_metadata") or {}).get("yielded")
    assert first_yielded == [], describe_events(events)
