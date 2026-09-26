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

Where a window exists only to hold a state open *for the scenario* -- a task
still running while the app is redeployed, or while a second build yields
-- the task takes a ``gate`` as well (see ``_gates``). A gated body holds
until the scenario releases it, and its duration becomes the upper bound: the
old sleep, reached only if the release is lost. An empty gate is the old
behaviour exactly.
"""

from __future__ import annotations

import os
from typing import Annotated

import stardag as sd

from ._gates import hold

# The setting ``ScopedUpstreams`` reads its structure from. Named here so the
# scenario that sets it and the task that reads it cannot drift apart.
SCOPED_UPSTREAMS_SETTING = "REGISTRY_LIVE_UPSTREAMS"

# Baked into a rollover app's image at deploy time (see ``rollover_app``) and
# read once, at import, by ``RolloverRoot``: the harness's stand-in for "the
# new deployment's code changed the root's identity". Unset everywhere else,
# the test process included, so every other deploy and every trigger sees
# the original class.
ROOT_VARIANT_ENV = "REGISTRY_LIVE_ROOT_VARIANT"
_ROOT_VARIANT = os.environ.get(ROOT_VARIANT_ENV, "")


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
def slow(
    values: sd.Depends[list[int]], seconds: int, limit_key: str = "", gate: str = ""
) -> list[int]:
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

    ``gate``, when set, ends the run as soon as the scenario releases it, with
    ``seconds`` as the upper bound (see ``_gates``). For the scenarios that
    need it RUNNING only until they have done something -- a redeploy, a
    second build's yield -- rather than for a fixed time.
    """
    del limit_key  # read off the task by the app's limit-key selector
    hold(gate, seconds, what="Slow")
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


class ConfiguredFanOut(sd.Task[list[int]]):
    """``SuspendingParent`` with a non-significant width.

    ``children`` is ``significant=False``: the number of dynamic children
    changes the structure, not the output (see ``run``), so it is not part of
    the task id. Two builds of one task object in one scope (deployment +
    settings) share its instance and its dynamic edges, so the second trusts
    the first's yield and never re-runs the pre-yield section -- which is what
    ``test_shared_structure_scope`` asserts, and the fan-out ``test_rollover``
    re-plans under a new deployment.

    Every child reads the same one-element ``Range`` rather than the
    width-sized one this task itself requires: a child's id must not carry the
    width, only its index.

    Two optional gates (see ``_gates``). ``pre_yield_gate`` ends the pre-yield
    section when the scenario releases it, bounded by ``pre_yield_seconds``;
    a run of the body after the release -- a restart under new code, a
    re-plan into a new scope -- finds it open and yields at once, where the
    ungated body sleeps the whole window again. ``child_gate`` does the same
    for every child, bounded by its ``child_seconds``.
    """

    salt: str
    children: Annotated[int, sd.StardagField(significant=False)] = 4
    child_seconds: int = 30
    pre_yield_seconds: int = 20
    pre_yield_gate: str = ""
    child_gate: str = ""

    def requires(self):
        return get_range(limit=self.children, salt=self.salt)

    def child_tasks(self) -> list:
        """The children this width yields, computable without running."""
        return [
            slow(
                values=get_range(limit=1, salt=self.salt),
                seconds=self.child_seconds + index,
                gate=self.child_gate,
            )
            for index in range(self.children)
        ]

    def run(self):
        indices = self.requires().load()
        hold(self.pre_yield_gate, self.pre_yield_seconds, what="pre-yield")
        kids = self.child_tasks()
        assert len(kids) == len(indices)
        yield kids
        # Width-invariant, as a non-significant field demands: every child
        # summarises the same one-element range, so the set of their lengths
        # is {1} at any width. The output must not encode the width, or two
        # constructions would disagree on the output behind one task id.
        self._save(sorted({len(kid.load()) for kid in kids}))


@sd.task(name="SlowOnWorker")
def slow_on_worker(
    values: sd.Depends[list[int]], seconds: int, worker: str = "default", gate: str = ""
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
    race the scenario happened to win. With a ``gate`` it is an upper bound:
    the scenario releases the gate once it has observed that, and the ones
    still running finish then (see ``_gates``).
    """
    del worker  # read off the task by the app's worker selector
    hold(gate, seconds, what="SlowOnWorker")
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

    ``gate`` makes ``seconds`` an upper bound (see ``_gates``): every upstream
    holds until the scenario releases it, so "outlive the whole scenario" is
    a state the scenario ends rather than a duration it has to fit inside.
    """

    salt: str
    stopped_worker: str = "alt"
    seconds: int = 180
    per_worker: int = 2
    gate: str = ""

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
            slow_on_worker(
                values=leaf,
                seconds=self.seconds + index,
                worker=worker,
                gate=self.gate,
            )
            for index in range(self.per_worker)
        ]

    def run(self):
        self._save(sum(len(upstream.load()) for upstream in self.requires()))


class ScopedUpstreams(sd.Task[list[int]]):
    """A task whose *structure* comes from the build's settings.

    ``requires()`` reads ``SCOPED_UPSTREAMS_SETTING`` -- the one route by
    which an environment variable may reach structure (design.md, "What
    carries over": structure may depend on the environment only through the
    deployment and ``settings``, both in the scope). So two builds that set
    it differently plan this one completion under two scopes with two
    upstream sets, which is S1.

    The output must not depend on it, and does not: the task-id promise is
    that output is a function of the significant parameters alone, and
    completion is global, so a result that varied with the setting would let
    one build reuse another's different answer.

    ``label`` is non-significant, so two builds may construct the same
    completion with different bodies: it is what makes "whichever claims it
    first runs *its* instance body" observable on the ledger.

    ``seconds`` keeps it RUNNING long enough for the second build to
    register against it mid-flight.
    """

    salt: str
    seconds: int = 60
    label: Annotated[str, sd.StardagField(significant=False)] = ""

    def requires(self):
        width = int(os.environ.get(SCOPED_UPSTREAMS_SETTING, "1"))
        return [get_range(limit=index, salt=self.salt) for index in range(width)]

    def run(self):
        import time

        time.sleep(self.seconds)
        self._save([0])


class DiesOnce(sd.Task[list[int]]):
    """A worker that dies without reporting, once (S21).

    The first execution of this task shortens its own claim and then exits
    the container hard -- ``os._exit``, so no ``finally``, no reporter, no
    failure report: the shape of an OOM kill. Every later execution runs
    normally.

    **Shortening the claim is the only synthesised part**, and it goes
    through the real route. A detached claim's TTL is the worker's timeout
    plus a 15-minute grace (``stardag.build._claims``), because the backend
    kills an execution before its claim lapses -- correct, and far longer
    than a scenario can wait. ``claim_renew`` sets the expiry to now plus
    the requested TTL for the holder of the live claim, so the dying worker
    renews itself down to ``lapse_seconds`` and the lapse the design is
    about arrives in seconds rather than in half an hour. Nothing after
    that is arranged: the lapse, the takeover and the second execution are
    the registry's and the tick's own.

    "First" is read off the registry, not off local state, because a
    container has none that survives it: an execution whose build ledger
    holds no other execution of this task is the first.
    """

    salt: str
    lapse_seconds: int = 20

    def requires(self):
        return get_range(limit=2, salt=self.salt)

    def run(self):
        import sys
        from uuid import UUID

        from stardag.registry import registry_provider

        registry = registry_provider.get()
        execution_id = UUID(os.environ["STARDAG_EXECUTION_ID"])
        build_id = UUID(os.environ["STARDAG_BUILD_ID"])
        mine = [
            e
            for e in registry.build_list_executions(build_id, include_ended=True)
            if e.task_id == str(self.id)
        ]
        if len(mine) <= 1:
            try:
                registry.claim_renew(
                    str(self.id),
                    execution_id=execution_id,
                    claim_ttl_seconds=self.lapse_seconds,
                )
            finally:
                print(
                    f"[registry-live] DiesOnce {self.id}: exiting hard, unreported",
                    file=sys.stderr,
                    flush=True,
                )
                os._exit(137)
        self._save(self.requires().load())


class RolloverRoot(sd.Task[list[int]]):
    """A root whose identity a new deployment can change (S20).

    Under ``REGISTRY_LIVE_ROOT_VARIANT=renamed`` -- set only in the image of
    the rollover app's *second* deploy -- the class gains a significant field
    with a default. A body registered under the first deploy lacks it, so it
    rehydrates with the default and the recomputed task id differs from the
    build's ``root_task_ids``: "new code changed what the build asked for",
    which a rollover must refuse rather than silently re-plan.

    ``gate`` is handed to the upstream ``Slow`` (see ``_gates``); ``seconds``
    is then its upper bound.
    """

    salt: str
    seconds: int = 60
    gate: str = ""
    if _ROOT_VARIANT == "renamed":
        revision: int = 2

    def requires(self):
        return slow(
            values=get_range(limit=2, salt=self.salt),
            seconds=self.seconds,
            gate=self.gate,
        )

    def run(self):
        self._save(self.requires().load())
