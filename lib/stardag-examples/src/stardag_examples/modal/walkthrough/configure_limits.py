"""Configure the named concurrency limit used by the walkthrough.

Named concurrency limits are configured *per environment in the
registry* — the SDK-side ``limit_key_selector`` (see ``app.py``) only
tags tasks with keys; the cap lives server-side and is enforced
atomically when a task starts, across all builds in the environment.

The API is a single endpoint (see the Modal how-to guide):

    PUT /api/v1/concurrency-limits/{key}   {"max_concurrent": N}

Equivalent curl (with an environment-scoped API key):

    curl -X PUT "$STARDAG_API_URL/api/v1/concurrency-limits/walkthrough-shards" \\
        -H "X-API-Key: $STARDAG_API_KEY" \\
        -H "Content-Type: application/json" \\
        -d '{"max_concurrent": 3}'

This script does the same through the SDK's configured registry client
(so it works with your active stardag profile, API key or browser
login). You can also manage limits in the registry UI: workspace admin
-> Concurrency Limits.

Usage:

    python -m stardag_examples.modal.walkthrough.configure_limits            # cap = 3
    python -m stardag_examples.modal.walkthrough.configure_limits --max-concurrent 5
"""

import argparse

import stardag as sd
from stardag.registry import APIRegistry

from stardag_examples.modal.walkthrough.app import SHARD_LIMIT_KEY


def set_limit(key: str, max_concurrent: int) -> None:
    registry = sd.registry_provider.get()
    if not isinstance(registry, APIRegistry):
        raise SystemExit(
            "No registry configured. Run 'stardag auth login' or set "
            "STARDAG_API_KEY / STARDAG_API_URL."
        )
    response = registry.client.put(
        f"{registry.api_url}/api/v1/concurrency-limits/{key}",
        json={"max_concurrent": max_concurrent},
        # Required for browser-login (JWT) auth; ignored with API keys.
        params=(
            {"environment_id": registry.environment_id}
            if registry.environment_id
            else None
        ),
    )
    response.raise_for_status()
    print(f"Configured limit: {response.json()}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Set the walkthrough's named concurrency limit."
    )
    parser.add_argument("--key", default=SHARD_LIMIT_KEY)
    parser.add_argument(
        "--max-concurrent",
        type=int,
        default=3,
        help="Max ProcessShard tasks running at once (across builds).",
    )
    args = parser.parse_args()
    set_limit(args.key, args.max_concurrent)


if __name__ == "__main__":
    main()
