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


@sd.task(name="Cooperative")
def cooperative(values: sd.Depends[list[int]], seconds: int) -> list[int]:
    """Sleeps in slices, asking between them whether it is still wanted.

    The cooperative-cancellation scenario's worker. ``Slow`` cannot serve
    it: one long ``time.sleep`` has no point at which the task's author
    could stop, which is exactly the case the design says it cannot
    protect. This one has such a point, and that is the whole difference
    the scenario measures.

    ``seconds`` is deliberately far longer than the scenario waits. The
    assertion is that the container is *gone* well before it would have
    finished on its own, so a worker that ignored the cancel keeps running
    and the poll times out rather than passing slowly.

    The slice is short relative to the check interval (30s by default), so
    the loop is never what delays the answer -- the throttle is, which is
    the thing under test.
    """
    import time

    import stardag as sd_

    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if sd_.cancellation_requested():
            # No output written and no completion reported: the runner
            # recognises this and records nothing for it.
            raise sd_.ExecutionCancelled("the build no longer wants this execution")
        time.sleep(2)
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

    def children_tasks(self) -> list:
        """The children ``run`` will yield, computable without running it.

        The indices are ``range(children)`` by construction of the leaf, so
        a scenario can name the children before the parent has yielded them
        -- to ask the event log which build touched them.
        """
        return [
            slow(values=self.requires(), seconds=self.child_seconds + index)
            for index in range(self.children)
        ]

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
    changes the structure, not the output (see ``run``). Two builds with different widths
    have different scopes, so an abandoned wide generation from one build
    is never inherited by a narrower build of the same task id — and two
    builds with the *same* width share a scope, so the second trusts the
    first's edges and never re-runs the pre-yield section.

    Child ids overlap between widths on purpose (index 0 and 1 exist for
    both), which is what lets a scenario tell "shared and re-run because it
    is in my plan too" from "inherited from an abandoned generation". That
    is why every child reads the same one-element ``Range`` rather than the
    width-sized one this task itself requires: a child's id must not carry
    the width, only its index.
    """

    salt: str
    children: Annotated[int, sd.StardagField(significance="dependencies_only")] = 4
    child_seconds: int = 30
    pre_yield_seconds: int = 20

    def requires(self):
        return get_range(limit=self.children, salt=self.salt)

    def child_tasks(self) -> list:
        """The children this width yields, computable without running."""
        return [
            slow(
                values=get_range(limit=1, salt=self.salt),
                seconds=self.child_seconds + index,
            )
            for index in range(self.children)
        ]

    def run(self):
        import time

        indices = self.requires().load()
        time.sleep(self.pre_yield_seconds)
        kids = self.child_tasks()
        assert len(kids) == len(indices)
        yield kids
        # Width-invariant, as a dependencies_only field demands: every
        # child summarises the same one-element range, so the set of their
        # lengths is {1} at any width. The output must not encode the width,
        # or two builds with different configs would disagree on the output
        # behind one task id.
        self._save(sorted({len(kid.load()) for kid in kids}))


@sd.task(name="SlowOnWorker")
def slow_on_worker(
    values: sd.Depends[list[int]], seconds: int, worker: str = "default"
) -> list[int]:
    """``Slow``, but routed to a named worker by the app's selector.

    A separate task rather than a ``worker`` field on ``Slow``, and that is
    the point of it: a task id is derived from its parameters, so adding a
    field to ``Slow`` would move the ids of every task in every other
    scenario. This one is new, so it moves nothing.

    Read by ``selectors.registry_live_worker``, which routes it to that
    Modal function. What the stop scenario needs from that is the
    *execution metadata*: the function name is what the registry records
    alongside the call ref, and therefore what ``stardag builds stop
    --worker`` selects on.

    ``seconds`` has to outlive the stop -- the tasks that are not stopped
    must still be running when the command releases the claims, so that
    "they finish afterwards" is a thing that can be observed rather than a
    race the scenario happened to win.
    """
    import time

    del worker  # read off the task by the app's worker selector
    time.sleep(seconds)
    return values


class WorkerFanIn(sd.Task[int]):
    """A root over several ``SlowOnWorker`` upstreams, split across workers.

    The shape ``stardag builds stop --worker`` needs and nothing else here
    produces: one build holding several live executions at once, on more
    than one Modal function, all started within a few seconds of each
    other.

    **The sleep has to outlive the whole scenario**, and that is what the
    duration is for rather than pacing. The evidence the scenario rests on
    is a probe of Modal itself once the command has returned: the calls it
    selected are no longer running and the ones it excluded still are. An
    upstream that reached the end of its own sleep in the meantime would
    answer "not running" for a reason that has nothing to do with the
    cancel, and the assertion would pass having tested nothing.

    It is sized for the container-start skew between the first upstream and
    the last, plus the command's own run -- generously, but not unboundedly,
    because every second past the scenario is a container still billing.
    Getting it wrong is safe in the one direction that matters: an upstream
    that outran its sleep is COMPLETED, not RUNNING, so the scenario's
    "all four running" wait never comes true and it fails on that timeout
    rather than passing vacuously.

    ``salt`` reaches the leaf and through it every task id here -- see the
    note on ``get_range``.
    """

    salt: str
    stopped_worker: str = "alt"
    seconds: int = 180
    per_worker: int = 2

    def requires(self):
        return self.stopped_tasks() + self.kept_tasks()

    def stopped_tasks(self) -> list:
        """The upstreams the scenario will stop. Nameable before they run."""
        return self._upstreams(self.stopped_worker)

    def kept_tasks(self) -> list:
        """The upstreams that must be left running by the same command."""
        return self._upstreams("default")

    def _upstreams(self, worker: str) -> list:
        # The index gives ``per_worker`` distinct ids per worker; the worker
        # name keeps the two groups apart, so all of them are separate
        # tasks. The leaf's own ``limit`` is unrelated to that count -- it
        # sizes the list every upstream reads, and one shared leaf feeds
        # them all. Two is simply a small number of integers.
        leaf = get_range(limit=2, salt=self.salt)
        return [
            slow_on_worker(values=leaf, seconds=self.seconds + index, worker=worker)
            for index in range(self.per_worker)
        ]

    def run(self):
        self._save(sum(len(upstream.load()) for upstream in self.requires()))
