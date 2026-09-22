# Development Guide

## Project Structure

```
lib/
├── stardag/           # Core SDK library
└── stardag-examples/  # Example DAGs and demos

app/
├── stardag-api/       # FastAPI backend for task tracking
└── stardag-ui/        # React frontend for monitoring
```

## Quick Start

### Install all packages

```bash
./scripts/install.sh
```

Or manually:

```bash
# Install each Python package (creates separate .venv per package)
cd lib/stardag && uv sync --all-extras && cd ../..
cd lib/stardag-examples && uv sync --all-extras && cd ../..
cd app/stardag-api && uv sync --all-extras && cd ../..

# Install frontend
cd app/stardag-ui && npm install && cd ../..

# Install root workspace (for dev)
uv sync --all-extras
```

### Run all tests

```bash
./scripts/test.sh
```

Or via tox:

```bash
tox -e stardag-py311,stardag-examples-py311,stardag-api-py311,stardag-ui
```

## Running the Full Stack

```bash
docker compose up -d
```

This starts:

- PostgreSQL database on port 5432
- API service on port 8000
- Web UI on port 3000

Then run a DAG with API registry:

```bash
export STARDAG_API_REGISTRY_URL=http://localhost:8000
python -m stardag_examples.api_registry_demo
```

View tasks at http://localhost:3000

## Development Commands

### Testing

```bash
# Test specific package
tox -e stardag-py311
tox -e stardag-examples-py311
tox -e stardag-api-py311
tox -e stardag-ui

# Run all Python tests
tox -e stardag-py311,stardag-examples-py311,stardag-api-py311
```

#### Live Modal tests

Modal integration tests come in two tiers. The unit tier (default) uses fakes
and needs no credentials. Modules marked `modal_live` hit a real Modal
workspace (deploy test apps, create volumes, run containers):

```bash
cd lib/stardag

# Unit tier only
uv run pytest tests/test_integration/test_modal -m "not modal_live"

# Live tier — requires Modal credentials; use a personal/dev profile!
STARDAG_MODAL_TEST_PROFILE=<your-dev-profile> \
  uv run pytest tests/test_integration/test_modal -m modal_live
```

Or through tox, which is what CI runs and which defaults to require mode:

```bash
STARDAG_MODAL_TEST_PROFILE=<your-dev-profile> tox -e stardag-modal-live
```

Gating (see `stardag.testing.modal.live_modal_guard`):

- `STARDAG_MODAL_LIVE_TESTS`: `auto` (default: run if authenticated, else
  skip), `1` (require: fail instead of skip), `0` (always skip). The
  `stardag-modal-live` tox env sets `1`, because invoking it _is_ the request
  to run the tier — `auto` would let a missing credential skip everything and
  still exit 0.
- `STARDAG_MODAL_TEST_PROFILE`: if set, live tests are skipped unless the
  active Modal profile matches. Convenient locally, where credentials come
  from a `~/.modal.toml` profile and the name therefore means something.
- `STARDAG_MODAL_TEST_WORKSPACE`: if set, the workspace the credentials
  **actually belong to** must match — resolved from the token itself. Prefer
  this wherever credentials come from the environment rather than a profile,
  CI most obviously. `MODAL_PROFILE` selects a section of `~/.modal.toml`, but
  `MODAL_TOKEN_ID`/`MODAL_TOKEN_SECRET` take precedence over that file and are
  not bound to the profile name, so a profile name asserts nothing there.

Set one of the two whenever you run the live tier, so it can never reach a
shared or production-adjacent workspace by accident.

The ordinary test envs exclude the tier twice over — `-m "not modal_live"`
plus `STARDAG_MODAL_LIVE_TESTS=0`. Both are needed: marker deselection happens
_after_ module import, so without the environment variable the guard still
makes a Modal API call per module before deciding to skip.

##### In CI

`.github/workflows/modal-live.yml` runs the tier against the `andhus` Modal
workspace, asserted via `STARDAG_MODAL_TEST_WORKSPACE`. It is **not** part of
the normal CI run and is
not a required check — it needs credentials, which GitHub does not give to
pull requests from forks.

| When                                                                                                              | Modal environment    |
| ----------------------------------------------------------------------------------------------------------------- | -------------------- |
| A pull request from a branch on this repo, that either touches the tier's paths or carries the `modal-live` label | `ci-pr-<number>`     |
| Manual `workflow_dispatch`                                                                                        | `ci-manual-<run-id>` |
| The weekly schedule                                                                                               | `ci-main`            |

**You do not normally need to do anything.** A pull request touching any of
these runs the tier automatically:

- `lib/stardag/src/stardag/integration/modal/`
- `lib/stardag/src/stardag/build/`
- `lib/stardag/src/stardag/testing/modal/`
- `lib/stardag/tests/test_integration/test_modal/`
- `lib/stardag/pyproject.toml` (the dependency list baked into the worker image)
- `tox.ini`, `.github/workflows/modal-live.yml` (the harness itself)

**The `modal-live` label is the manual override**, for a change that touches
none of those and still warrants a live run — a dependency bump, a `selfhost`
change, a hunch. The `Decide what to run` job logs which rule applied and
why, so a surprising skip is one click to explain.

The schedule is weekly rather than nightly, and it is not there to catch
regressions in merged code — the automatic trigger does that. It is there for
the one signal no commit produces: drift underneath us. `test_live_semantics.py`
pins Modal _platform_ behaviour, and rebuilt images re-resolve dependencies;
both change with time rather than with commits.

Each run gets its own Modal environment, and deleting that environment removes
the apps, volumes and dicts inside it — so the tier can leave its fixed-name
objects (`stardag-testing`, `stardag-testing-app`, ...) exactly where they are
without concurrent runs colliding. A concurrency group serialises runs sharing
an environment name, which is what makes those fixed names safe.

**What the tier does and does not cover.** It exercises Modal _execution_:
real containers, detached spawn and re-attach, retries, cancellation, timeout
semantics. It does _not_ exercise registry interaction — the registries in
these tests are `NoOpRegistry` subclasses, so claim arbitration and reactive
wake-ups are simulated in-process rather than checked against the real API.
Registry behaviour is covered separately by `app/stardag-api`'s own suite
against Postgres, and the crossing between the two by the registry-live tier
below.

#### Registry-live tests

The tier above runs real Modal workers against fake registries. The API's own
suite runs a real registry with no Modal. **Neither covers the crossing** — a
real worker reporting to a real registry over the network — and that crossing
is not a detail of reactive scheduling, it _is_ reactive scheduling: the
worker writes status, the registry flags wake candidates, and the worker
spawns the next tick when no scheduler is live.

`integration-tests/tests_registry_live/` covers it, by deploying a registry
for the run. What the scenarios there assert:

| Scenario                    | The question it answers                                                                 |
| --------------------------- | --------------------------------------------------------------------------------------- |
| `test_reactive_e2e`         | With nothing resident anywhere, does a worker spawn the next tick?                      |
| `test_claim_race`           | Two builds want one task; does the registry let exactly one run it?                     |
| `test_cross_build_wake`     | A blocker finishes in one build — is a different, dormant build woken?                  |
| `test_wake_storm`           | Several dormant builds flagged at once — is each woken _once_, not once per notifier?   |
| `test_limit_slot_wake`      | A concurrency slot frees — is the build queued on it woken, though it shares no task?   |
| `test_suspended_blocker`    | A task suspended on dynamic children holds no claim. Is it waited on rather than reset? |
| `test_failed_blocker`       | A shared task _failed_. Is that left alone as a result, rather than reset?              |
| `test_watchdog_sweep`       | Does one sweep spawn a tick per build and return in seconds?                            |
| `test_wide_fan_out`         | A layer wider than one pass may spawn — throttle, or stall?                             |
| `test_scheduler_lease_live` | Does the lease serialize on real Postgres, and lapse on the real clock?                 |

**Two apps are deployed, not one.** `registry-live-dag` runs everything
except the watchdog sweep, which gets `registry-live-watchdog` to itself.
The sweep lists running builds scoped by _reactive app name_, so a sweep
driven against the shared app would spawn ticks for whatever else was
running at that moment — waking the dormant builds that four other
scenarios assert cannot be woken by anything but the mechanism they test.
They would not fail; they would quietly stop meaning anything. The
environment stays shared, because it is the unit of teardown; only the app
name separates them, and the image is identical so the extra deploy is
seconds.

**The registry runs its own Postgres inside its own Modal container.** There
is no database account to create, nothing to provision and nothing to clean
up: `modal environment delete` takes the API, the database, the worker app,
the target-root volume and the API-key secret in one call. Migrations run from
scratch on every container start, which is a check the deployed path never
performs.

The price of that is that the container holding the database must not be
replaced mid-run, because a recycle loses the whole database rather than some
rows — and every scenario then fails in ways that read as scheduling bugs.
`min_containers=1` and an explicit CPU/memory request are the prevention; the
boot nonce on `/_harness/boot` is the detection, checked after every scenario,
so a recycle is one sentence naming the cause instead of a debugging session.

**A recycled container is retried, once, and nothing else is.** CI reruns the
tier — re-provisioning first, since the replacement container's database is
empty — when and only when that boot nonce changed. A scenario that failed on
its own merits is never retried. Locally the marker is not written and the
assertion message tells you to provision again. The alternative, PGDATA on a
Modal Volume, was considered and rejected; `record_recycle` in `_harness.py`
carries the reasoning.

**A transport timeout is the second retryable failure, and the list ends
there.** A request that receives _no HTTP response at all_ says nothing about
the code under test. It has happened five times, in five scenarios against
five endpoints, and the cause is still unidentified. CI runs the tier once
more — without re-provisioning, since the stack is intact — and emits a
workflow warning, so occurrences are counted rather than silenced. Strictly a
timeout: an assertion failure and an HTTP error status are real results and
fail immediately. `_diagnostics.transport_timeout` states both exclusions, and
`tests/test_registry_live_diagnostics.py` holds them.

**Read the phase before concluding what was lost**, because it is recorded and
it is not always the same thing. Fixtures here talk to the registry at both
ends of a scenario — `slot_limit` sets a concurrency limit before and deletes
it after — so a timeout in `setup` means the scenario never started, one in
`call` means no assertion was reached, and one in `teardown` can follow a body
that passed and proved exactly what it set out to. Every marker line, record
filename and record body carries it.

**A failure that is not a timeout forbids the retry, whatever phase it came
from.** One exemption exists and it is by type, not by phase:
`RegistryContainerRecycled`, which is the recycle case and has a recovery of
its own. A fixture failing to clean up is a real failure and ends the run.

**On a timeout the harness probes `/_harness/boot` before the scenario gives
up.** It returns a closure variable and touches no database, so a probe that
does not answer, or takes seconds to, means the container itself is not
serving — that is hypothesis A, identified outright.

**A prompt answer refutes A and establishes nothing else**, which is worth
stating flatly because the first version of this claimed otherwise. It read a
fast probe as proof that the _database_ path was blocked and named the
registry's locking as the lever; the endpoint it asks touches no database, so
it answers exactly as fast whether the timed-out handler was slow or its
response was produced and lost. The run that first exercised it served 6257
requests with a maximum handler time of 460 ms, so the verdict was contradicted
by evidence in its own artifact (STA-92). The label for that case is
`CONTAINER SERVING` — an observation, not a hypothesis.

**What separates the remaining hypotheses is the access log**, and the join is
a separate pass because the log is not readable from inside the run. After the
dump, `diagnose.py` reconciles each timeout's JSON sidecar with the registry's
own account of that request, writes `verdicts.txt`, and — on a runner —
emits the verdict as a `Registry-live verdict` annotation next to the retry
warning it explains, so the run summary says what the timeout was and not
only that there was one:

| What the log shows for the timed-out request    | Verdict                                       |
| ----------------------------------------------- | --------------------------------------------- |
| Seconds inside the handler                      | **B** — the database path; a product signal   |
| Fast handler, long total: it sat queued         | **A** — a starved container                   |
| Served in milliseconds, or never logged at all  | **C** — the server was not what took the time |
| Anything, but the log misses part of the window | **no verdict** — a gap, not evidence          |

C is a positive finding rather than a fallback, and it is what the live data
shows: the traceback ends in `httpcore._receive_response_body`, so the
response head arrived and the body did not.

**Three rules the pass follows, each learned by getting it wrong.**

A probe that found nothing serving is never overturned by a later reading of a
log — a direct observation outranks an inference, and the log would be quiet in
exactly that case.

A positive verdict says how many requests matched: paths that create a resource
carry no id, twelve workers issue them at once, so a slow line in the window is
a candidate rather than an identification.

**A and B rest on a line that is present; C rests on nothing in the window
being slow.** So truncation cannot touch the first two and withdraws the third:
if the dump's oldest line falls inside the lookback, the answer is `no verdict`
even though rows were found, because "none of the ones I can see was slow" is
not the claim C makes. So the coverage check never pre-empts a positive
finding: with rows in hand it runs after B and A, and it runs first only when
there are no rows at all — where there is nothing for it to suppress.

**Both callers share one decision function**, which is the point of it
existing. They were separate, and the copy used when the exception carried no
request had grown only a B branch — so a queued row produced C there and A on
the other path: two verdicts for one set of facts, the wrong one being a C.
The wording still differs by how much is known; the decision does not.

**Everything a red run should be diagnosed from is uploaded as one artifact**,
`registry-live-diagnostics-<attempt>`. That is not a convenience: `modal
environment delete` takes the registry container, the scenario apps and every
line they logged, minutes after the run goes red, and three separate
occurrences were diagnosable only because somebody happened to pull the logs
by hand while the other tier was still running. The artifact holds both marker
files, one record per timeout with its boot probe — plus a JSON sidecar of the
same facts, which is what the join reads, so rewriting a sentence in the record
cannot silently break it — `verdicts.txt`, each attempt's pytest output, and
`modal app logs` for all four apps — the registry, both scenario apps and the
one `test_rollover` deploys for itself — with timestamps and container ids. The
registry's access log reports `duration` and `execution` separately per
request, which is the line-level form of the same question — time spent queued
against time spent in the handler.

**The workflow and the code it runs come from different commits.** For a
`pull_request` event GitHub takes the workflow file from the merge ref, while
this job checks out the PR's own head on purpose. A branch not rebased since
the instrument landed therefore runs the markers, retry and log dump against a
tree that has none of the code behind them — which once reported "no scenario
reported a transport timeout" for a run holding two of them, and failed the
dump with `invalid choice: 'logs'`. A step checks the checkout up front now and
says "not measured" rather than "measured and found nothing"; the fix is to
rebase.

It checks the **three capabilities separately** — the classifier, the log dump
and the verdict pass — because they landed in different PRs and a single
boolean over them fails the wrong way: a branch carrying the classifier but not
the verdict join would be treated as having neither and would lose its Modal
log dump, which is the most useful thing in the artifact and a capability that
branch has. Each consumer is gated on what it actually calls.

Locally none of that is configured and the record is printed to stderr
instead. `provision logs --output-dir <dir>` is the log dump on its own, and
`python -m stardag_integration_tests.registry_live.diagnose --dir <dir>` runs
the verdict pass over any directory holding a `registry.log` and some records —
including one downloaded from a CI run with `gh run download`.

Both retries are counted as workflow annotations, titled
`Registry transport timeout` and `Registry container recycled`, so the rate is
a query rather than a memory:

```bash
gh api repos/stardag-dev/stardag/actions/runs/<run-id>/jobs \
  -q '.jobs[] | select(.name=="Registry-live tier") | .check_run_url' \
  | xargs -I{} gh api {}/annotations \
      -q '.[] | select(.annotation_level=="warning") | .title'
```

The job's own `check_run_url` rather than its `id`: the two happen to be equal
for Actions today, and nothing says they must stay so.

If the rate does not fall once a cause is found and fixed, the answer is to
escalate — never to widen what the retry accepts.

##### Running it against your own Modal account

You need Modal credentials and nothing else. Everything lands in a Modal
environment named after your checkout, so several worktrees can each have
their own stack at once:

```bash
export MODAL_PROFILE=<your-dev-profile>

# Bring a stack up: ~30s against a warm image cache, ~90s cold.
uv run --project integration-tests --python 3.12 python \
  -m stardag_integration_tests.registry_live.provision up

# Run the scenarios (concurrently).
tox -e registry-modal-live

# ...iterate on a scenario against the same stack, as often as you like...

# Throw the whole thing away.
uv run --project integration-tests --python 3.12 python \
  -m stardag_integration_tests.registry_live.provision down
```

**`--python 3.12` is not optional.** Both Modal images take their Python
from whatever interpreter serializes their functions, and the tox env pins
3.12 (`basepython`, `UV_PYTHON`). Provisioning under a different
interpreter — which `uv` will happily pick, since the project only requires
`>=3.11` — deploys functions one minor version cannot unpickle, and the
container dies with `Runner segmentation fault (SIGSEGV), exit code: 139`,
no traceback, leaving a build that looks empty. It also rebuilds the venv on
every alternation between the two commands.

`provision stop` is the middle option: it stops the deployed apps but keeps
the environment and its warm image layers, so the next `up` is still fast.
Worth it if you are done for the day but not done with the branch — the
registry pins `min_containers=1`, so a deployment left up keeps a container
warm indefinitely rather than scaling to zero.

`provision` names the environment `dev-<checkout-directory>` — so a worktree
at `worktrees/my-feature` gets `dev-my-feature`. Pass `--modal-env` to
override. It refuses any name outside the `dev-` and `ci-` prefixes: deleting
a Modal environment is irrevocable and takes everything inside it, and the
workspace may well also hold deployments you care about.

**Keeping the stack between runs is the point.** Provisioning is the slow
part; re-running one scenario against a live stack takes seconds. Tear it down
when you are done with the branch, not after every run.

Set `STARDAG_MODAL_TEST_WORKSPACE` if you want provisioning to assert which
Modal account it is about to build in — resolved from the token, not from a
profile name.

##### Concurrency, and turning it off

The scenarios run concurrently (`-n 12`, sized to the scenario count). They
are almost entirely sleep — each waits on Modal containers it does not own —
so running them together costs little more than running the longest, and the
tier's runtime is the length of its slowest scenario rather than the sum of
its parts. They share one registry container, which serves them concurrently,
and each salts its own task ids.

Measured: **~3.5 minutes** for the thirteen tests, plus ~40s to provision.
The bound is `test_suspended_blocker` at ~200s — it has to hold a task
RUNNING long enough for a second build to register against it, then
SUSPENDED long enough for that build to tick while it is.

**Wait on a state, never on a clock.** Every scenario here rests on an
ordering — a second build registering while a task is still RUNNING, a tick
landing while one is SUSPENDED — and a `sleep` sized for that window is wrong
in both directions. It is wall clock on every run, and on the one run where
Modal is slow to start a container it silently produces the situation it was
meant to prevent: a scenario that still passes while testing something
weaker. `_wait.wait_for_task_status` is the alternative, and where a window
is a guess about infrastructure rather than about scheduling, the scenario
asserts the ordering actually held and says which constant to raise if it
did not.

The rule survives even where the thing under test _is_ a clock. The scheduler
lease expires, so `test_scheduler_lease_live` cannot avoid timing — but it
waits on the expiry rather than asserting at an instant, and it asks anything
that must hold _while_ a lease is live of a lease with ten minutes left, never
of the five-second one that is about to lapse. That distinction is the
difference between a test and a bet on latency: the earlier form asked "is a
competitor refused?" one round trip into a five-second lease, and on a loaded
runner the round trip outlived the lease, so a correct answer was reported as
a failure on somebody's unrelated PR.

**Assert on durable state, never on a report from a process that can be
preempted.** The sibling rule, and the one with teeth: a tick summary, a
worker's self-report and an in-memory counter are diagnostics; the registry
is the evidence. A scheduler tick can be killed at any moment, so
`assert any(s.get("rolled_over") for s in summaries)` tests the absence of a
_report_ and concludes the absence of the _behaviour_ — and those come apart
exactly when it matters. A tick rolled a build over on 2026-09-21 and was
preempted 53 seconds later; the rollover was correct and the scenario failed
in the same words a genuine rollover regression would have produced. That is
the cost: not the flake, but that a real regression then reads as the known
flake. `test_rollover` now asserts that _this build_ is scoped to the new
code, that edges exist under it, and that the deployments record names it
current — none of which needs a reporter to survive — and prints what the
ticks said underneath, as a diagnostic.

The direction matters, and it is the whole of how to apply this. Requiring a
_good_ report to be present fails when the reporter dies. Requiring a _bad_
one to be absent can only fail on evidence that really exists, so
`assert not any(s.get("outcome") == "rollover_failed" ...)` stays. When the
durable substitute is not obvious, it is usually the task event log:
`test_interruption_classification` asks for a `task_started` recorded after
the `task_interrupted` rather than for a tick's `interruptions_restarted`
count, which says the same thing and survives the tick that did it.

**A truncated trail cannot answer a counting question in either direction,
so counts come from the event log.** This is the correction to a rule that
briefly said the opposite. Losing a tick's summary lowers any sum read from
the trail, so an exact count fails for a reason that is not the code's — and
relaxing it to `<=` is _worse_, not safer: the missing summary is exactly
where a duplicate spawn would have been recorded, so the ceiling passes
**because** the evidence is gone. A one-directional argument (an under-count
cannot breach a ceiling) rules out false failures and says nothing about false
passes; both have to be ruled out before a weakened assertion is honest.

The way out is that the thing being counted usually _is_ durable, and the
harness was asking the wrong witness. A tick records a `task_started` once
`submit_detached` has returned, carrying the backend's reference for the call
it just created — so the registry holds one ref-bearing row per execution
actually submitted, written before the container reports anything and
therefore immune to the tick dying on the way home.
`_events.spawned_executions` counts **distinct `executor_ref`s** per task, and
every spawn assertion is `== N` strict against it.

Both halves of "distinct ref-bearing" carry weight, and each was got wrong
once before it was got right. _Distinct_, because the SDK retries a POST whose
response was lost and the API deliberately appends a second row for it — two
rows naming one call are one execution. _Ref-bearing_, because **a granted
claim is not a spawn**: the claim is taken first and the submission can still
fail, in which case the tick records a task failure and never increments
`spawned`, while the claim row sits there looking like an execution that never
happened. The ref only exists once there is a call to name, so counting refs
excludes that case by construction rather than by a special case.

**What genuinely has no durable record is skipped, and the skip is counted.**
A tick self-healing a completion, a concurrency-limit denial and a tick
finding the lease held are reported nowhere but the trail. For those,
`_wait.require_complete_trail` returns no answer when the terminal tick never
reported: `pytest.skip` with a reason, plus a marker CI counts the way it
counts transport-timeout retries. A skip that nobody counts is how a tier
quietly skips its way to green; a counted one is a measurement.

**Run the full tier in CI, not locally.** A local run deploys into the same
Modal workspace CI uses, so it contends with whatever checks are in flight —
and contention is the leading unexplained variable behind this tier's red
rate. Verifying locally to protect CI makes CI less reliable, for you and for
everyone else with a PR open. Push and read the `Registry-live tier` check;
when it goes red the diagnostics artifact is downloadable, so a CI red is as
diagnosable as a local one. Keep local stacks for what they are good at:
iterating on one scenario by name (`tox -e registry-modal-live -- -n0
tests_registry_live -k <name>`), harness work that needs a stack, and forcing
a branch CI cannot reach. Tear one down as soon as it is done.

**The tier is serialised workspace-wide, so a pending run is normal and a
cancelled one is not a failure.** One run peaks at a realistic 45–65 Modal
containers, with an upper bound near 115 and no `max_containers` on any
scenario worker or tick function. The `andhus` workspace caps at **100
concurrent containers** and also carries every developer's `dev-<checkout>`
stack and the self-hosted deployment — so two tier runs do not fit, and on
2026-09-22 the limit was reached. The `registry-live` job therefore sits in a
job-level concurrency group whose name is a constant
(`registry-live-andhus-workspace`): one tier run at a time across every PR and
the weekly schedule, with `cancel-in-progress: false` so a run already holding
a Modal environment is never killed mid-flight. Expect the check to sit pending
for around ten minutes when another PR is ahead of you.

**The trap, because it looks exactly like a failure.** GitHub keeps at most
**one running and one pending** job per concurrency group. A third run does not
queue behind the second — it takes the pending slot, and the _older pending_
job is **cancelled**. On that PR it shows as a cancelled check with **no logs
at all**, because a job cancelled before it starts runs no steps and so cannot
explain itself.

**Two different things produce that same logless cancelled check**, so do not
attribute every one of them to the group above:

| What happened                   | How to tell                                                                                                                                            |
| ------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------ |
| You pushed again to this PR     | The workflow-level group has `cancel-in-progress: true` for pull requests, so the previous run is killed wherever it had got to. There is a newer run. |
| A third run took the queue slot | The workspace-wide group dropped the older pending job. The newest run on the PR is the cancelled one.                                                 |

Neither is a failure, neither is a flake, and nothing is wrong with the branch:
re-run the job, or push again. Since the cancelled job cannot write any of this
itself, the `Decide what to run` job writes it to the run summary up front,
whenever it decides the tier should run — the only place in the run that can.

**One consequence, stated as an exposure rather than a reassurance.** Teardown
is gated on `always() && !cancelled()`. If a cancelled `registry-live` job makes
the _run_ read as cancelled, that run's Modal environment is not deleted by its
own teardown — and the `Sweep stale Modal environments` backstop **will not
collect it while the PR is open**: it deletes a `ci-pr-<n>` environment only
once PR `<n>` is no longer `OPEN`. So the environment, and the warm container
its `min_containers=1` registry holds, can persist until the PR is closed.

That gap is not new — a hand-cancelled run has always been able to produce it —
but this change makes it reachable without anyone pressing cancel, so it is
worth knowing while it stands. Whether the cancellation reads as a _run_
cancellation at all is the part still to confirm on first occurrence; if it does
not, teardown runs normally and none of this applies. The sweeper itself runs on
**every workflow run**, not nightly and not only on the weekly schedule — its
own comment explains why.

**Take a precondition from the constants only where the arithmetic closes.**
`test_reactive_e2e` spawns its own work, so a tick that lingers for a fixed
window once idle must go before work that outlasts it —
`assert_dormancy_is_forced` asserts exactly that, and no container is slow
enough to break it. The other three wake-up scenarios wait on a task **another
build already started**, where the constant is the task's total runtime and
the quantity that matters is what remains when the waiting build's tick
begins. Comparing the constant there lets a slow bootstrap leave the build
resident through the completion while `75 > 15` still looks reassuring, so
`assert_remaining_work_outlasts_linger` measures the remainder from the
registry's record of the start, at the moment the waiting build is triggered.

**`wait_for_terminal` no longer fails on a missing final summary.** Its wait
stays — the read-before-report race is real, and without it every counter read
below is short by a tick. But 15 of the 18 scenarios call it, so raising there
meant a tick preempted between writing the build's terminal status and
reporting its summary reddened whichever scenario was unlucky, after ninety
seconds, with a message that reads like a scheduling defect. It now warns,
records the trail as possibly truncated, and returns; `trail_may_be_truncated`
lets a failure message say so.

**A scenario that needs its second build woken _twice_ should keep that
build resident instead.** `select_wake_candidates` hands a flagged build
out at most once per 120s window and does not compare the flag's timestamp
against the hand-out's — so a build re-flagged after being handed out
waits for the window to lapse, and then needs someone to drain. In the
full concurrent tier another scenario's tick does that (drains are not
scoped to the caller's build), so such a scenario passes here and hangs
under `-n0`. The watchdog is the production backstop for it, and this tier
runs none. The two blocker scenarios keep their second build resident for
this reason — which is the better shape anyway, since what they test is
what a tick _decides_, not who called it.

**When something fails, run them one at a time.** A shared registry and
interleaved logs make a low-level failure much harder to read:

```bash
tox -e registry-modal-live -- -n0 tests_registry_live -k cross_build_wake
```

Note the path is repeated: `--` replaces tox's default posargs wholesale, so
passing only flags would drop it. In CI the same switch is the `serial` input
on `workflow_dispatch`.

##### Gating

`STARDAG_REGISTRY_LIVE_TESTS` is on or off, with no `auto` in between —
unlike the Modal tier, which can cheaply ask "are there credentials?" and skip
politely. Here there is nothing to detect: the registry does not exist until
this tier deploys one, so "detect and decide" would mean building the stack in
order to find out whether to build it. The tox env sets it, and the tier lives
outside the project's default `testpaths`, so neither a bare `pytest` nor a
bare `tox` can reach it.

When it is on, the guard asserts — at module import, so a misconfiguration is
a collection error rather than a scenario that quietly ran against nothing:

- the resolved registry is a real `APIRegistry`, not a `NoOp` (with no
  registry configured the SDK falls back to one, and the scenarios would pass
  having checked nothing);
- it is **this session's deployment**, by URL. The type check alone is not
  enough: a production registry is a perfectly real `APIRegistry`, and these
  scenarios trigger builds, race claims and cancel things;
- it answers an authenticated request, made through the registry's own
  client, so the credentials are proven rather than assumed.

The SDK is configured from `STARDAG_API_URL` / `STARDAG_WORKSPACE_ID` /
`STARDAG_ENVIRONMENT_ID` / `STARDAG_API_KEY` rather than a profile, and that
matters more than it looks: profile resolution reads `~/.stardag/config.toml`
_and_ walks the working directory's parents looking for one, so a checkout
under your home directory finds your real config several levels up.

##### In CI

The same workflow runs both tiers, decided separately, into the same
per-run Modal environment. A change under `app/stardag-api/`,
`lib/stardag/src/stardag/{build,registry,integration/modal,selfhost,testing/modal,_cli}/`
or `integration-tests/` triggers this one. (`_cli/` is in that list because
the post-deploy wiring the harness reuses — login, workspace resolution,
minting the API key and pushing it as a Modal secret — lives in
`_cli/_selfhost_connect.py`.) Teardown is its own job that waits for
both tiers — sharing an environment means whichever finished first would
otherwise delete the other's stack out from under it.

### Linting & Formatting

```bash
tox -e pre-commit
```

### Type Checking (pyright)

```bash
# Type check specific package
tox -e stardag-pyright
tox -e stardag-examples-pyright
tox -e stardag-api-pyright
```

Note: pyright currently has pre-existing errors and is excluded from CI.

### Full CI Check

```bash
tox
```

## Frontend Development

```bash
cd app/stardag-ui
npm run dev      # Start dev server (port 5173)
npm test         # Run tests
npm run build    # Production build
```

The dev server proxies `/api` to `http://localhost:8000`.

## Authentication for Local Development

When developing locally against the docker compose stack, you need to authenticate the SDK with the API service.

### Setup

1. Start the full stack (includes Keycloak identity provider):

```bash
docker compose up -d
```

2. Access the web UI at http://localhost:3000 and create an account or log in.

3. Install the CLI:

```bash
cd lib/stardag
uv sync --extra cli
```

### Authentication Methods

**Method 1: Browser Login (recommended for interactive development)**

```bash
uv run stardag auth login
```

This opens your browser to Keycloak (http://localhost:8080). After login, tokens are stored in `~/.stardag/credentials.json`.

Check your auth status:

```bash
uv run stardag auth status
```

**Method 2: API Key (for scripts/automation)**

1. Log in to the web UI at http://localhost:3000
2. Go to Organization Settings > API Keys
3. Create a new API key for your workspace
4. Set the environment variable:

```bash
export STARDAG_API_KEY=sk_your_key_here
```

### Sanity Check

After authentication, verify the setup works:

```bash
# Check auth status
uv run stardag auth status

# Run the demo script to test API registry integration
cd lib/stardag-examples
export STARDAG_API_URL=http://localhost:8000
uv run python -m stardag_examples.api_registry_demo
```

You should see tasks appearing in the web UI at http://localhost:3000.

### Logout

```bash
uv run stardag auth logout
```

## Releasing the Server

The server (Registry API + web UI) is released as one image with its own
semver, independent of the SDK. API and UI share a single joint version.

Before tagging, make sure `CHANGELOG.md` has an entry covering the
release's Registry API / UI / Deployment changes (move them out of
`[Unreleased]`) — the GitHub release links to it.

### When to cut one

**After each significant change to the Registry API or the UI**, not in
batches. The image is the only route those changes have to a self-hosted
deployment: the hosted service builds from a commit, so it always runs the
newest API, but a self-hoster runs whatever the last `server-v*` tag built.
An unreleased API change therefore reaches nobody outside the hosted
deployment.

Letting releases lag has a specific failure mode, and it is silent. Version
skew degrades gracefully by design — an older registry answers nothing to an
endpoint it does not have, and the SDK falls back — so a self-hoster on a
stale image sees no error. They see a feature they upgraded the SDK for
quietly not working. `server-v0.1.2` sat for 18 days that way, through six
SDK releases (v0.19.0 → v0.22.0) — including the cross-build wake-up
endpoints v0.22.0's headline feature is built on.

"Significant" means anything a user could notice: new or changed endpoints, a
schema migration, a UI change, a dependency swap on the auth or security
path. A pure refactor with no external surface can wait for the next one.
Bump the minor when the HTTP surface grows or there is a migration, the patch
for fixes and dependency floors.

Bump `DEFAULT_SERVER_VERSION` in the same PR — the pin is what a fresh
`stardag self-host up` gets.

### Dropping support for older SDKs

The hosted service always runs the latest API, so the compatibility case
that actually happens is an **old SDK against a new API**. The server
accepts every SDK version by default; the floor lives in
`STARDAG_API_SDK_MINIMUM_VERSION` (see
`app/stardag-api/src/stardag_api/sdk_compat.py`) and is published as
`minimum_sdk_version` on `GET /api/v1/version`.

Raising that floor is a product decision, not an implementation detail: it
breaks working deployments on purpose. **An API change that raises
`minimum_sdk_version` must say so in all three places a user could look:**

1. `CHANGELOG.md` — under the release's Registry API section, with the new
   minimum and what stopped working below it.
2. `RELEASE_NOTES.md` — under the SDK release that clears the bar, as a
   migration note. This is the file users are pointed at when they upgrade.
3. **The error the server returns** — which is automatic, provided you set
   the value rather than special-casing anything: the 426 body names the
   client's version, the required version and the upgrade command.

A newer SDK against an older self-hosted API is not a supported
configuration and nothing tries to keep it working — self-hosters upgrade
the server and the SDK together.
The image definition is `app/server.Dockerfile` (build context = repo root):

```bash
docker build -f app/server.Dockerfile -t stardag-server .
```

> **Python versions are decoupled for the prebuilt path.** `stardag
self-host up` deploys the prebuilt image by _reference_: it points the
> Modal `web`/`migrate` functions at module-level entry points in
> `lib/stardag/src/stardag/selfhost/_modal_entry.py` (`serialized=False`),
> which Modal imports inside the image. Nothing is cloudpickled, so the
> CLI's interpreter is independent of the Dockerfile's base Python — bump
> the Dockerfile freely. (Only `--from-source` still serializes function
> bodies with the client interpreter, and there the image's Python is
> matched to it automatically.)

To release, push a `server-vX.Y.Z` tag on `main`:

```bash
git tag server-vX.Y.Z
git push origin server-vX.Y.Z
```

CI (`.github/workflows/publish-server-image.yml`) then:

1. Builds the image and pushes it to
   `ghcr.io/stardag-dev/stardag-server:X.Y.Z` and `:latest`, with
   `STARDAG_SERVER_VERSION=X.Y.Z` baked in (surfaced at
   `GET /api/v1/version`).
2. Creates a GitHub Release for the tag with the web UI (extracted from the
   pushed image, so it is byte-identical to what the image serves) attached
   as `stardag-ui-dist-X.Y.Z.tar.gz` (for deployments that serve the UI
   separately, e.g. from S3/CDN).

### First release only: make the GHCR package public

The first push creates the `stardag-server` GHCR package with **private**
visibility. `stardag self-host` pulls the image anonymously
(`modal.Image.from_registry` without credentials), so the prebuilt-image
path fails for everyone until the package is made public. One-time step
after the first release workflow completes:

1. Go to the package settings:
   <https://github.com/orgs/stardag-dev/packages/container/stardag-server/settings>
2. Under "Danger Zone" → "Change package visibility", set it to **Public**.
3. While there, connect the package to the repository (adds the README and
   links it from the repo's Packages sidebar).

Verify with an anonymous pull: `docker logout ghcr.io && docker pull
ghcr.io/stardag-dev/stardag-server:X.Y.Z`.

`stardag self-host` deploys the prebuilt image by default; each SDK release
pins the server version it was tested against
(`DEFAULT_SERVER_VERSION` in `lib/stardag/src/stardag/selfhost/_modal_app.py`
— bump it when a new server version becomes the tested pairing).

### Version convention for non-release builds

Release builds get a clean `X.Y.Z` from the tag (CI passes it as the
`STARDAG_SERVER_VERSION` build arg). Any _other_ build of
`app/server.Dockerfile` (e.g. a deployment pipeline building from an
arbitrary commit) should derive the version with `scripts/server-version.sh`,
which normalizes `git describe --tags --match "server-v*"` to semver
build-metadata form — so deployments truthfully report their deviation from
the nearest release:

| State                          | Version          |
| ------------------------------ | ---------------- |
| Exactly at `server-vX.Y.Z`     | `X.Y.Z`          |
| N commits past the nearest tag | `X.Y.Z+N.g<sha>` |
| No `server-v*` tag reachable   | `0.0.0+g<sha>`   |

```bash
docker build -f app/server.Dockerfile \
  --build-arg STARDAG_SERVER_VERSION="$(scripts/server-version.sh)" .
```

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for guidelines on submitting changes.
