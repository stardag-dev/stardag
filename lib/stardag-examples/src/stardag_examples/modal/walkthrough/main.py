"""Trigger the walkthrough DAG on the deployed Modal app.

Uses ``StardagApp.build_trigger`` — the recommended way to start builds
when using the Stardag Registry: the build id is minted in the registry
*from this process, before anything runs on Modal*, so any restart or
re-trigger resumes the same build instead of starting a new one.

Examples (after ``stardag modal deploy .../walkthrough/app.py``):

    # Default mode: resident build function orchestrates; tasks run as
    # detached Modal function calls.
    python -m stardag_examples.modal.walkthrough.main

    # Reactive mode (experimental): no resident orchestrator — the build
    # is driven by short-lived scheduler ticks.
    python -m stardag_examples.modal.walkthrough.main --reactive

    # Re-trigger an existing build (resume after failure, wake a stalled
    # reactive build) — completed tasks are skipped, running tasks are
    # re-attached:
    python -m stardag_examples.modal.walkthrough.main --build-id <id>

    # Add another root to the same build (new fan-out for another
    # source), sharing the build id and the registry DAG view:
    python -m stardag_examples.modal.walkthrough.main \\
        --build-id <id> --source other-dataset

Requires registry credentials locally (the active stardag profile) in
addition to Modal credentials — unlike ``build_spawn`` (see
``modal/basic``), which needs only Modal credentials.
"""

import argparse
from uuid import UUID

from stardag_examples.modal.walkthrough.app import app
from stardag_examples.modal.walkthrough.tasks import report_dag


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Trigger the walkthrough DAG on the deployed Modal app."
    )
    parser.add_argument(
        "--source",
        default="demo",
        help="Source to process (each source yields a deterministic DAG).",
    )
    parser.add_argument(
        "--reactive",
        action="store_true",
        help=(
            "Schedule reactively (experimental): no resident build "
            "function; scheduler ticks drive the build."
        ),
    )
    parser.add_argument(
        "--build-id",
        default=None,
        help=(
            "Existing build id to re-attach to. Re-triggers resume the "
            "build; passing a new --source adds its DAG as an extra root."
        ),
    )
    parser.add_argument(
        "--shard-sleep-seconds",
        type=float,
        default=20.0,
        help="Duration of each ProcessShard task.",
    )
    parser.add_argument(
        "--scan-sleep-seconds",
        type=float,
        default=240.0,
        help="Duration of the LongScan task.",
    )
    parser.add_argument(
        "--wait",
        action="store_true",
        help="Block until the spawned Modal function call finishes.",
    )
    args = parser.parse_args()

    root = report_dag(
        args.source,
        shard_sleep_seconds=args.shard_sleep_seconds,
        scan_sleep_seconds=args.scan_sleep_seconds,
    )

    result = app.build_trigger(
        root,
        build_id=UUID(args.build_id) if args.build_id else None,
        description=f"Modal walkthrough ({args.source})",
        # NOTE: in reactive mode a re-trigger must also pass reactive=True.
        reactive=args.reactive,
    )
    print(f"build_id:      {result.build_id}")
    print(f"function call: {result.function_call.object_id}")
    print(f"root task:     {root.id}")
    print("Follow the build in the registry UI (Builds page).")
    if args.wait:
        print("Waiting for the spawned function call to finish...")
        result.function_call.get()
        print(f"Done. Report: {root.load()!r}")


if __name__ == "__main__":
    main()
