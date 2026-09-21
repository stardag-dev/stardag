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

``/_harness/boot`` separates them, because it is the one endpoint that
returns a closure variable and touches no database. If it answers
promptly while a real endpoint has just timed out, the container is alive
and serving and the *database path* is what is blocked -- hypothesis B.
If it does not answer either, the container itself is unreachable --
hypothesis A. One probe at the moment of failure turns the next
occurrence into a diagnosis instead of another row in a table.

Nothing here retries or suppresses anything by itself. It classifies, it
probes, and it writes down what it found; CI reads the marker and decides.
"""

from __future__ import annotations

import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

import httpx

from ._harness import Deployment, read_boot_id

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

# Short on purpose. The question the probe asks is not "does the registry
# work" but "did it answer *promptly* while a real call was timing out",
# and a generous timeout blurs exactly that distinction. Fifteen seconds
# is far longer than the endpoint's own work (it returns a string held in
# a closure) and far shorter than the 30s the SDK client had already spent
# per attempt before giving up.
BOOT_PROBE_TIMEOUT_SECONDS = 15.0


@dataclass(frozen=True)
class BootProbe:
    """What ``/_harness/boot`` said, and how fast it said it."""

    answered: bool
    elapsed: float
    boot_id: str | None
    error: str | None

    def label(self, expected_boot_id: str) -> str:
        """Which hypothesis this probe supports, as a word CI can count."""
        if not self.answered:
            return "HYPOTHESIS A"
        if self.boot_id != expected_boot_id:
            return "RECYCLE"
        return "HYPOTHESIS B"

    def verdict(self, expected_boot_id: str) -> str:
        """Which hypothesis this probe supports, in one sentence."""
        if not self.answered:
            return (
                f"HYPOTHESIS A -- the boot probe also failed after "
                f"{self.elapsed:.1f}s ({self.error}). Nothing answered at "
                f"all, so the block is not in the database path: the "
                f"container is starved, throttled or wedged -- or, since the "
                f"probe runs in the stalled process, the runner itself is. "
                f"The levers are the container's resources and the tier's "
                f"worker count."
            )
        if self.boot_id != expected_boot_id:
            return (
                f"NEITHER -- the boot probe answered in {self.elapsed:.1f}s "
                f"with a *different* boot id ({expected_boot_id} -> "
                f"{self.boot_id}), so the container was replaced. This is the "
                f"recycle case; the post-scenario check records it separately "
                f"and CI re-provisions before retrying."
            )
        return (
            f"HYPOTHESIS B -- the boot probe answered in {self.elapsed:.1f}s, "
            f"from the same container ({self.boot_id}). The process is alive "
            f"and serving HTTP, so what timed out is the *database* path: "
            f"pool exhaustion or lock waits under concurrent clients. That "
            f"makes this a product signal, and the lever is upstream in the "
            f"registry's locking."
        )


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

    What is left is the case where no response exists to reason about:
    ``httpx.TimeoutException`` and its four subclasses, connect, read,
    write and pool. Nothing else -- not a connection reset, not a
    protocol error -- because the evidence names timeouts and a wider net
    would start covering failures nobody has looked at.
    """
    found: BaseException | None = None
    for current in _chain(error):
        if _carries_http_status(current) or isinstance(current, AssertionError):
            return None
        if found is None and _is_timeout(current):
            found = current
    return found


def _chain(error: BaseException) -> Iterator[BaseException]:
    """Every exception reachable from ``error``, causes and context alike.

    Both links are followed because the two matter for different call
    sites: ``__cause__`` for anything re-raised explicitly, ``__context__``
    for a timeout that surfaces from inside an ``except`` block. That is
    the whole of it -- exception *groups* are not unpacked, because
    nothing in this tier raises one: the one scenario that runs calls
    concurrently uses a bare ``asyncio.gather``, which re-raises the first
    exception rather than collecting them. A ``TaskGroup`` here would need
    this to grow a case.

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


def _is_timeout(error: BaseException) -> bool:
    """Whether this exception means "no response arrived in time"."""
    if isinstance(error, httpx.TimeoutException):
        return True
    # httpx maps httpcore's transport exceptions to its own at the
    # transport boundary, so the httpx type is normally the one raised --
    # but the httpcore one is what a traceback names, and a transport that
    # re-raised rather than mapped would otherwise read as a real failure.
    # Matched structurally rather than by importing httpcore, which is
    # httpx's dependency and not this package's.
    kind = type(error)
    return kind.__module__.partition(".")[0] == "httpcore" and kind.__name__.endswith(
        "Timeout"
    )


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
    error: BaseException,
    timeout: BaseException,
) -> BootProbe:
    """Probe the registry, print the finding, and leave it for CI to read.

    Called at the moment a scenario fails, which is the only moment the
    probe answers a useful question: a minute later the contention that
    caused the timeout has passed and the registry answers everything.
    """
    probe = probe_boot(deployment.api_url)
    record = _render(
        deployment, nodeid=nodeid, error=error, timeout=timeout, probe=probe
    )

    # Always visible, marker or no marker. A developer running the tier
    # locally sets none of the environment variables below, and the
    # printed record is then the whole of the diagnosis -- pytest shows
    # captured output for a failing test, which this always is.
    print(record, file=sys.stderr)

    directory = _diagnostics_dir()
    if directory is None:
        return probe

    try:
        directory.mkdir(parents=True, exist_ok=True)
        (directory / _record_name(nodeid)).write_text(record + "\n")
        # One line per occurrence, appended: twelve xdist workers write
        # this file, in separate processes, and a whole-file write would
        # mean the last one to fail is the only one CI ever hears about.
        # Appends of a single short line are atomic enough for that.
        with (directory / TIMEOUT_MARKER_NAME).open("a") as marker:
            marker.write(
                f"{nodeid} -- {type(timeout).__name__} -- "
                f"{probe.label(deployment.boot_id)}\n"
            )
    except OSError as failure:  # pragma: no cover - diagnostics only
        print(
            f"Could not write the timeout diagnostics to {directory}: {failure}",
            file=sys.stderr,
        )
    return probe


def _diagnostics_dir() -> Path | None:
    path = os.environ.get(DIAGNOSTICS_DIR_ENV, "").strip()
    return Path(path) if path else None


def _record_name(nodeid: str) -> str:
    """A filename per occurrence, unique across the tier's worker processes."""
    slug = "".join(char if char.isalnum() else "-" for char in nodeid).strip("-")
    return f"timeout-{slug[:120]}-{os.getpid()}.txt"


def _render(
    deployment: Deployment,
    *,
    nodeid: str,
    error: BaseException,
    timeout: BaseException,
    probe: BootProbe,
) -> str:
    return "\n".join(
        [
            "",
            "=" * 72,
            "TRANSPORT TIMEOUT against the registry -- no response was received,",
            "so no assertion in this scenario was ever evaluated.",
            "=" * 72,
            f"  scenario:  {nodeid}",
            f"  raised:    {type(error).__module__}.{type(error).__name__}: {error}",
            f"  timeout:   {type(timeout).__module__}.{type(timeout).__name__}",
            f"  registry:  {deployment.api_url}",
            f"  boot id:   {deployment.boot_id} (at provisioning time)",
            "",
            f"  boot probe: {'answered' if probe.answered else 'no answer'} "
            f"in {probe.elapsed:.1f}s"
            + (f", boot id {probe.boot_id}" if probe.boot_id else "")
            + (f", {probe.error}" if probe.error else ""),
            "",
            "  " + probe.verdict(deployment.boot_id),
            "=" * 72,
            "",
        ]
    )
