"""Tell a transport timeout apart from a real failure, and probe *why*.

One failure class has outlived every fix aimed at this tier: a request to
the registry that never gets an answer at all. Five occurrences in five
scenarios against five different endpoints, none of them a container
recycle -- so no scenario's logic is implicated and no endpoint is. The
registry is intermittently taking longer than the client's timeout to
answer anything.

Two hypotheses survive, and they want opposite fixes:

**A. The container is starved or throttled.** A Postgres *and* a FastAPI
app serving twelve concurrent pytest-xdist workers, on one container. The
answer would be more resources, or fewer workers.

**B. The database path is blocked** -- pool exhaustion, or lock waits
under twelve concurrent clients. That would make this a *product* signal
rather than an infrastructure one, and the answer would be upstream in
the registry's locking.

**C. The server answers and the answer does not arrive.** Nothing on the
registry is slow at all; the response is produced and lost between the
container and the runner.

``/_harness/boot`` settles exactly one of these, and the discipline of
this module is to claim no more than that. It is the one endpoint that
returns a closure variable and touches no database, so a *slow or absent*
answer while a real endpoint is timing out means the container is not
serving -- hypothesis A. A **prompt** answer refutes A and nothing else:
it establishes that the process is alive and handing back twenty bytes,
which is equally true under B and under C. An earlier version of this
module read a prompt probe as proof of B and named the registry's locking
as the lever; the run that first exercised it answered 6257 requests with
a maximum handler time of 460 ms, so the verdict was contradicted by
evidence in its own artifact (STA-92).

**What separates B from C is the registry's access log**, which prints
``duration`` and ``execution`` per request -- queueing against handler
time -- and is dumped into the same artifact. That join happens after the
run, in ``diagnose.py``, because the log is not readable from inside it.
This module therefore writes *facts* -- when, which request, what the
probe said -- and a label covering only what it can support.

Nothing here retries or suppresses anything by itself. It classifies, it
probes, and it writes down what it found; CI reads the marker and decides.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

import httpx

from ._harness import (
    CLASSIFICATION_FAILED,
    Deployment,
    read_boot_id,
    record_recycle,
)

# Where the harness leaves everything a failed run should be diagnosed
# from. CI sets it to a directory it uploads as a workflow artifact, and
# points the recycle marker at the same place, so one artifact carries the
# marker files, the per-occurrence records and the Modal logs together.
# Unset locally, where the printed record is the whole story.
DIAGNOSTICS_DIR_ENV = "STARDAG_REGISTRY_LIVE_DIAGNOSTICS_DIR"

# The marker CI keys its one retry on, inside that directory. Deliberately
# a *second* marker rather than a flag on the recycle one: the two failures
# want different recoveries. A recycle lost the database, so its retry has
# to re-provision first; a transport timeout leaves the stack intact, so
# its retry is just the tier again.
TIMEOUT_MARKER_NAME = "transport-timeouts"

# Its counterweight, and the reason the retry cannot launder a product
# failure. The tier runs twelve scenarios at once, so one worker meeting a
# transport timeout says nothing about what the other eleven met: without
# this, a run where one scenario timed out and another failed an assertion
# would be retried whole, and a flaky assertion passing the second time
# would turn the check green over a real failure. Every failure that is not
# a transport timeout is named here -- setup, call and teardown alike,
# since fixtures do registry I/O at both ends of a scenario -- and CI
# refuses to retry a run that has any. One exemption, by type rather than
# by phase: ``RegistryContainerRecycled``, which has its own marker and a
# recovery of its own.
NON_TIMEOUT_MARKER_NAME = "non-timeout-failures"

# ``CLASSIFICATION_FAILED`` is imported from ``_harness`` above rather
# than defined here: ``record_recycle`` lives there and has to fail
# closed too, and this module already depends on that one.

# Short on purpose. The question the probe asks is not "does the registry
# work" but "did it answer *promptly* while a real call was timing out",
# and a generous timeout blurs exactly that distinction. Fifteen seconds
# is far longer than the endpoint's own work (it returns a string held in
# a closure) and far shorter than the 30s the SDK client had already spent
# per attempt before giving up.
BOOT_PROBE_TIMEOUT_SECONDS = 15.0

# What counts as answering *promptly*, which is the word the whole
# discrimination rests on. "It answered at all" is not the question: a
# probe that comes back after fourteen of its fifteen seconds is itself
# evidence of a starved container, not of a healthy one behind a blocked
# database. A healthy probe against a live stack measured about 0.5s
# including TLS and the Modal edge; three seconds is six times that and
# still a twentieth of the client timeout the real call had already blown
# through, so the band in between is wide enough that neither verdict is
# reached by a near miss.
BOOT_PROBE_PROMPT_SECONDS = 3.0

# The label a prompt probe earns. Deliberately not "HYPOTHESIS B": it
# states what was observed -- the container is alive and answering -- and
# leaves the choice between B and C to the pass that can actually make it.
# A label that names a hypothesis is read as a diagnosis, and this one
# would be a diagnosis of whichever hypothesis happened to be written down
# first.
CONTAINER_SERVING = "CONTAINER SERVING"

# One machine-readable sidecar per occurrence, next to the human record.
# The prose is for whoever opens the artifact; this is what the
# access-log join reads, because parsing back a paragraph that exists to
# be rewritten is how the two drift apart.
SIDECAR_SUFFIX = ".json"


@dataclass(frozen=True)
class BootProbe:
    """What ``/_harness/boot`` said, and how fast it said it."""

    answered: bool
    elapsed: float
    boot_id: str | None
    error: str | None
    # False for the one caller that had its answer already: a timeout out
    # of ``assert_same_container``, which read this very endpoint six
    # times over a hundred seconds and got nothing. The conclusion is the
    # same as a failed probe's, but the artifact must not claim a probe
    # ran, and an elapsed time of 0.0s would say exactly that.
    probed: bool = True

    def label(self, expected_boot_id: str) -> str:
        """What this probe establishes, as a word CI can count.

        Three outcomes, not four, and the asymmetry is deliberate. A
        failed or slow probe *identifies* hypothesis A: nothing was
        serving, and that is a complete answer. A prompt probe only
        *refutes* A -- it says the container is alive and serving, which
        both of the remaining hypotheses predict. So the third label
        names what was observed rather than a hypothesis, and the
        access-log pass in ``diagnose.py`` is what turns it into one.
        """
        if not self.answered:
            return "HYPOTHESIS A"
        if self.boot_id != expected_boot_id:
            return "RECYCLE"
        if self.elapsed > BOOT_PROBE_PROMPT_SECONDS:
            return "HYPOTHESIS A"
        return CONTAINER_SERVING

    def verdict(self, expected_boot_id: str) -> str:
        """The same judgement in a sentence, derived from ``label``.

        Derived rather than decided again, so the artifact's prose and the
        word CI counts cannot come apart.
        """
        label = self.label(expected_boot_id)
        if label == "RECYCLE":
            detail = (
                f"the boot probe answered in {self.elapsed:.1f}s with a "
                f"*different* boot id ({expected_boot_id} -> {self.boot_id}), "
                f"so the container was replaced. This is the recycle case; "
                f"the post-scenario check records it separately and CI "
                f"re-provisions before retrying."
            )
        elif label == "HYPOTHESIS A" and self.answered:
            detail = (
                f"the boot probe answered, but took {self.elapsed:.1f}s to "
                f"return a string held in a closure -- over the "
                f"{BOOT_PROBE_PROMPT_SECONDS:.0f}s this treats as prompt. A "
                f"slow answer is not evidence of a blocked database path; it "
                f"is evidence that the container, or the runner the probe "
                f"ran on, is starved or throttled. The levers are the "
                f"container's resources and the tier's worker count."
            )
        elif label == "HYPOTHESIS A" and not self.probed:
            detail = (
                "no probe was run, and none was needed: this timeout came "
                "out of the post-scenario boot check, which had already "
                "read /_harness/boot six times over a hundred seconds "
                "without an answer. Nothing is serving, so the block is "
                "not in the database path: the container is starved, "
                "throttled or wedged -- or the runner is. The levers are "
                "the container's resources and the tier's worker count."
            )
        elif label == "HYPOTHESIS A":
            detail = (
                f"the boot probe also failed, after {self.elapsed:.1f}s "
                f"({self.error}). Nothing answered at all, so the block is "
                f"not in the database path: the container is starved, "
                f"throttled or wedged -- or, since the probe runs in the "
                f"stalled process, the runner itself is. The levers are the "
                f"container's resources and the tier's worker count."
            )
        else:
            detail = (
                f"the boot probe answered in {self.elapsed:.1f}s, from the "
                f"same container ({self.boot_id}). The process is alive and "
                f"serving HTTP promptly, which rules out hypothesis A and "
                f"nothing else -- a container that is starved would have "
                f"failed this. It does *not* show that the database path "
                f"was blocked: this endpoint returns a closure variable and "
                f"touches no database, so it answers exactly as fast whether "
                f"the handler behind the timed-out call was slow "
                f"(hypothesis B) or the response was produced and never "
                f"arrived (hypothesis C). The registry's access log "
                f"separates those, and diagnose.py reads it once the logs "
                f"are dumped. No lever is named here, because none is "
                f"identified yet."
            )
        return f"{label} -- {detail}"


def probe_boot(
    api_url: str, *, timeout: float = BOOT_PROBE_TIMEOUT_SECONDS
) -> BootProbe:
    """Ask ``/_harness/boot`` how it is, and time the answer.

    ``read_boot_id`` opens its own client, which is the property that
    makes this probe mean anything: a connection pool saturated by the
    scenario's own in-flight requests would otherwise make every probe
    time out in the pool rather than on the wire, and every occurrence
    would read as hypothesis A whatever was actually true.
    """
    started = time.monotonic()
    try:
        boot_id = read_boot_id(api_url, timeout=timeout)
    except Exception as error:
        return BootProbe(
            answered=False,
            elapsed=time.monotonic() - started,
            boot_id=None,
            error=repr(error),
        )
    return BootProbe(
        answered=True,
        elapsed=time.monotonic() - started,
        boot_id=boot_id,
        error=None,
    )


def transport_timeout(error: BaseException) -> BaseException | None:
    """The timeout that failed this test -- but only if that is *all* it is.

    This is the discriminator the retry rests on, and the reason the retry
    is not a way of hiding product bugs. Two exclusions do that work, and
    both are deliberately stated rather than implied:

    - **An HTTP error status is never a timeout.** A 4xx or 5xx means the
      registry answered; whatever it said is a real result and a second
      ask is not owed. Every one of this tier's real finds arrived that
      way -- a 500 from ``tasks/bulk``, a 401 from a replaced container.
    - **An assertion is never a timeout**, even with one in its context.
      An ``AssertionError`` means a scenario reached a judgement and the
      judgement went against the code. Retrying that is precisely the
      "run it again and hope" this tier exists to avoid.

    What is left is the case where no complete response exists to reason
    about: a timeout at any phase (``httpx.TimeoutException`` and its
    four subclasses), a network error (the connection refused, reset or
    dropped), or a body cut short (``RemoteProtocolError``: "peer closed
    connection without sending complete message body"). The last was
    once excluded because only timeouts had been seen; then the tier met
    one (STA-102), and it is the most direct form of the failure this
    tier keeps meeting -- the server sent a ``Content-Length`` and the
    body never arrived. What unites them is not "timeout" but "no
    complete answer", so that is the rule.

    **A cause chain and an exception group are read differently, and the
    difference is load-bearing.** A chain is one failure described at
    several levels, so a ``RuntimeError`` wrapping a ``ReadTimeout`` is
    still a timeout. A group is several *independent* failures that
    happened to arrive together -- pytest builds one from multiple
    failing fixture finalizers -- so a non-timeout sibling is a real
    failure of its own, and the whole group is a timeout only if every
    member is. Treating a group like a chain is how
    ``[ReadTimeout, RuntimeError]`` would buy a retry over the
    ``RuntimeError``.
    """
    return _timeout_in(error, set())


def _timeout_in(error: BaseException, groups_seen: set[int]) -> BaseException | None:
    found: BaseException | None = None
    for current in _chain(error):
        members = _group_members(current)
        if members is not None:
            # Only *groups* are guarded against revisiting, and only to
            # stop a self-referential one recursing forever. Guarding
            # every exception instead conflates "seen this already" with
            # "not a timeout", and a group holding the same timeout
            # object twice -- which is what a pair of identical fixture
            # failures looks like -- then vetoes itself.
            if id(current) in groups_seen:
                continue
            groups_seen.add(id(current))
            for member in members:
                nested = _timeout_in(member, groups_seen)
                if nested is None:
                    return None
                found = found or nested
            continue

        if _carries_http_status(current) or isinstance(current, AssertionError):
            return None
        if found is None and _is_transport_fault(current):
            found = current
    return found


def _group_members(error: BaseException) -> tuple[BaseException, ...] | None:
    """A group's members, or ``None`` for anything that is not a group.

    Matched structurally rather than by naming ``BaseExceptionGroup``:
    this repo pins no ruff ``target-version`` anywhere, so the builtin
    reads as undefined and fails the lint. The membership test keeps it
    honest whatever carries the attribute.
    """
    members = getattr(error, "exceptions", None)
    if not isinstance(members, tuple) or not members:
        return None
    if not all(isinstance(member, BaseException) for member in members):
        return None
    return members


def _chain(error: BaseException) -> Iterator[BaseException]:
    """Every exception reachable from ``error`` through its own links.

    Two links, and only two: ``__cause__`` for anything re-raised
    explicitly, and ``__context__`` for a timeout surfacing from inside an
    ``except`` block. Both describe *one* failure at several levels, which
    is why a non-timeout wrapper does not veto.

    Groups are deliberately not expanded here. They are several
    independent failures rather than one described twice, so they need the
    opposite rule, and the caller applies it -- see ``transport_timeout``.

    Cycle-guarded by identity: an exception raised inside its own handler
    can make ``__context__`` circular, and this runs on the failure path
    where an infinite loop would be especially unhelpful.
    """
    seen: set[int] = set()
    queue: list[BaseException] = [error]
    while queue:
        current = queue.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        yield current
        for linked in (current.__cause__, current.__context__):
            if linked is not None:
                queue.append(linked)


# httpcore's names for the same faults, for a transport that re-raised
# rather than mapped (see ``_is_transport_fault``).
_HTTPCORE_FAULTS = frozenset(
    {"RemoteProtocolError", "ConnectError", "ReadError", "WriteError"}
)


def _is_transport_fault(error: BaseException) -> bool:
    """Whether this exception means "no complete response arrived"."""
    if isinstance(
        error, (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError)
    ):
        return True
    # httpx maps httpcore's transport exceptions to its own at the
    # transport boundary, so the httpx type is normally the one raised --
    # but the httpcore one is what a traceback names, and a transport that
    # re-raised rather than mapped would otherwise read as a real failure.
    # Matched structurally rather than by importing httpcore, which is
    # httpx's dependency and not this package's.
    kind = type(error)
    if kind.__module__.partition(".")[0] != "httpcore":
        return False
    return kind.__name__.endswith("Timeout") or kind.__name__ in _HTTPCORE_FAULTS


def _request_of(error: BaseException) -> tuple[str, str] | None:
    """The method and path of the request that timed out, if it is knowable.

    ``httpx`` attaches the ``Request`` to the exception it raises, so the
    chain walk that found the timeout also finds what was being asked.
    The ``httpcore`` exception underneath it carries no such thing, which
    is why this walks rather than reading the timeout directly.

    Only the path is kept. The host is the registry in every case, and a
    query string would carry ids that make two occurrences of the same
    call look like different endpoints to the join downstream.
    """
    for current in _chain(error):
        # ``getattr`` with a default does not make this safe, and that is
        # not a hypothetical: ``httpx.HTTPError.request`` is a *property*
        # that raises ``RuntimeError("The .request property has not been
        # set.")`` when the exception was constructed without one, and a
        # raising property propagates straight through the default. Left
        # unguarded this turns every timeout raised without a request --
        # anything the harness constructs itself, and anything a future
        # httpx raises before it has a request to attach -- into a
        # classifier that dies, which CI reads as a run it must not retry.
        try:
            request = getattr(current, "request", None)
        except Exception:
            continue
        method = getattr(request, "method", None)
        url = getattr(request, "url", None)
        path = getattr(url, "path", None)
        if isinstance(method, str) and isinstance(path, str):
            return method, path
    return None


def _carries_http_status(error: BaseException) -> bool:
    """Whether the registry answered, whatever it answered with.

    Two shapes, because the two clients in this tier fail differently: the
    SDK turns a non-2xx into a ``stardag`` error carrying ``status_code``,
    while the harness's own direct ``httpx`` calls raise
    ``HTTPStatusError`` from ``raise_for_status``.
    """
    if isinstance(error, httpx.HTTPStatusError):
        return True
    return isinstance(getattr(error, "status_code", None), int)


def record_transport_timeout(
    deployment: Deployment,
    *,
    nodeid: str,
    phase: str,
    error: BaseException,
    timeout: BaseException,
    already_probed: bool = False,
) -> BootProbe:
    """Probe the registry, print the finding, and leave it for CI to read.

    Called at the moment a scenario fails, which is the only moment the
    probe answers a useful question: a minute later the contention that
    caused the timeout has passed and the registry answers everything.

    ``phase`` names the pytest phase for the record and the filename --
    fixture teardown runs after a failed call, so one scenario can time
    out twice, and without the phase the second record would overwrite
    the first.

    ``already_probed`` is *not* derived from it, and that separation is
    the point. Only ``BootCheckUnanswered`` arrives with its probe done,
    and only its own raiser knows that; inferring it from the teardown
    phase swallowed the probe for any fixture whose cleanup timed out.
    """
    # Taken before the probe, which is allowed fifteen seconds. The join
    # downstream looks for this request in the registry's access log
    # within a window around this instant, and a timestamp taken after the
    # probe would put the failure up to fifteen seconds late -- wide
    # enough to catch a neighbouring call to the same endpoint instead.
    observed_at = dt.datetime.now(dt.timezone.utc)
    request = _request_of(error)

    probe = (
        probe_boot(deployment.api_url)
        if not already_probed
        else BootProbe(
            answered=False,
            elapsed=0.0,
            boot_id=None,
            error=None,
            probed=False,
        )
    )
    label = probe.label(deployment.boot_id)
    record = _render(
        deployment,
        nodeid=nodeid,
        phase=phase,
        error=error,
        timeout=timeout,
        probe=probe,
        observed_at=observed_at,
        request=request,
    )

    # Always visible, marker or no marker. A developer running the tier
    # locally sets none of the environment variables below, and the
    # printed record is then the whole of the diagnosis -- pytest shows
    # captured output for a failing test, which this always is.
    print(record, file=sys.stderr)

    # A probe that answers from a *different* container has not diagnosed
    # this issue's failure class; it has identified a recycle, off the
    # same boot nonce the post-scenario check uses. Say so where CI acts
    # on it, because the two want different recoveries: a recycle's retry
    # re-provisions, since the replacement's database is empty, and a
    # transport timeout's does not. Leaving it to the post-scenario check
    # to notice would be leaving it to a second read that may not get an
    # answer either.
    #
    # It is kept *out* of the timeout marker for the same reason: that
    # file is the count this issue escalates on, and a recycle counted as
    # a transport timeout would make the count say the opposite of what
    # happened.
    if label == "RECYCLE":
        assert probe.boot_id is not None
        record_recycle(deployment.boot_id, probe.boot_id)

    directory = _diagnostics_dir()
    if directory is None:
        return probe

    try:
        directory.mkdir(parents=True, exist_ok=True)
        stem = _record_name(nodeid, phase)
        (directory / f"{stem}.txt").write_text(record + "\n")
        (directory / f"{stem}{SIDECAR_SUFFIX}").write_text(
            json.dumps(
                _facts(
                    deployment,
                    nodeid=nodeid,
                    phase=phase,
                    error=error,
                    timeout=timeout,
                    probe=probe,
                    label=label,
                    observed_at=observed_at,
                    request=request,
                ),
                indent=2,
                sort_keys=True,
            )
            + "\n"
        )
        if label != "RECYCLE":
            # One line per occurrence, appended: twelve xdist workers write
            # this file, in separate processes, and a whole-file write would
            # mean the last one to fail is the only one CI ever hears about.
            # Appends of a single short line are atomic enough for that.
            with (directory / TIMEOUT_MARKER_NAME).open("a") as marker:
                marker.write(
                    f"{nodeid} [{phase}] -- {type(timeout).__name__} -- {label}\n"
                )
    except OSError as failure:  # pragma: no cover - diagnostics only
        print(
            f"{CLASSIFICATION_FAILED}: could not write the timeout "
            f"diagnostics to {directory}: {failure}",
            file=sys.stderr,
        )
    return probe


def record_non_timeout_failure(
    *, nodeid: str, phase: str, error: BaseException | None
) -> None:
    """Name a failure that the retry must not be allowed to paper over.

    The other half of the discriminator, and the half that only matters
    because twelve scenarios share a run. A transport timeout in one
    worker says nothing about what the other eleven met; CI reads this
    file and refuses to retry a run that holds any, so a real failure
    cannot be carried to green on the back of somebody else's timeout.

    ``error`` may be ``None``: a strict ``xfail`` that passes is a failed
    report carrying no exception at all, and dropping it would let the
    retry turn an XPASS green. Anything that failed and is not a
    transport timeout belongs here, whether or not it brought an
    exception with it.

    Every phase writes here, because fixtures talk to the registry at
    both ends of a scenario and a fixture failing for real must end the
    run exactly as a scenario body would. One failure is exempt and it is
    exempt by type, not by phase: ``RegistryContainerRecycled`` has its
    own marker and its own retry, which re-provisions precisely because a
    lost database explains *every* failure in the run -- which a
    transport timeout does not.
    """
    directory = _diagnostics_dir()
    if directory is None:
        return
    try:
        directory.mkdir(parents=True, exist_ok=True)
        with (directory / NON_TIMEOUT_MARKER_NAME).open("a") as marker:
            described = type(error).__name__ if error else "failed with no exception"
            marker.write(f"{nodeid} [{phase}] -- {described}\n")
    except OSError as failure:  # pragma: no cover - diagnostics only
        # Fail closed. An unwritten marker is read by CI as "nothing real
        # broke", so say so where the marker cannot be forged away.
        print(
            f"{CLASSIFICATION_FAILED}: could not write the failure marker "
            f"to {directory}: {failure}",
            file=sys.stderr,
        )


def _diagnostics_dir() -> Path | None:
    path = os.environ.get(DIAGNOSTICS_DIR_ENV, "").strip()
    return Path(path) if path else None


def _record_name(nodeid: str, phase: str) -> str:
    """The shared stem of one occurrence's two files, without a suffix.

    Node id, phase and pid together. The phase is not decoration: fixture
    teardown runs after a failed call, so one scenario can produce a
    call-phase record and a teardown-phase one, and without it the second
    would overwrite the first -- losing the probe, which only the first
    one carries.

    The stem is shared by the human record and its JSON sidecar, so the
    two are obviously one occurrence in a directory listing.
    """
    slug = "".join(char if char.isalnum() else "-" for char in nodeid).strip("-")
    return f"timeout-{phase}-{slug[:120]}-{os.getpid()}"


def _facts(
    deployment: Deployment,
    *,
    nodeid: str,
    phase: str,
    error: BaseException,
    timeout: BaseException,
    probe: BootProbe,
    label: str,
    observed_at: dt.datetime,
    request: tuple[str, str] | None,
) -> dict[str, Any]:
    """Everything the access-log join needs, and nothing it has to parse.

    Deliberately facts only. There is no verdict field here: the verdict
    is what the join produces, and a sidecar carrying a provisional one
    would be quoted as the answer by anyone who read it first.
    """
    return {
        "nodeid": nodeid,
        "phase": phase,
        "observed_at": observed_at.isoformat(),
        "request_method": request[0] if request else None,
        "request_path": request[1] if request else None,
        "raised": f"{type(error).__module__}.{type(error).__name__}",
        "timeout": f"{type(timeout).__module__}.{type(timeout).__name__}",
        "message": str(error),
        "boot_id_at_provisioning": deployment.boot_id,
        "probe": {
            "probed": probe.probed,
            "answered": probe.answered,
            "elapsed": round(probe.elapsed, 3),
            "boot_id": probe.boot_id,
            "error": probe.error,
        },
        "probe_label": label,
    }


# What a timeout at each phase cost, which is no longer the same sentence
# for all three. A call-phase timeout means no assertion was reached; a
# teardown one can follow a body that passed and proved what it set out to.
_WHAT_WAS_LOST = {
    "setup": "The scenario never started, so it proved nothing.",
    "call": "No assertion in this scenario was ever evaluated.",
    "teardown": (
        "The scenario's own result stands; its cleanup, or the check that "
        "the registry survived it, did not complete."
    ),
}


def _render(
    deployment: Deployment,
    *,
    nodeid: str,
    phase: str,
    error: BaseException,
    timeout: BaseException,
    probe: BootProbe,
    observed_at: dt.datetime,
    request: tuple[str, str] | None,
) -> str:
    return "\n".join(
        [
            "",
            "=" * 72,
            "TRANSPORT TIMEOUT against the registry -- no response was received.",
            _WHAT_WAS_LOST.get(phase, "The scenario did not complete."),
            "=" * 72,
            f"  scenario:  {nodeid}",
            f"  phase:     {phase}",
            f"  at:        {observed_at.isoformat(timespec='seconds')}",
            "  request:   " + (f"{request[0]} {request[1]}" if request else "unknown"),
            f"  raised:    {type(error).__module__}.{type(error).__name__}: {error}",
            f"  timeout:   {type(timeout).__module__}.{type(timeout).__name__}",
            f"  registry:  {deployment.api_url}",
            f"  boot id:   {deployment.boot_id} (at provisioning time)",
            "",
            "  boot probe: not run -- see below"
            if not probe.probed
            else (
                f"  boot probe: {'answered' if probe.answered else 'no answer'} "
                f"in {probe.elapsed:.1f}s"
                + (f", boot id {probe.boot_id}" if probe.boot_id else "")
                + (f", {probe.error}" if probe.error else "")
            ),
            "",
            "  " + probe.verdict(deployment.boot_id),
            "=" * 72,
            "",
        ]
    )
