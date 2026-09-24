# API Service

The Stardag API service provides a REST API for task tracking and coordination.

## Overview

The API enables:

- Task registration and status tracking
- Build coordination across workers
- Target root configuration
- Workspace and environment management

## Authentication

### API Keys

For programmatic access (CI/CD, scripts):

```sh
export STARDAG_API_KEY=sk_your_api_key_here
```

Generate keys from the Web UI under Workspace Settings > API Keys.

### OAuth/OIDC

For interactive use (CLI, web):

=== "Active venv"

    ```sh
    stardag auth login
    ```

=== "uv run ..."

    ```sh
    uv run stardag auth login
    ```

Uses browser-based OAuth flow.

## Base URL

| Environment | URL                       |
| ----------- | ------------------------- |
| SaaS        | `https://api.stardag.com` |
| Local dev   | `http://localhost:8000`   |
| Self-hosted | Your configured domain    |

## SDK Integration

The SDK handles API communication automatically, and uses registry based on your [configuration](../configuration/index.md).

Yoy can also pass a registry implementation to the [`build` functions](../concepts/build-execution.md#the-build-functions) explicitly:

```python
from stardag.registry import APIRegistry

registry = APIRegistry()
sd.build(task, registry=registry)
```

## API Endpoints

Two prefixes, for two different sets of entities:

- **`/api/v1`** — auth, users, workspaces, environments, members, invites,
  API keys and target roots. Unchanged by the registry redesign below.
- **`/api/v2`** — the registry itself: tasks, task instances, plans,
  deployments, settings and executions. Everything in this section lives
  under `/api/v2`.

### Health Check

```
GET /health
```

Returns API status.

### Authentication

```
GET  /.well-known/jwks.json     # JWKS for token verification
GET  /api/v1/auth/config        # Auth configuration
POST /api/v1/auth/exchange      # Exchange refresh token for workspace-scoped access token
```

### User

```
GET /api/v1/me                  # Current user profile with workspaces
GET /api/v1/me/invites          # Pending workspace invites
```

### Workspaces

```
POST   /api/v1/workspaces                           # Create workspace
GET    /api/v1/workspaces/{workspace_id}            # Get workspace details
PATCH  /api/v1/workspaces/{workspace_id}            # Update workspace
DELETE /api/v1/workspaces/{workspace_id}            # Delete workspace
GET    /api/v1/workspaces/{workspace_id}/members    # List members
```

### Environments

```
GET    /api/v1/workspaces/{workspace_id}/environments                    # List environments
POST   /api/v1/workspaces/{workspace_id}/environments                    # Create environment
GET    /api/v1/workspaces/{workspace_id}/environments/{environment_id}   # Get environment
PATCH  /api/v1/workspaces/{workspace_id}/environments/{environment_id}   # Update environment
DELETE /api/v1/workspaces/{workspace_id}/environments/{environment_id}   # Delete environment
```

### Target Roots

```
GET /api/v1/target-roots             # Get target roots for current environment
```

### Deployments and settings (`/api/v2`)

The two halves of a build's scope — see [Deployments and code
versions](../concepts/modal-orchestration.md#deployments-and-code-versions)
and [Build & Execution](../concepts/build-execution.md#the-registry-as-a-ledger-over-the-entities-of-a-build).

```
POST /api/v2/deployments                     # Record a deployment (before the Modal deploy)
POST /api/v2/deployments/{id}/activate       # Mark it live (after the deploy succeeds)
GET  /api/v2/deployments                     # List deployments (?app=, ?current=true)
GET  /api/v2/settings/{settings_hash}        # Read a stored settings body
PUT  /api/v2/concurrency-limits/{key}        # Create or replace a named limit
DELETE /api/v2/concurrency-limits/{key}      # Remove a named limit
GET  /api/v2/concurrency-limits              # List named limits
```

### Builds (`/api/v2`)

```
POST   /api/v2/builds                           # Create a build (root_task_ids required)
GET    /api/v2/builds                           # List builds
GET    /api/v2/builds/{build_id}                # Get a build
POST   /api/v2/builds/{build_id}/complete       # Complete (recomputes plan_complete)
POST   /api/v2/builds/{build_id}/fail           # Fail
POST   /api/v2/builds/{build_id}/cancel         # Cancel (releases every plan's claims)
POST   /api/v2/builds/{build_id}/exit-early     # Exit without releasing claims
POST   /api/v2/builds/{build_id}/resume         # Resume under a (deployment, settings) scope
DELETE /api/v2/builds/{build_id}                # Delete (refused while a claim or open execution remains)
GET    /api/v2/builds/{build_id}/frontier       # Runnable / discovery-job / running members
GET    /api/v2/builds/{build_id}/events         # The build's event log
GET    /api/v2/builds/{build_id}/executions     # This build's executions (?not_in_current_plan, ?include_ended)
POST   /api/v2/builds/{build_id}/skip-blocked   # Propagate skip-blocked over the active plan
```

### Plans and members (`/api/v2`)

A plan is one build's request under one scope; a member is one task
instance admitted into it. See [The plan: roots, discovery,
closure](../concepts/build-execution.md#the-plan-roots-discovery-closure).

```
POST /api/v2/builds/{build_id}/plans                              # Create/reuse a plan; register unexpanded roots
GET  /api/v2/plans/{plan_id}/roots                                 # The plan's root instances
POST /api/v2/plans/{plan_id}/members                               # Register a chunk (static or discovery-job result)
POST /api/v2/plans/{plan_id}/seal                                  # Verify and seal (activates a replacement plan)
POST /api/v2/plans/{plan_id}/members/{task_id}/start               # Claiming start
POST /api/v2/plans/{plan_id}/members/{task_id}/complete            # Report completion
POST /api/v2/plans/{plan_id}/members/{task_id}/fail                # Report failure
POST /api/v2/plans/{plan_id}/members/{task_id}/suspend             # Suspend (dynamic dependencies pending)
POST /api/v2/plans/{plan_id}/members/{task_id}/retry               # Reset a failed/cancelled/skipped task
POST /api/v2/plans/{plan_id}/members/{task_id}/interrupt           # Report an interruption (checkpointed)
POST /api/v2/plans/{plan_id}/members/{task_id}/preempt             # Report a platform preemption
POST /api/v2/plans/{plan_id}/members/{task_id}/skip                # Skip (never-started, upstream failed)
POST /api/v2/plans/{plan_id}/members/{task_id}/cancel              # Cancel one task (the claim holder only)
POST /api/v2/plans/{plan_id}/members/{task_id}/yield               # One yield batch: children + closure
POST /api/v2/plans/{plan_id}/members/{task_id}/exclude             # Operator: give up on this member
POST /api/v2/plans/{plan_id}/members/{task_id}/discovery-failed    # Discovery excludes itself (class import / requires())
POST /api/v2/plans/{plan_id}/members/{task_id}/artifacts           # Upload artifacts
POST /api/v2/tasks/{task_id}/claim/renew                           # Renew an in-process claim's TTL
```

### Tasks (`/api/v2`)

```
GET /api/v2/tasks/{task_id}              # The task: status, claim, current execution
GET /api/v2/tasks/{task_id}/artifacts    # This task's artifacts
GET /api/v2/tasks/{task_id}/events       # This task's event log
```

### Executions and wake-ups (`/api/v2`)

```
POST   /api/v2/executions/{execution_id}/stopped        # Record a stop; releases a still-held claim
POST   /api/v2/builds/wake-candidates                    # Flagged builds with no live scheduler lease
POST   /api/v2/builds/{build_id}/notify                  # Flag a build for a wake-up
GET    /api/v2/builds/{build_id}/notify                  # Read the wake flag
DELETE /api/v2/builds/{build_id}/notify                  # Clear the wake flag
POST   /api/v2/builds/{build_id}/scheduler-lease         # Acquire the scheduler lease
PUT    /api/v2/builds/{build_id}/scheduler-lease         # Renew it
DELETE /api/v2/builds/{build_id}/scheduler-lease         # Release it
PUT    /api/v2/builds/{build_id}/reactive-meta           # Set the owning app + tick config
POST   /api/v2/builds/{build_id}/tick-summaries          # Record a tick's outcome
GET    /api/v2/builds/{build_id}/tick-summaries          # List recent ticks
```

## Refusal codes

A write route that cannot apply refuses with a 4xx and a `detail` naming
one of these reasons — never a silent no-op and never a 500 for an
ordinary race:

| Code                             | Status | Meaning                                                                         |
| -------------------------------- | ------ | ------------------------------------------------------------------------------- |
| `task_identity_conflict`         | 409    | An existing task row disagrees on namespace/name/version/output_uri             |
| `instance_body_conflict`         | 409    | The scope already has this `instance_hash` with a different body                |
| `instance_conflict`              | 409    | The plan already holds a different instance of this task id                     |
| `root_instance_conflict`         | 409    | A re-trigger's roots differ from the build's recorded ones                      |
| `root_mismatch`                  | 400    | Plan roots do not match the build's `root_task_ids`                             |
| `duplicate_item`                 | 400    | One instance appears twice in a chunk with different items                      |
| `unknown_upstream_instance`      | 400    | A declared upstream is not a registered instance in this scope                  |
| `unknown_yielded_instance`       | 400    | A `yielded` hash was not one of the batch's own items                           |
| `plan_sealed`                    | 409    | A non-idempotent write against an already-sealed plan                           |
| `plan_incomplete_registration`   | 409    | `/seal` found unexpanded or missing members                                     |
| `plan_incomplete`                | 409    | `/complete` found the plan unsealed, incomplete or an excluded root             |
| `plan_superseded`                | 409    | The plan is not the build's active one                                          |
| `root_excluded`                  | 409    | An excluded root's build cannot be completed                                    |
| `member_excluded`                | 409    | A claiming start named an excluded member                                       |
| `not_expanded`                   | 409    | A claiming start named a member with no known upstreams yet                     |
| `upstream_incomplete`            | 409    | Re-checked at claim time: an upstream is not COMPLETED                          |
| `task_not_actionable`            | 409    | The task's status is not one a claim may be taken from                          |
| `task_already_completed`         | 409    | An observation raced a claiming start; the task is already COMPLETED            |
| `task_not_skippable`             | 409    | `skip` against a result (COMPLETED, FAILED, CANCELLED) it must not overwrite    |
| `not_claim_holder`               | 409    | A report or single-task action came through a plan that is not the claim holder |
| `build_not_running`              | 409    | A claiming start against a build that is not RUNNING                            |
| `deployment_mismatch`            | 409    | A worker's `STARDAG_DEPLOYMENT_ID` differs from its plan's                      |
| `deployment_activation_conflict` | 409    | `/activate` disagrees with a value already recorded                             |
| `local_deployment_conflict`      | 409    | A `local` lookup names another app for a recorded code id                       |
| `unknown_limit`                  | 404    | A concurrency limit key does not exist                                          |
| `concurrency_limit_reached`      | 409    | A named limit is full at claim time                                             |
| `reserved_settings_key`          | 400    | A `settings` key starts `STARDAG_` or `MODAL_`                                  |
| `clock_skew`                     | 400    | An `observed_at` is ahead of the server's clock by too much                     |
| `rate_limited`                   | 429    | Per-workspace write rate limit (`Retry-After` header)                           |
| `creation_quota_exceeded`        | 429    | The 24-hour `task_instance` creation quota for the environment                  |

## Error Handling

API errors return JSON with:

```json
{
  "detail": "Error description"
}
```

Common status codes:

| Code | Meaning                           |
| ---- | --------------------------------- |
| 401  | Invalid or expired authentication |
| 403  | Insufficient permissions          |
| 404  | Resource not found                |
| 409  | Conflict — see the table above    |
| 422  | Validation error                  |
| 429  | Rate or quota limited             |

## See Also

- [Using the API Registry](../how-to/use-api-registry.md) - SDK integration guide
- [Self-Hosting](self-hosting.md) - Run your own API server
