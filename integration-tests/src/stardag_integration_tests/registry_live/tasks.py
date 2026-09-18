"""Tasks for the registry-live scenarios.

In their own module, and that is load-bearing rather than tidy: the app
declares ``task_modules=[...]`` covering exactly this module, so a scheduler
tick running in a container that has imported nothing else can still rebuild
these tasks from what the registry knows about them. If that stopped
working, a tick would have no way to reconstruct the plan and the scenarios
would fail at the first hand-off -- which is the point of exercising it.

The durations are not padding. Each one sizes a *window* some scenario needs
to exist, and the comments say which; shortening one to make a run faster is
how a scenario silently stops testing anything.
"""

from __future__ import annotations

from typing import Annotated

import stardag as sd


@sd.task(name="Range")
def get_range(limit: int, salt: str) -> list[int]:
    """The chain's leaf, and the only place a run's identity enters it.

    ``salt`` does nothing to the result and everything to the *task id*.
    Task ids are derived from parameters, so a fresh salt makes this task
    and every task downstream of it new -- which is what stops a rerun
    finding the previous run's outputs already on the target root, calling
    every task complete, and passing having scheduled nothing.

    It is a parameter of the leaf rather than of each task so that the
    number of tasks in the plan is the same on every run, in a fresh
    environment or a reused one. Scenarios assert on how many tasks were
    spawned, and an assertion that depends on what a previous run happened
    to leave behind is not an assertion.
    """
    del salt
    return list(range(limit))


@sd.task(name="Square")
def square(values: sd.Depends[list[int]], offset: int) -> list[int]:
    return [(value + offset) ** 2 for value in values]


@sd.task(name="Sum")
def get_sum(integers: sd.Depends[list[int]]) -> int:
    return sum(integers)


@sd.task(name="Slow")
def slow(values: sd.Depends[list[int]], seconds: int, limit_key: str = "") -> list[int]:
    """Runs long enough to still be RUNNING when someone else asks for it.

    Two scenarios need that. The cross-build ones need a task genuinely
    in-flight rather than one that races to completion before the second
    build's first tick even fetches a frontier; and the wake-up scenario
    needs it to outlive the waiting build's tick, so that when it finishes
    there is provably no scheduler left anywhere to notice on its own.

    ``limit_key`` is **opt-in and empty by default**, and that default is
    load-bearing rather than tidy. A task carrying a concurrency-limit key
    is not merely accounted against a limiter: on every transition out of
    RUNNING the registry flags *every build in the environment* holding a
    PENDING task under the same key, whether or not a limit is configured
    for it. With a key applied unconditionally and the scenarios running
    concurrently in one environment, one scenario's ``Slow`` finishing
    would wake another scenario's build -- and a build that gets woken by
    a neighbour is a build whose own wake-up path was never tested. So a
    scenario asks for a key only when the key is what it is testing.
    """
    import time

    del limit_key  # read off the task by the app's limit-key selector
    time.sleep(seconds)
    return values


@sd.task(name="Fails")
def fails(values: sd.Depends[list[int]], seconds: int) -> list[int]:
    """Runs for ``seconds``, then fails deterministically.

    For the scenario where a shared task's status is a *result* rather than
    a revocation. The sleep is what makes the decision observable: the
    second build has to register while this task is still RUNNING, because
    RUNNING is the one status a trigger will not reset -- and a trigger
    resetting it is the (correct) behaviour that would hide the mid-flight
    decision under test.
    """
    import time

    time.sleep(seconds)
    del values
    raise RuntimeError("deliberate failure: this task exists to fail")


class SuspendingParent(sd.Task[list[int]]):
    """Registers dynamic children, yields, and so sits SUSPENDED while they run.

    The shape one scenario needs and nothing else here produces: a shared
    task whose worker registered its children, yielded and returned. It
    holds **no claim** -- so nothing but its owning build's liveness says
    whether anyone is still progressing it, and a second build that reset
    it would redo every bit of the pre-yield work for nothing while the
    first build is legitimately mid-flight.

    Note which duration bounds that window. It is the *worker* timeout, not
    the claim TTL: the TTL is derived from the timeout plus a grace, so it
    is strictly the looser of the two, and a fixture sized against it would
    be guaranteed to outlive its own execution budget.

    ``salt`` reaches the leaf, and through it this task's own id and its
    children's -- see the note on ``get_range``.
    """

    salt: str
    children: int = 2
    child_seconds: int = 60
    pre_yield_seconds: int = 20

    def requires(self):
        return get_range(limit=self.children, salt=self.salt)

    def run(self):
        import time

        indices = self.requires().load()
        # A RUNNING window before the yield, and it is load-bearing rather
        # than padding: the second build has to *register* while this task
        # is still RUNNING. Register any later and plan closure follows the
        # freshly-written dynamic edges, pulls the children into that
        # build's plan too, and it is then simply a build with running
        # tasks of its own -- never stalled, so never classifying a blocker
        # at all. Correct behaviour; just not the state under test.
        time.sleep(self.pre_yield_seconds)
        # The children hang off the leaf this task already required, which
        # is complete by the time they are registered -- so the suspended
        # window costs one level of container start rather than two.
        #
        # Their ids are made distinct by the one parameter that was going
        # to differ anyway. A task id is derived from its parameters, so
        # two children given identical ones would be a single task and the
        # fan-out would be imaginary.
        kids = [
            slow(values=self.requires(), seconds=self.child_seconds + index)
            for index in indices
        ]
        yield kids
        self._save([len(kid.load()) for kid in kids])


class FanIn(sd.Task[int]):
    """A single root over ``width`` independent leaves: one wide layer.

    Flat rather than a binary tree, and that is the whole design. What the
    fan-out scenario exercises is how many actionable tasks one tick pass
    can put on workers, so the quantity that matters is the width of a
    *single* layer. A tree over the same leaves would nearly double the
    task count -- and every extra task is another real Modal container --
    while making no layer any wider than this one.

    Distinct ``limit`` values give the leaves distinct ids, so these really
    are N independent tasks rather than one task referenced N times, which
    is what makes it a fan-out rather than a deduplication test.
    """

    salt: str
    width: int = 24

    def requires(self):
        return [get_range(limit=index, salt=self.salt) for index in range(self.width)]

    def run(self):
        self._save(sum(len(leaf.load()) for leaf in self.requires()))


class Resumable(sd.Task[list[int]]):
    """Catches a platform interruption and asks to be resumed.

    The documented checkpoint recipe, minus the checkpoint: this task
    restarts its sleep from zero when it is resumed, because what the
    scenario is about is *classification and recovery*, not checkpoint
    storage. Keeping real state here would add a target root write to the
    part of the run that has to be fast and deterministic, and would prove
    nothing the unit tests do not already pin.

    What matters is the shape of the raise. ``except MODAL_INTERRUPTIONS``
    then ``raise ... from None`` is what the docs tell people to write, and
    it is the form whose ``__context__`` the runner reads to decide whether
    the backend is going to restart this input. A scenario that raised
    ``ResumableInterruption`` bare would exercise the fallback instead --
    the opposite of the path under test.

    ``seconds`` has to outlast a cold container start plus the harness
    noticing the task is RUNNING, since the interruption is delivered from
    outside while it sleeps.
    """

    salt: str
    seconds: int = 120

    def requires(self):
        return get_range(limit=2, salt=self.salt)

    def run(self):
        import time

        from stardag.integration.modal import MODAL_INTERRUPTIONS

        try:
            time.sleep(self.seconds)
        except MODAL_INTERRUPTIONS:
            raise sd.ResumableInterruption(
                "interrupted mid-sleep; no checkpoint kept"
            ) from None
        self._save(self.requires().load())


class ConfiguredChain(sd.Task[int]):
    """A root whose *upstream* is chosen by a ``dependencies_only`` field.

    The shape behind the "changed ``requires()``" incident, reproducible
    from one deployment: two builds with different ``build_config`` give
    this task the same id — ``upstream_seconds`` is not part of it — and a
    different upstream, since ``seconds`` *is* part of ``Slow``'s id. The
    second build's structure scope differs from the first's, so its edges
    live apart and the first build's abandoned upstream never gates it.
    """

    salt: str
    upstream_seconds: Annotated[
        int, sd.StardagField(significance="dependencies_only")
    ] = 90

    def requires(self):
        return slow(
            values=get_range(limit=3, salt=self.salt), seconds=self.upstream_seconds
        )

    def run(self):
        self._save(sum(self.requires().load()))


class ConfiguredFanOut(sd.Task[list[int]]):
    """``SuspendingParent`` with its width read from the build config.

    ``children`` is ``dependencies_only``: the number of dynamic children
    changes the structure, not the output. Two builds with different widths
    have different scopes, so an abandoned wide generation from one build
    is never inherited by a narrower build of the same task id — and two
    builds with the *same* width share a scope, so the second trusts the
    first's edges and never re-runs the pre-yield section.

    Child ids overlap between widths on purpose (index 0 and 1 exist for
    both), which is what lets a scenario tell "shared and re-run because it
    is in my plan too" from "inherited from an abandoned generation".
    """

    salt: str
    children: Annotated[int, sd.StardagField(significance="dependencies_only")] = 4
    child_seconds: int = 30
    pre_yield_seconds: int = 20

    def requires(self):
        return get_range(limit=self.children, salt=self.salt)

    def run(self):
        import time

        indices = self.requires().load()
        time.sleep(self.pre_yield_seconds)
        kids = [
            slow(values=self.requires(), seconds=self.child_seconds + index)
            for index in indices
        ]
        yield kids
        self._save([len(kid.load()) for kid in kids])
