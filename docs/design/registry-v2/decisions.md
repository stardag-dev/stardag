# Registry v2: decisions log

Each decision is recorded with its recommendation and the runner-up it
displaced; flipping one is a local edit to the design, not a re-plan.
Dated entries are added as the line evolves.

## Decisions D1–D13 (2026-09-23)

| #   | Decision                                        | Recommendation                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                     | Runner-up                                                                                                                                       |
| --- | ----------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | ----------------------------------------------------------------------------------------------------------------------------------------------- |
| D1  | Name of the completion-keyed table              | **`task`**, keyed by `task_id` (the completion hash keeps its user-facing name and format). The row is the task's global fact record: identity-by-promise, status, claim, execution pointer. "Claim" is one of its states, so `task_claim` over-names it; `task_outcome` names something the row does not store.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                   | `task_claim` (the maintainer's draft)                                                                                                           |
| D2  | Name of the per-scope body table                | **`task_instance`**; "deterministic" is carried by the scope columns, not the name (the row does not make anything deterministic; the scope's contract does). Hash column `instance_hash`. Settled 2026-09-23 with two rules instead of a longer name: the vocabulary rule (an _instance_ is a registry row under a scope; the Python object is a _task object_; repeated in code docs at the critical places) and the API rule (`instance_hash` is never a public identifier on its own; instances are addressed by row id or the full scope triple).                                                                                                                                                                                                                                                                                                                                                                                                             | `deterministic_task_instance`, `scoped_task_instance`                                                                                           |
| D3  | Second identity naming                          | `task_id` (completion) and `instance_hash` (all parameters). Field flag is `StardagField(significant: bool = True)`. Not "completion_hash"/"instance_hash" pair: renaming `task_id` renames a user-visible concept (target paths, CLI, UI) for no user gain.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                       | `task_completion_hash` / `task_instance_hash`                                                                                                   |
| D4  | Per-trigger config name                         | **`settings`** everywhere: `build_trigger(settings=)`, `sd.build(settings=)`, CLI `--settings KEY=VALUE`, table `settings`, column `settings_hash`, scope `(deployment_id, settings_hash)` (settled 2026-09-23; the draft said `exec_config`). A flat `dict[str, str]` applied as env vars in every process of the build; the intended consumer is the pydantic-settings pattern. Not `env_overrides` (the worker selector's per-task env, an app-deploy setting, stays), not `build_config` (v1's deleted mechanism; it also belongs to no build), not `build_settings` (same objection). Precedence settings > selector env > deployment env; `STARDAG_*` and `MODAL_*` keys refused at the trigger. Contract: may change structure and execution, **never output**.                                                                                                                                                                                             | `exec_config` (the draft), `build_env`                                                                                                          |
| D5  | `execution` ledger table                        | **Add it**, one row per claim granted. Two ends, written by two hands: `claim_released_at`/`claim_outcome` by the server when the claim moves (taken over, lapsed-at-takeover, released by cancel/fail); `ended_at`/`outcome` only by the execution's own report or an operator stop. It is a record, not coordination: the claim stays on `task`; a row with `ended_at IS NULL` may still be a running container, which is exactly what `builds stop` wants to list. Needed by: orphan definition (STA-67), attempts-as-rows (D9), the stop list, STA-65's per-execution end, STA-94, the UI executions view. STA-78 dropped it when it was going to _arbitrate_; here it only records.                                                                                                                                                                                                                                                                           | keep `task.execution_id` only and define orphans over plan membership                                                                           |
| D6  | Modal deployment identity                       | **One row per deploy**, id minted by `stardag modal deploy` (uuid7) and baked into the image as `STARDAG_DEPLOYMENT_ID`; created **before** the deploy (the server assigns a monotonic `generation` per app) and activated after it succeeds, so "current" is the activated row with the highest generation and a late record cannot roll a build back (amended 2026-09-23 after the first Copilot round). Redeploy of unchanged code = new deployment = new scope (the maintainer's stated position). Local builds: lookup-or-create by `(environment, kind='local', code_id)` with `code_id` from `STARDAG_CODE_ID` → clean git SHA → fresh uuid, so the existing `STARDAG_CODE_ID` is the pin.                                                                                                                                                                                                                                                                  | dedupe Modal deploys by `(app_name, code_id)` too, so a no-code-change redeploy shares scope (sound under the env-var contract, fewer re-plans) |
| D7  | Completion invalidation                         | **The registry follows the world, and only that** (narrowed 2026-09-23): the sole path out of COMPLETED is discovery's `observed_complete: false` with the `observed_at` guard, recorded as `TASK_INVALIDATED{target_missing}`. No operator route declares a task incomplete; an operator acts on the target and triggers a build (`stardag tasks check` reports the observation). The record stays truthful because the promise is unchanged — the same output is re-produced. "Fix its output" is a change of promise and needs a new `task_id` (version bump or new significant parameter), which cascades to downstream ids on its own; a cascading uncomplete in the registry is neither feasible (no target access, compound completion states) nor sound (other builds rely on global completion).                                                                                                                                                          | the draft's operator `POST /tasks/{id}/invalidate`; or keep COMPLETED sticky forever                                                            |
| D8  | Static-phase atomicity                          | **Chunked registration in post-order + `plan.sealed_at`.** Each chunk is self-consistent (an instance lands with its declared edges; upstreams are in the same or an earlier chunk); the frontier may run on an unsealed plan; build completion requires `sealed_at`. Not one giant transaction: plans reach thousands of tasks.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                   | one request per plan                                                                                                                            |
| D9  | Per-(plan, task) state                          | **No counters.** Attempts and interruptions per build are counted from `execution` rows over the build's plans (the ledger is the one source); `plan_member` carries only membership facts and `excluded_at`. Replaces the three per-build event replays in `services/status.py`. (Flipped by the review: per-plan counters diverge from the global claim and restart at every re-plan, STA-44's "budget silently grew".)                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                          | per-plan counters                                                                                                                               |
| D10 | STA-78 stabilisation window vs v2               | **v2 is the bet and has the highest priority** (Anders, 2026-09-23). It is the explicit exception to STA-78's decision 3; STA-78 continues at reduced priority, and small v1 fixes may still land under it when chosen (see the "Continue on v1 unchanged" list in [plan.md](plan.md)'s Adjacent issues section).                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                  | pause v2 until STA-78 done-when                                                                                                                 |
| D11 | Claims in the resident engine                   | **Every execution claims**, uniformly, with a TTL: detached executors get theirs from the start request; in-process (thread/process pool) executions get a short one that the resident driver **renews** (`…/claim/renew`), exactly as v1's resident engine renewed the distributed lock, so a dead resident process lapses like any other worker and no reaper or NULL-expiry special case is needed (amended 2026-09-23 after the first Copilot round; the draft held in-process claims with no expiry). The resident engine yields with `suspend: false` (the parent stays claimed while its in-process generator is alive). `distributed_lock` is **retired**: the claim is the mutual exclusion. v1 claimed only when `supports_detached`, leaving pool executions on the deprecated global lock. Applies only when a build has a registry: a single-process `sd.build()` without one has no claims, no plan and no instance rows, and stays fully supported. | keep the lock table for in-process runs                                                                                                         |
| D12 | `compat_default` and the two hashes             | **`compat_default` kept**, on significant fields only (a value equal to it is dropped from `task_id`, so a field can be added without re-keying every downstream — the #340 cascade), together with custom `"hash"`-mode serializers: user control over hashing stays on `task_id`. Only `significance=` and `hash_exclude=` are removed. The **`instance_hash` is the hash of the canonical body itself** — no hash mode, no user control beyond ordinary pydantic serialization — and the SDK enforces the one thing it needs, round-trip stability (`dump(validate(dump(x))) == dump(x)`), at registration with `UnstableSerializationError`. Settled 2026-09-23.                                                                                                                                                                                                                                                                                               | remove `compat_default`; or a user-customisable instance hash                                                                                   |
| D13 | Which deployment a non-Modal driver plans under | A driver whose tasks run on a Modal app (hybrid `sd.build()` with `ModalTaskExecutor`, or `reactive_discovery="local"`) plans under **the app's current deployment**; the laptop code matching it is on the user, as today's clean-tree sharing was. A pure local build plans under a `local` deployment. `/yield` refuses a worker whose `STARDAG_DEPLOYMENT_ID` differs from the plan's (409 `deployment_mismatch`). Principle (Anders, 2026-09-23): where nothing can be guaranteed, take the simplest mechanism and put the responsibility on the user, stated in the docs.                                                                                                                                                                                                                                                                                                                                                                                    | plan under a local deployment and roll over at the first deployed tick (discovers everything twice)                                             |

## Review dispositions (2026-09-23)

Thirty findings; the full findings are in
[research/review-2026-09-23.md](research/review-2026-09-23.md). Accepted
and folded in (the design above is post-review): settings must never
affect output (F1); closure stays as a frontier-time mechanism because
instances are shared across plans (F2, F23-static); lapsed claims are
ACTIONABLE (F3); `output_uri` is checked per instance (F4); invalidation
and observed completion land in the chunk transaction (F5, F29); roots
first and a verifying `/seal` (F6); exclusion cascades, excluded root fails
the build (F7); uniform claims, resident `suspend: false`, lock table
retired (F8 → D11); `/seal` re-checks the current deployment (F9); the
ledger's two ends split (F10); idempotency rules for reports, claims,
invalidate and chunk events (F11); `FOR NO KEY UPDATE`, sorted chunk
inserts, build status not flipped in task transactions (F12);
`activated_at` (F13); build delete guard and `event.build_id SET NULL`
(F14, F15); counters dropped (F16 → D9); limit keys at claim time (F17);
composite FKs, executor fields only on `execution`, `deployment`
RESTRICT, quota on instances (F18); root instance conflict and body
conflict are 409s (F19); resume re-observes targets and retries (F20);
`/yield` checks the worker's deployment and hybrid drivers plan under the
app's deployment (F21 → D13); `compat_default` kept (F22 → D12);
`exit-early` releases nothing (F23); dynamic `instance_conflict` is
non-retryable (F24); eighteen scenarios added (F25); request shapes and
hashing rules stated (F26, F28); `settings` reserved keys, scoping,
empty hash (F27); `expanded_at`, `plan_member`, `taken_over` (F30).

Rejected, with reason: renaming `settings` to `build_env` ("env"
collides with the `environment` entity; recorded as D4's runner-up);
`completion_id` in the API (the user-facing name stays `task_id`);
splitting `superseded` further (the plan timestamp and the tick exit are
the same fact seen from two sides; only the execution outcome was
renamed).

## Maintainer review, round 1 (2026-09-23) — all thirteen settled

Anders went through D1–D13 on 2026-09-23. Settled as recommended: D1, D3,
D5, D6, D8, D9. Amended in place above: D2 (two rules instead of a longer
name), D4 (`exec_config` → `settings`), D7 (narrowed to the observed path,
no operator route), D10 (v2 is the bet), D11 (registry-free builds stay
supported), D12 (the instance hash is the hash of the body; round-trip
stability enforced), D13 (simplest mechanism, responsibility on the user).
One rule confirmed along the way: a within-scope divergence of an expanded
instance's declared upstreams can only come from user code breaking the
env-var or snapshot contract, and it is appended and recorded
(`TASK_STRUCTURE_DIVERGED`), never refused.

## Copilot review, round 1 (2026-09-23) — dispositions

Seventeen threads on PR #379. Accepted and folded into the design:
deployment `generation` assigned at create, activation after deploy (D6
amended); resident claims renew a TTL instead of holding a NULL expiry (D11
amended); scope columns with composite FKs on `plan_member`; composite FKs
on `execution`; build deletion also refused while an execution is unended;
`observed_at` on items so a delayed duplicate observation cannot undo a
later completion; seal accepts a COMPLETED unexpanded root; the frontier
closure admits COMPLETED upstreams too, so seal's closure check holds;
`batch_id` on `/yield` so a retry after a lost response is replayed rather
than refused; `/complete` recomputes `plan_complete` in its own transaction;
`event.plan_id` and `event.execution_id` are `SET NULL`; the empty
`settings` is created lazily (the "always present" wording was the
contradiction); `plan.md` corrected on the members route, on
`compat_default` (kept, D12) and on where `reactive_discovery="local"` plans
(the app's deployment, D13).

Rejected: refusing a re-registration of an expanded instance whose static
upstream set differs. Within a scope edges only grow and gating can only
over-approximate; that is the soundness argument the design rests on, and a
409 there would fail builds on a benign env-var contract breach. The design
appends the edges and records `TASK_STRUCTURE_DIVERGED`, as v1 warned.

## Copilot review, round 2 (2026-09-23) — dispositions

Eight threads. Accepted: `local` deployments are created already activated;
the composite FK `(environment_id, settings_hash)` on `task_instance` and
`plan`, with the scope columns NOT NULL; `UNIQUE (id, deployment_id,
settings_hash)` on `plan` as the target of `plan_member`'s composite FK; a
per-build `plan.generation` so `/seal` activates only the latest request
(two replacements with different settings can no longer leave the build on
the older one); late reports defined against `claim_released_at` and stated
to write the ledger end without touching task status; the D4 rendering and
the S25 reaper wording fixed (both already superseded by the settled
decisions).

Accepted in bounded form: the `observed_at` guard compares a driver clock
with a server clock. The alternative — a registry status version captured
before the target check — needs a registry read per task inside discovery,
which is exactly the coupling discovery avoids. Instead the server refuses an
`observed_at` ahead of its own clock by more than a few seconds, the only
skew direction that can pass the check wrongly; backward skew only skips an
observation. The residual worst case is a spurious re-run that re-produces
the same output under the task-id contract, never wrong output, and it is
recorded as an intentional limitation.

## Copilot review, rounds 3–5 (2026-09-23/24) — dispositions

Twenty-one threads over three passes on successive commits; five were
already fixed by the time they were read (nested body, local activation,
settings FK, plan unique key, the D11 table) and two were stale wording
(`FOR UPDATE` in the carry-over bullet, I4 in plan.md). Accepted from the
fourteen distinct points: `claim_expires_at` NOT NULL whenever RUNNING, so
liveness is a finite expiry and nothing is live forever; composite FKs tying
`task.claim_plan_id` to the membership and `task.execution_id` to an
execution of the same task; one schema rule that every FK between
environment-scoped tables carries `environment_id`; identity metadata
(namespace, name, version, output*uri) compared on registration, 409
`task_identity_conflict`; `/complete` reads the members' task rows `FOR
SHARE` so it serialises with a concurrent invalidation, and `force` never
overrides a missing seal; the claiming start re-checks upstream completion
under its own lock (409 `upstream_incomplete`) — the frontier is a hint, the
claim is the decision (S39); renewal names its execution and is granted only
to the live holder; `claim_outcome` gains `interrupted` and `lapsed`, and
every move off RUNNING closes the current claim on the ledger, the observed
completion of a lapsed-claim task included; a failed discovery job excludes
the member (`discovery_failed`) instead of failing the global task, so it is
never re-selected; the task-id rule states that all `significant=False`
fields are excluded; framework-owned `STARDAG*\*` identifiers are written
last and cannot be overridden by settings, selector or deployment env.
Nothing rejected in these rounds.

## Methodology (2026-09-24)

Agreed with the maintainer and recorded in `plan.md`: a vertical spike (I0)
before the surfaces; a must-still-hold list of registry-live scenarios whose
assertions survive v2, canonical `api-pg` tests for the registration and
transition invariants written before the service, and a test tier per
scenario in `design.md`; seven engineering rules traceable to v1 defects;
and two standing mechanisms that tell reviewers, Copilot included, what a
PR against `v2` deliberately leaves out (`.github/copilot-instructions.md`
and the `v2.md` PR template).

## Copilot review, round 6 (2026-09-24) — dispositions

Seven threads on 20feadf2. Accepted: NOT NULL stated on every composite-FK
column (a composite FK skips rows with a NULL referencing column); the FK
from the claim pointer written in the membership key's column order, and the
execution's FKs naming their exact target keys; lifecycle transitions
(`complete`, `fail`, `cancel`, `exit-early`, `resume`, `/activate`, `/seal`)
idempotent by state — a re-delivery finds the state and writes nothing; the
invalidation guard names `task.completed_at` explicitly; the yield's
`batch_id` becomes a typed `event` column with a unique index, looked up
under the parent's row lock (the JSON-metadata version violated engineering
rule 4); a tick finishing another driver's unsealed plan re-observes
unexpanded members' targets before sealing; `force` never overrides an
excluded root. On the FK column order: PostgreSQL matches referenced columns
to a unique constraint as a set, so the original text was creatable, but
writing it in key order costs nothing and removes the doubt.

## Implementation notes, I0 step 1 (2026-09-24)

Corrections the schema work made to `design.md`, none of them a change of
decision; the design now says what the migration does.

- `event.build_id`: no CHECK tying it to the event type. `build_id`,
  `plan_id` and `execution_id` are nullable pointers,
  `ON DELETE SET NULL (col)` and `DEFERRABLE INITIALLY DEFERRED`: a build
  delete reaches one event row along three paths, and an immediate check fails on an
  execution already deleted whose own SET NULL has not run yet. The one
  CHECK kept is that a build-level event carries no `plan_id`.
- "Every composite-FK column NOT NULL" becomes "every composite-FK _scope_
  column NOT NULL": the pointer columns on `task` and `event` are nullable,
  and Postgres skipping the check when they are NULL is the intended "no
  claim".
- PostgreSQL 15 or newer is a requirement, for the column-list
  `SET NULL (col)` form.
- `excluded_reason` is `operator | discovery_failed | upstream_excluded`.
- `TASK_WAITING_FOR_LOCK` is removed with the lock table.
- A `local` deployment's `app_name` is `"local"` unless the driver names an
  app.

## Implementation notes, I0 step 3a (2026-09-24)

Two decisions taken by the coordinator, and the readings the build
lifecycle and wake-ups needed. The design now says what the code does.

- **Authority keys on the execution, not the clock.** A report is applied
  when it names `task.execution_id` and that execution's claim has not
  been released, whether or not it has lapsed; only a released claim
  (taken over, closed by an observation, released by a build) makes a
  report late. Step 2 read "and the claim is live" strictly and refused a
  worker that finished seconds after expiry with `execution_not_current`,
  discarding a real completion whose target exists: the next observation
  would have marked it COMPLETED anyway, but only after a tick re-claimed
  and possibly re-ran it. A lapsed claim names its execution until a
  claiming start takes it over, so nothing else can be current.
- **`/seal` runs the closure step first.** Edges belong to the instance,
  which scope-mate plans share; another plan's expansion can add an edge
  from one of this plan's members to an instance it does not hold. Step 2's
  seal refused that with `closure_open`, making a correct seal fail on
  another plan's timing. The closure is the mechanism for exactly this, so
  the seal runs it and then verifies; a conflict it finds fails the build.
- From step 2's readings, now stated: registration locks `task` only when
  an observation changes a status; a claiming start refuses an unexpanded
  member (`upstream_incomplete`, `not_expanded`) and an excluded one
  (`member_excluded`); `task_identity_conflict` covers `output_uri`;
  `TASK_STRUCTURE_DIVERGED` only when new static edges land; invalidation
  clears `task.completed_at`; the default claim TTL is 3600 s, capped at
  24 h.
- **`limit_keys` move to the claiming start** (coordinator): the tick
  computes them from the instance body and sends them with the claim. The
  consequence the design did not spell out: a task that has never held a
  claim has no `task_limit_key` rows, so "builds with members queued on the
  same keys" found nothing for it. The reading taken: a claim refused with
  `concurrency_limit_reached` writes the keys it asked for (the refusal is
  recorded, like a late report). It occupies no slot — its claim is not
  live — and makes the queued task findable when a slot frees. To confirm.
- **A build release sets the task CANCELLED**, for `complete` and `fail` as
  well as `cancel`: in every case the build stopped wanting the task, and
  "revocation is not a result" (v1 rule 36) makes CANCELLED actionable for
  every other build holding it. `ended_at` stays NULL; the worker's report
  is late from then on, and a vanished-then-produced target is picked up by
  the next observation.
- The `plan_incomplete` refusal carries a `reason` (`not_sealed`,
  `root_excluded`, `members_incomplete`); `root_task_ids` is required at
  `POST /builds` and checked at `create_plan` (400 `root_mismatch`).

## Implementation notes, I7 (2026-09-24)

- **A process applying settings serves one build at a time** (coordinator,
  from Copilot on PR #384). Settings are process-global environment
  variables by design (D4), and a tick awaits inside the block that applies
  them; the deployed tick was packed ten inputs to a container, so two
  builds could interleave and read each other's values, or lose their own
  to the other's restore. Rather than replace environment variables with a
  task-local context (which the pydantic-settings pattern cannot read), the
  deployed tick and every worker function run one input per container and
  scale by containers; a declared `max_concurrent_inputs` above one on them
  is refused at deploy. `settings_applied` additionally holds a
  process-level owner token (the build id) and raises `SettingsError` when
  another build's settings are installed, as does the worker wrapper for
  the build named in its `env_overrides`; equal or empty settings do not
  make an overlap safe, so the guard keys on the build, not the values.
  The cost is more containers — a lingering tick now holds one of its own
  — accepted.

## Implementation notes, I0 step 3b (2026-09-24)

Six rulings by the coordinator, then the readings `/yield`, the remaining
transitions, exclusion and `builds stop` needed. `design.md` now says what
the code does; the readings marked "to confirm" are open.

Rulings:

- **Local deployments are never current and never superseded.** The seal's
  current-deployment check, resume's reactivation check and rollover apply
  to `kind = modal` only; a `local` deployment is authoritative for its own
  plans, and `GET /deployments` marks no local row current. A local driver
  at a new commit plans under a new scope; the old commit's plans stay
  usable rather than becoming unsealable.
- **A refused claim records the limit keys it asked for** (step 3a's
  reading, confirmed): the refusal holds no slot — its claim is not live —
  and makes the queued task findable when a slot on those keys frees.
- **A non-claiming start follows the authority rule of every report**:
  applied while its `execution_id` is the task's current execution, lapsed
  or not; late only after a takeover (or an observation or a build release
  closed the claim). Step 2 required a live claim.
- **Reactivating a superseded plan on resume respects the latest-request
  rule** (409 `plan_superseded`). Read literally — "no plan of a higher
  generation exists" — the rule refuses every reactivation, since a
  superseded plan was always superseded by a later generation, and S14's
  reactivation could never happen. The reading taken: the rule refuses
  while a _later request is still registering_ (a higher-generation plan
  never activated), which is the race the seal's rule exists for; moving
  between requests the build already activated is what resume is for. To
  confirm.
- **Every status timestamp is stamped after the row lock** in
  `transition_task()`: the caller's clock orders the transaction's events,
  the task and ledger columns get the time the transition took effect. A
  completion stamped before its lock wait could precede an observation
  made during the wait, and the `observed_at` guard would then let a
  stale "missing" undo a real completion.
- **Terminal build states keep "last event wins"** (a repeat of the same
  state is a no-op): a documented v1 carry-over, not a new rule.
- **A report comes through the plan holding the claim** (carried from the
  step-3 base): the current execution's report under a plan other than
  `task.claim_plan_id` is 409 `not_claim_holder` with no trace, before any
  ledger end. Step 3b applies it to `interrupt`, `preempt` and `/yield` as
  well (a yield's replay lookup still comes first, so a retried batch is
  replayed whatever its route).

Readings:

- **An `instance_conflict` at `/yield` is applied by the server**: the
  batch's items roll back to a savepoint, the parent gets `TASK_FAILED`
  with the conflict named (its claim released `failed`, its execution
  ended), and the batch is refused 409 `instance_conflict`. Any other
  registration refusal rolls the whole batch back and leaves the worker to
  report `TASK_FAILED` (S13).
- **A refused batch is recorded without a typed `batch_id`** (the id is in
  its metadata): the replay lookup keys on the typed column, so a refusal
  can never be replayed as an applied batch, and the unique index is not
  spent on it. `yielded` must be a subset of the batch's items (400
  `unknown_yielded_instance`); the yielded instances are admitted
  `dynamic`, their closure `static`.
- **A preemption is not an end of the execution.** The platform restarts
  the same execution under the same id, so `/preempt` writes no `ended_at`
  (an end would refuse the restart's reports as `execution_already_ended`);
  it sets `preempted_at` and pulls the claim's expiry to a 900 s grace,
  never back. The restart's non-claiming start re-grants the TTL and clears
  `preempted_at` ("a restart is outstanding" is `preempted_at IS NOT
NULL`, where v1 compared it with the status time). The `preempted`
  execution outcome is therefore unused. To confirm.
- **`skip` is a scheduling decision, not a report.** The design lists it
  among the reports naming an `execution_id`, but v1's SDK uses it for a
  never-started task whose upstream failed, which has no execution. It
  names none; it moves PENDING, SUSPENDED or INTERRUPTED (or a lapsed
  claim) to SKIPPED, is refused against a live claim, COMPLETED, and FAILED
  / CANCELLED (409 `task_not_skippable`: results a skip must not
  overwrite), and is idempotent. Skip-blocked applies it. To confirm.
- **A single task's cancel** is for the build holding the claim, through
  any of its plans (409 `not_claim_holder`, also when nobody holds it); the
  execution's `ended_at` is untouched (cooperative).
- **The exclusion cascade stops at COMPLETED.** A downstream is excluded
  (`upstream_excluded`) when it is not COMPLETED and has an excluded,
  not-COMPLETED upstream: a COMPLETED member blocks nobody, so excluding a
  COMPLETED member cascades nothing, and a COMPLETED root reached by the
  walk is not excluded (the build is not failed for a request already met).
  Exclusion is refused on a superseded plan (`plan_superseded`) and is
  idempotent by state; an excluded root fails the build (`root_excluded`)
  in the same transaction.
- **`/executions/{id}/stopped` releases a claim the execution still
  holds.** Nothing will ever report for a stopped execution, so leaving its
  claim would hold the task until the claim lapsed (up to 24 h); the stop
  writes the ledger end (`stopped`) and, if the execution is current and
  unreleased, releases the claim `cancelled` with the task CANCELLED
  (ACTIONABLE). To confirm.
- **The guardrails at the v2 boundary**: v1's per-workspace rate limit is a
  router-level dependency of the v2 router (every write route, reads not
  limited, 429 `rate_limited` with `Retry-After`); the 24-hour quota is per
  environment on `task_instance` rows
  (`LIMITS_MAX_TASK_INSTANCES_PER_ENVIRONMENT_24H`), charged in
  `register_items` after the insert for the rows it returned, so a
  re-delivered chunk is never refused (429 `creation_quota_exceeded`). The
  count scans `task_instance` by `(environment_id, created_at)`, which has
  no index yet; it runs only when a chunk inserted rows and the quota is
  configured.
