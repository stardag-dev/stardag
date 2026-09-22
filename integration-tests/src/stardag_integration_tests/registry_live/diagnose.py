"""Turn a transport timeout's facts into a verdict, using the access log.

The boot probe that runs at the moment of failure can only establish
whether the container was serving (see ``_diagnostics``). It cannot tell
a slow handler from a response that was produced and never arrived,
because the endpoint it asks touches no database and would answer just as
fast either way.

The registry's own access log can, and it is already in the artifact. Each
line carries both halves of the question::

    2026-09-22 10:08:57+00:00 ta-01M349...  GET /health -> 200 OK  \
(duration: 5.88 s, execution: 286.5 ms)

``execution`` is time inside the handler; ``duration`` is the whole
request as the server saw it, so ``duration - execution`` is queueing.
Against one timed-out request that gives three distinguishable answers:

* the handler took seconds -- **hypothesis B**, the database path;
* the handler was fast but the request sat queued -- **hypothesis A**,
  a starved container;
* the server handled it in milliseconds, or never logged it at all --
  **hypothesis C**: nothing on the registry was slow, and the response
  did not reach the client.

C is not a fallback for "the other two did not match". It is a positive
finding, and it is what the first run to carry this instrument actually
showed: 6257 requests, maximum handler time 460 ms, maximum total
duration 5.88 s, against a client that waited 30 s and gave up in
``_receive_response_body`` -- after the response head had arrived.

This runs *after* the Modal logs are dumped, which is why it is a
separate pass rather than part of the classifier. It decides nothing: it
reads the artifact, writes ``verdicts.txt`` into it, prints the same text
so the verdict is visible without downloading anything, and exits 0
whatever it found.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path

# The registry's log, as ``provision logs`` names it.
REGISTRY_LOG_NAME = "registry.log"

# Where this writes its answer, inside the same directory.
VERDICTS_NAME = "verdicts.txt"

# The verdict is the one output that answers the question people actually
# ask about a red tier, and until this it was the one output nobody saw
# without downloading an artifact. On a runner it also goes out as a
# workflow annotation, next to the retry warnings it explains, so the
# answer is on the run summary rather than a click away. The full
# reasoning stays in the artifact -- an annotation is a headline.
ANNOTATION_TITLE = "Registry-live verdict"

# How far *back* from the moment the client gave up to look for the
# server's line. The client sends at T, waits its timeout, and the record
# is written at roughly T+timeout; the server, if it answered at all,
# logged somewhere in between. Ninety seconds covers the 30s SDK client
# timeout with room for a slower one and for clock skew between a GitHub
# runner and a Modal container.
#
# It is deliberately not narrowed to make the match unique, because it
# cannot be. Most paths on this API carry an id and match exactly one
# call, but the ones that create a resource -- ``POST /api/v1/builds``,
# which is what timed out on the occurrence this was built against -- do
# not, and twelve workers issue them at once. The verdicts below say how
# many candidates a window held and weaken their claim accordingly,
# rather than pretending a single one was identified.
LOOKBACK_SECONDS = 90.0

# And a little forward, purely for skew: the server's clock can be ahead
# of the runner's, which would otherwise put its line after the moment the
# failure was recorded.
LOOKAHEAD_SECONDS = 15.0

# What counts as the server being slow. Measured handler times on this
# tier sit under half a second and the whole distribution is tight, so
# five seconds is an order of magnitude outside anything observed while
# still being a small fraction of the client's patience. A handler under
# this bar cannot be what produced a 30s timeout.
SLOW_EXECUTION_SECONDS = 5.0

# The same bar for the whole request. Above it with a fast handler means
# the time went on queueing rather than in the code.
SLOW_DURATION_SECONDS = 10.0

_LINE = re.compile(
    r"^(?P<at>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}(?:\.\d+)?[+-]\d{2}:\d{2})\s+"
    r"(?P<container>\S+)\s+"
    r"(?P<method>[A-Z]+)\s+(?P<path>\S+)\s+->\s+(?P<status>\d{3})\b"
    r".*?\(duration:\s*(?P<duration>[\d.]+)\s*(?P<duration_unit>ms|s),\s*"
    r"execution:\s*(?P<execution>[\d.]+)\s*(?P<execution_unit>ms|s)\)"
)


@dataclass(frozen=True)
class Served:
    """One request as the registry saw it."""

    at: dt.datetime
    container: str
    method: str
    path: str
    status: int
    duration: float
    execution: float

    @property
    def queued(self) -> float:
        """Time the request was not in the handler, floored at zero.

        The two numbers are measured by different layers, so a handler
        that finishes as the duration is sampled can leave this very
        slightly negative. Reporting a negative queue would read as a
        measurement being wrong rather than as the rounding it is.
        """
        return max(0.0, self.duration - self.execution)

    def describe(self) -> str:
        return (
            f"{self.at.isoformat(timespec='seconds')} {self.method} {self.path} "
            f"-> {self.status} (duration {self.duration:.3f}s, "
            f"execution {self.execution:.3f}s)"
        )


@dataclass(frozen=True)
class AccessLog:
    """Every request the registry logged, and the span it covers."""

    served: list[Served]
    present: bool

    @property
    def span(self) -> tuple[dt.datetime, dt.datetime] | None:
        if not self.served:
            return None
        return self.served[0].at, self.served[-1].at

    def covers_window_before(self, moment: dt.datetime) -> bool:
        """Whether the log reaches across the whole window searched below.

        This gates exactly one conclusion: the one drawn from *absence*.
        Finding a matching request needs no such check -- the evidence is
        there to read -- but concluding that the server never saw one
        does, because ``modal app logs`` takes a line budget and a
        ``--since`` window and drops the oldest lines when it runs out.
        Reading a truncated dump as "the request never arrived" would
        turn a missing log into a confident diagnosis.
        """
        span = self.span
        if span is None:
            return False
        return (
            span[0] <= moment - dt.timedelta(seconds=LOOKBACK_SECONDS)
            and span[1] >= moment
        )

    def around(
        self, moment: dt.datetime, *, method: str | None, path: str | None
    ) -> list[Served]:
        start = moment - dt.timedelta(seconds=LOOKBACK_SECONDS)
        end = moment + dt.timedelta(seconds=LOOKAHEAD_SECONDS)
        window = [row for row in self.served if start <= row.at <= end]
        if method is None or path is None:
            return window
        return [row for row in window if row.method == method and row.path == path]

    def slowest(self) -> tuple[Served | None, Served | None]:
        """The worst request by duration and by handler time.

        The run-level counterpart to the per-occurrence join, and on its
        own often the whole answer: if nothing in several thousand
        requests came near the client's timeout, no per-occurrence match
        is going to find that it did.
        """
        if not self.served:
            return None, None
        return (
            max(self.served, key=lambda row: row.duration),
            max(self.served, key=lambda row: row.execution),
        )


def parse_access_log(path: Path) -> AccessLog:
    """Read the dumped Modal log and keep the access lines.

    Everything else in the file -- container boot, application logging,
    the header ``provision logs`` writes -- is skipped rather than
    rejected, because this has to keep working when the app starts
    logging something new.
    """
    if not path.is_file():
        return AccessLog(served=[], present=False)
    served: list[Served] = []
    for line in path.read_text(errors="replace").splitlines():
        match = _LINE.match(line.strip())
        if match is None:
            continue
        served.append(
            Served(
                at=dt.datetime.fromisoformat(match["at"]),
                container=match["container"],
                method=match["method"],
                # Query-stripped, to match the record's side. The sidecar
                # stores `httpx.URL.path`, which never carries a query, so
                # if this line ever did the two could not match and every
                # call with parameters would read as absent -- hypothesis C
                # manufactured out of a formatting difference. Neither log
                # sampled so far prints one (including
                # `POST /builds/{id}/tasks/{id}/start`, which carries
                # `execution_id` as a query parameter), so this is
                # belt-and-braces rather than a fix; it costs one call and
                # removes a silent failure that would look exactly like a
                # finding.
                path=match["path"].partition("?")[0],
                status=int(match["status"]),
                duration=_seconds(match["duration"], match["duration_unit"]),
                execution=_seconds(match["execution"], match["execution_unit"]),
            )
        )
    served.sort(key=lambda row: row.at)
    return AccessLog(served=served, present=True)


def _seconds(value: str, unit: str) -> float:
    return float(value) / 1000.0 if unit == "ms" else float(value)


def verdict_for(occurrence: dict, log: AccessLog) -> tuple[str, list[str]]:
    """The hypothesis this occurrence supports, and the evidence for it.

    The probe's own label is honoured where it is sound. A probe that did
    not answer, or answered slowly, *identified* hypothesis A at the
    moment of failure, and no later reading of a log can overturn a
    direct observation that nothing was serving. Only the prompt-probe
    case is left open for the log to settle, which is exactly the case
    the probe could not decide.
    """
    label = occurrence.get("probe_label")
    if label == "HYPOTHESIS A":
        return "HYPOTHESIS A", [
            "The boot probe itself did not get a prompt answer, which "
            "identifies a container that was not serving. The access log "
            "cannot overturn that, and is reported below only as context."
        ]
    if label == "RECYCLE":
        return "RECYCLE", [
            "The probe answered from a different container: this is the "
            "recycle case, which has its own marker and its own recovery."
        ]

    at = _parsed_time(occurrence.get("observed_at"))
    if at is None:
        return "NO VERDICT", [
            "The record carries no usable timestamp, so its request "
            "cannot be located in the access log."
        ]
    if not log.present:
        return "NO VERDICT", [
            f"No {REGISTRY_LOG_NAME} in this artifact. The Modal log dump "
            f"did not run, or produced nothing."
        ]
    method = occurrence.get("request_method")
    path = occurrence.get("request_path")
    matches = log.around(at, method=method, path=path)
    identified = method is not None and path is not None

    if not matches:
        # Absence is the one conclusion that needs the window whole, so it
        # is gated before it is drawn -- and only here, where there is no
        # positive finding it could suppress.
        if not log.covers_window_before(at):
            return "NO VERDICT", [
                _incomplete_window(log, at),
                "So the absence of a matching request is a gap in the "
                "evidence rather than evidence.",
            ]
        if not identified:
            return "HYPOTHESIS C", [
                "The failing request could not be identified from the "
                "exception, so this reads the whole window instead.",
                f"The registry logged nothing at all in the "
                f"{LOOKBACK_SECONDS:.0f}s before the client gave up, and "
                f"the log does cover that window. Nothing on the registry "
                f"took the time the client spent waiting.",
            ]
        return "HYPOTHESIS C", [
            f"The registry logged no {method} {path} in the "
            f"{LOOKBACK_SECONDS:.0f}s before the client gave up, and the "
            f"log does cover that window. So the request did not reach the "
            f"handler -- or reached it and was never logged, which the "
            f"server only fails to do if it never completed the response.",
            "Either way nothing on the registry took the time the client "
            "spent waiting.",
        ]

    return _decide(matches, log, at, method=method, path=path)


def _decide(
    matches: list[Served],
    log: AccessLog,
    at: dt.datetime,
    *,
    method: str | None,
    path: str | None,
) -> tuple[str, list[str]]:
    """Choose between B, A and C given the rows that could be the failure.

    **One function for both callers**, which is the point of it existing.
    It was two, and the copy that ran when the exception carried no
    request had grown only a B branch -- so a queued row there produced
    "HYPOTHESIS C ... nothing on the registry took the time" while the
    same row on the other path produced A. Two verdicts for one set of
    facts, and one of them wrong in the direction this whole pass exists
    to prevent. The wording still differs; the decision does not.

    Order is load-bearing: B, then A, then the coverage gate, then C.
    Both positives rest on a line that is *present*, so a truncated dump
    cannot make them wrong and must not be allowed to discard them. C
    rests on nothing in the window being slow, which a truncated dump can
    only fail to establish.
    """
    identified = method is not None and path is not None

    # How confidently a matching line can be called *the* line. One
    # candidate is an identification; several are a set the failing call
    # is somewhere in, and the two support different strengths of claim.
    # Only the positive verdicts need this caveat -- a window in which
    # nothing at all was slow rules out B and A for every member of it at
    # once, so C does not weaken with the count.
    if not identified:
        ambiguity = (
            " The failing request could not be identified from the "
            "exception, so this is the worst of everything in the window "
            "rather than a line attributed to the failure."
        )
    elif len(matches) == 1:
        ambiguity = ""
    else:
        ambiguity = (
            f" This is the worst of {len(matches)} calls to {method} {path} "
            f"in the window, which is a candidate rather than an "
            f"identification: this path carries no id, so the failing call "
            f"cannot be picked out from its neighbours."
        )

    slow_handler = [row for row in matches if row.execution >= SLOW_EXECUTION_SECONDS]
    if slow_handler:
        worst = max(slow_handler, key=lambda row: row.execution)
        return "HYPOTHESIS B", [
            f"The registry spent {worst.execution:.1f}s inside the handler: "
            f"{worst.describe()}.{ambiguity}",
            "That is the database path being slow, which makes this a "
            "product signal. The lever is upstream in the registry's "
            "locking.",
        ]

    queued = [row for row in matches if row.duration >= SLOW_DURATION_SECONDS]
    if queued:
        worst = max(queued, key=lambda row: row.duration)
        return "HYPOTHESIS A", [
            f"A request took {worst.duration:.1f}s in total but only "
            f"{worst.execution:.1f}s in the handler, so "
            f"{worst.queued:.1f}s of it was queueing: "
            f"{worst.describe()}.{ambiguity}",
            "The container was not able to get to the work. The levers are "
            "its resources and the tier's worker count.",
        ]

    if not log.covers_window_before(at):
        return "NO VERDICT", [
            _incomplete_window(log, at),
            "Rows were found and none of them was slow, but that is only a "
            "claim about the part of the window that survived the dump.",
        ]

    worst = max(matches, key=lambda row: row.duration)
    if not identified:
        subject = (
            f"The failing request could not be identified from the "
            f"exception, so this reads the whole window: the slowest of "
            f"{len(matches)} requests in it was served"
        )
    elif len(matches) == 1:
        subject = "The registry served this request"
    else:
        subject = (
            f"The slowest of {len(matches)} calls to {method} {path} in the "
            f"window was served"
        )
    return "HYPOTHESIS C", [
        f"{subject} in {worst.duration:.3f}s "
        f"({worst.execution:.3f}s in the handler): {worst.describe()}.",
        "Nothing matching was slow, so whichever of them timed out, it was "
        "not the server that took the time. The response was produced and "
        "never received: the loss is between the container and the runner, "
        "rather than anywhere this tier's code can reach.",
    ]


def _incomplete_window(log: AccessLog, at: dt.datetime) -> str:
    """Say which part of the window is missing, in the log's own terms."""
    span = log.span
    where = (
        f"{span[0].isoformat(timespec='seconds')} .. "
        f"{span[1].isoformat(timespec='seconds')}"
        if span
        else "empty"
    )
    return (
        f"The access log does not cover the {LOOKBACK_SECONDS:.0f}s before "
        f"{at.isoformat(timespec='seconds')} (it spans {where}), so part of "
        f"the window was dropped from the dump rather than being quiet."
    )


def _parsed_time(value: object) -> dt.datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = dt.datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.timezone.utc)


def load_occurrences(directory: Path) -> list[tuple[Path, dict]]:
    """Every timeout sidecar in the directory, including a retried attempt's.

    ``attempt-1/`` holds the first attempt's records when a retry
    happened, and those are exactly the occurrences the retry was granted
    for -- so a pass that read only the top level would have nothing to
    say about the run that prompted it.
    """
    found: list[tuple[Path, dict]] = []
    for path in sorted(directory.rglob("timeout-*.json")):
        try:
            loaded = json.loads(path.read_text())
        except (OSError, ValueError) as error:
            print(f"  could not read {path.name}: {error}", file=sys.stderr)
            continue
        if isinstance(loaded, dict):
            found.append((path, loaded))
    return found


def report(directory: Path) -> str:
    """The whole verdict text for one diagnostics directory."""
    log = parse_access_log(directory / REGISTRY_LOG_NAME)
    occurrences = load_occurrences(directory)

    lines = ["", "=" * 72, "REGISTRY-LIVE TRANSPORT TIMEOUTS -- verdicts", "=" * 72]

    slowest_duration, slowest_execution = log.slowest()
    if not log.present:
        lines.append(
            f"  No {REGISTRY_LOG_NAME} in this directory: the Modal log dump "
            f"did not run or produced nothing, so no verdict below can rest "
            f"on the server's own account."
        )
    elif slowest_duration is None or slowest_execution is None:
        lines.append(
            f"  {REGISTRY_LOG_NAME} holds no access lines. Either the "
            f"registry served nothing, or its log format changed and the "
            f"parser here no longer matches it -- check before reading "
            f"anything into the verdicts below."
        )
    else:
        span = log.span
        assert span is not None
        lines += [
            f"  access log: {len(log.served)} requests, "
            f"{span[0].isoformat(timespec='seconds')} .. "
            f"{span[1].isoformat(timespec='seconds')}",
            f"  slowest by duration:  {slowest_duration.describe()}",
            f"  slowest by execution: {slowest_execution.describe()}",
            f"  over {SLOW_EXECUTION_SECONDS:.0f}s in the handler: "
            f"{sum(1 for r in log.served if r.execution >= SLOW_EXECUTION_SECONDS)}",
            f"  over {SLOW_DURATION_SECONDS:.0f}s in total: "
            f"{sum(1 for r in log.served if r.duration >= SLOW_DURATION_SECONDS)}",
        ]

    if not occurrences:
        lines += [
            "",
            "  No transport-timeout records in this directory. Nothing to reconcile.",
            "=" * 72,
            "",
        ]
        return "\n".join(lines)

    for path, occurrence in occurrences:
        verdict, evidence = verdict_for(occurrence, log)
        attempt = path.parent.name if path.parent != directory else "latest attempt"
        request = occurrence.get("request_method") and (
            f"{occurrence['request_method']} {occurrence['request_path']}"
        )
        lines += [
            "",
            "-" * 72,
            f"  {occurrence.get('nodeid', path.stem)}",
            f"  phase {occurrence.get('phase', '?')}, {attempt}, "
            f"at {occurrence.get('observed_at', '?')}",
            f"  request:  {request or 'not identifiable from the exception'}",
            f"  probe:    {occurrence.get('probe_label', '?')}",
            "",
            f"  VERDICT: {verdict}",
        ]
        lines += [f"    {line}" for line in evidence]

    lines += ["", "=" * 72, ""]
    return "\n".join(lines)


def annotation(verdicts: list[tuple[str, dict]]) -> str | None:
    """The one-line-per-occurrence headline, or ``None`` if there is none.

    Deliberately not the evidence. A workflow annotation is read at a
    glance and truncated when long, so it carries the verdict and what it
    is about; anyone who wants the reasoning has `verdicts.txt` in the
    artifact, which the retry warning already points at.
    """
    if not verdicts:
        return None
    parts = [
        f"{verdict}: {occurrence.get('nodeid', '?')} [{occurrence.get('phase', '?')}]"
        for verdict, occurrence in verdicts
    ]
    return f"::warning title={ANNOTATION_TITLE}::" + "; ".join(parts)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="diagnose",
        description=(
            "Reconcile the registry-live timeout records with the "
            "registry's access log and write the verdicts."
        ),
    )
    parser.add_argument(
        "--dir",
        dest="directory",
        required=True,
        type=Path,
        help="The diagnostics directory holding the records and the logs.",
    )
    args = parser.parse_args(argv)

    if not args.directory.is_dir():
        # Not an error: a green run with no retry writes no directory at
        # all, and this must never be what turns a run red.
        print(f"No diagnostics directory at {args.directory}; nothing to do.")
        return 0

    text = report(args.directory)
    print(text)
    try:
        (args.directory / VERDICTS_NAME).write_text(text + "\n")
    except OSError as error:  # pragma: no cover - diagnostics only
        print(f"Could not write {VERDICTS_NAME}: {error}", file=sys.stderr)

    # Only on a runner: the `::warning` form is noise in a terminal, and
    # the printed report above is already the whole answer there.
    if os.environ.get("GITHUB_ACTIONS") == "true":
        log = parse_access_log(args.directory / REGISTRY_LOG_NAME)
        headline = annotation(
            [
                (verdict_for(occurrence, log)[0], occurrence)
                for _, occurrence in load_occurrences(args.directory)
            ]
        )
        if headline:
            print(headline)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
