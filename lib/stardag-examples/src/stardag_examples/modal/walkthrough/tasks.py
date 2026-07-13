"""Task definitions for the Modal integration walkthrough.

A small but non-trivial DAG, shaped to exercise the interesting parts of
detached/reactive Modal execution:

- ``ProcessShard``: a *fan-out* of medium-length tasks (sleep-based). The
  app tags them with a named concurrency-limit key (see ``app.py``), so
  with a configured cap you can watch them queue and drain in the
  registry UI.
- ``LongScan``: one longer-running task, routed to a dedicated worker
  with a higher timeout — long enough to observe detached execution
  (e.g. re-trigger the build while it runs and see it *re-attach*
  instead of re-executing).
- ``Summarize``: a task with *dynamic dependencies*: it first requires
  ``PlanShards`` (static dep), then yields one ``ProcessShard`` per
  discovered shard from its ``run()`` generator. Between the yield and
  the resume the task is *suspended* — with no orchestrator holding it
  in memory in reactive mode.

All sleeps are parameters so tests (and impatient users) can build the
same DAG with ``*_sleep_seconds=0``.
"""

import hashlib
import time

import stardag as sd
from pydantic import BaseModel

sd.auto_namespace(__name__)

# Cap the fan-out to something easy to eyeball in the UI.
MIN_SHARDS = 4
MAX_SHARDS = 8


class ShardSpec(BaseModel):
    shard_id: int
    size: int


class PlanShards(sd.Task[list[ShardSpec]]):
    """Pretends to inspect ``source`` and decide which shards to process.

    Uses a deterministic hash of ``source`` so the same source always
    yields the same DAG, while different sources yield different fan-outs
    (handy for re-triggering a build with an extra root).
    """

    source: str

    def run(self) -> None:
        digest = hashlib.sha256(self.source.encode()).digest()
        num_shards = MIN_SHARDS + digest[0] % (MAX_SHARDS - MIN_SHARDS + 1)
        self._save(
            [
                # digest is 32 bytes; byte[i+1] is in range for i < MAX_SHARDS < 32
                ShardSpec(shard_id=i, size=1 + digest[i + 1] % 100)
                for i in range(num_shards)
            ]
        )


class ProcessShard(sd.Task[dict[str, float]]):
    """A medium-length unit of work — the fan-out under the concurrency limit.

    The app's ``limit_key_selector`` tags every ``ProcessShard`` with the
    ``SHARD_LIMIT_KEY`` named limit (see ``app.py``), so at most
    ``max_concurrent`` of them run at once — across builds, enforced by
    the registry.
    """

    source: str
    spec: ShardSpec
    sleep_seconds: float = 20.0

    def run(self) -> None:
        time.sleep(self.sleep_seconds)
        self._save(
            {"shard_id": float(self.spec.shard_id), "value": float(self.spec.size)}
        )


class Summarize(sd.Task[dict[str, float]]):
    """Aggregates the shard results — via *dynamic dependencies*.

    The shard fan-out isn't known until ``PlanShards`` has run, so it
    can't be declared in ``requires()``. Instead ``run()`` is a generator:
    it yields the ``ProcessShard`` batch, the build suspends the task
    until all shards are complete, then resumes the generator to
    aggregate. In the UI the yielded edges render as *dynamic* deps.
    """

    source: str
    shard_sleep_seconds: float = 20.0

    def requires(self):
        return PlanShards(source=self.source)

    def run(self):
        specs = self.requires().load()
        shards = [
            ProcessShard(
                source=self.source,
                spec=spec,
                sleep_seconds=self.shard_sleep_seconds,
            )
            for spec in specs
        ]
        yield shards  # suspend until every shard is complete
        values = [shard.load()["value"] for shard in shards]
        self._save(
            {
                "num_shards": float(len(shards)),
                "total": float(sum(values)),
                "max": float(max(values)),
            }
        )


class LongScan(sd.Task[dict[str, float]]):
    """One long-running task (sleep-based stand-in for a slow scan/training).

    Routed to the ``long`` worker (higher timeout) by the app's
    ``worker_selector``. While it sleeps, try re-triggering the build —
    the running function call is re-attached, not re-executed.
    """

    source: str
    sleep_seconds: float = 240.0

    def run(self) -> None:
        time.sleep(self.sleep_seconds)
        digest = hashlib.sha256(self.source.encode()).digest()
        self._save({"checksum": float(int.from_bytes(digest[:4], "big"))})


class Report(sd.Task[str]):
    """Root task: joins the shard summary and the long scan."""

    summary: sd.TaskLoads[dict[str, float]]
    scan: sd.TaskLoads[dict[str, float]]

    def requires(self):
        return [self.summary, self.scan]

    def run(self) -> None:
        summary = self.summary.load()
        scan = self.scan.load()
        self._save(
            f"Processed {summary['num_shards']:.0f} shards "
            f"(total={summary['total']:.0f}, max={summary['max']:.0f}); "
            f"scan checksum {scan['checksum']:.0f}."
        )


def report_dag(
    source: str = "demo",
    *,
    shard_sleep_seconds: float = 20.0,
    scan_sleep_seconds: float = 240.0,
) -> Report:
    """The walkthrough DAG for one ``source``."""
    return Report(
        summary=Summarize(source=source, shard_sleep_seconds=shard_sleep_seconds),
        scan=LongScan(source=source, sleep_seconds=scan_sleep_seconds),
    )
