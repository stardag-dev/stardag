# Scope-keyed dependency structure

> **Superseded** by [registry-v2/design.md](registry-v2/design.md)
> (2026-09-23). Kept for its record of the problem and of the two designs
> abandoned before it; the mechanics described here are v1's.

What a task id promises and what it does not, why the registry keeps a
task's dependency edges per _structure scope_ rather than globally or per
build, and what that makes of the three kinds of parameter a task can have.

Written after two earlier designs for the same problem were built, reviewed
and abandoned. The last section records them and why, because the framing
they used is the one a reader will arrive at first.

## The problem this answers

Three bugs, one incident, all about dependency edges outliving the code that
declared them. The registry recorded every edge ever registered for a task
id, environment-wide and forever, and gated the task on all of them:

- A task whose `requires()` changed stayed gated on an upstream nothing
  would ever produce again. The only escape was bumping the _downstream's_
  version.
- A dynamic fan-out abandoned mid-flight gated its own parent, and a later
  build re-ran the whole stale generation before it could do anything else.
- A runtime-only parameter that changed how a task fanned out was recorded
  once, at first registration, and never again, so a later build asking for
  a different width silently ran at the first one.

The first two look like the registry failing to keep up with the code. The
third looks like a persistence bug. All three are the same mistake: **the
registry treated a task's structure as a property of its identity**, when it
is a property of the code and configuration that evaluated it.

## Two identities, doing two jobs

A task has an identity that must survive code changes, and a structure that
must not.

**Completion identity** is the task id: a hash of the task's parameters,
never of its code. It names the promise the task makes about the world
state it establishes — _same id, same output_ — and the user keeps that
promise by bumping `__version__` when the output would change. It has to
survive code changes, or nothing finished would ever be reused.

**Structure identity** is the set of upstream tasks the code declares and
discovers for that id: `requires()` plus whatever `run()` yields. It is a
function of the code and of the configuration the code reads. It only
matters for tasks that have not completed yet, because discovery never walks
past a complete task, and incomplete work is cheap to re-plan.

The apparent inconsistency — hashing parameters only for one identity, code
and config for the other — is the two identities doing different jobs. A
promise must be permanent. A plan exists only for work still to be done.

So:

> **A task id promises the world state its completion establishes. It does
> not promise the upstream set it was built from.** A downstream task asks
> for its upstream's world state, not for how the upstream got there.

The alternative, making the upstream set part of the promise, was built and
rejected (see the last section). Its cost is a cascade: change what `U`
requires and `U` must bump its version; then every task that requires `U`
without holding it as a parameter must bump too, because _its_ upstream set
changed. Nobody wants that, and nobody asked for it.

## The rule

> **Dependency edges are scoped to a structure scope: the code they were
> evaluated under, plus the structure-significant part of the build
> configuration they were evaluated with. Completion and the execution claim
> stay global, keyed on task id.**

Concretely:

- Every edge row carries a `scope_key`. Every build carries one — **the
  scope it is currently planned under** — and readiness ("are all my
  upstreams complete?") is evaluated over the edges in that scope only.
- A build's scope moves when the code that drives it does: the first
  scheduler pass on new code re-plans the build under its own scope (see
  [Rollover](#rollover-a-build-follows-the-live-deployment)). Resuming or
  re-triggering a build under a different `dependencies_only` config is
  refused; a build has one config for its life.
- Within a scope, edges only grow. Nothing retracts them, and no build
  outside the scope reads them.
- Two builds in different scopes may materialise one task id over different
  upstream sets. The duplicated upstream work is accepted. The outputs are
  equal by the user's existing obligation.

### Why it is sound

Gating can only over-approximate. Within a scope edges are only ever added,
so a task is gated on at least everything the current code declared for it.
Nobody outside the scope can remove a gate, because nobody outside the scope
can see the row. Under-gating — running a task before an upstream its code
reads is complete — is the only route to wrong output, and it is not
reachable.

That is the whole argument. It is the same argument per-build edges would
have, with more sharing.

### Why the sharing is licensed

A build in a scope trusts edges another build in the same scope recorded.
That is valid because of a contract stardag already states: a task that
reads mutable data must snapshot it and reference the snapshot by a
parameter. Under that rule, a task's static and dynamic upstream set is a
function of its parameters, its code and its structure-significant
configuration. Fix the last two and the structure is deterministic per task
id. An edge one build discovered is exactly the edge the next build in the
scope would rediscover. Discovering it again is only cost.

### What it buys

- A changed `requires()` or fan-out needs no version bump and no refusal:
  new code is a new scope, a new build plans under it, and a running build
  rolls over to it at its next scheduler pass.
- Many builds over overlapping DAGs under one deployment share discovered
  structure. A fan-out's pre-yield section runs once per scope, not once per
  build.
- No retraction rule anywhere, and so no question of the form "is that edge
  still current" or "is that attempt the current one". An edge scoped to a
  code version is a historical fact by construction.
- Plan closure — admitting into a build's plan the incomplete upstreams of
  its tasks along recorded edges — becomes unconditionally correct, because
  within a scope those edges cannot be stale by code. Closure runs at
  registration and again whenever a build stalls, so an edge a scope-mate's
  worker wrote after the build closed its plan is picked up rather than
  waited on.
- A child that can never complete gates its parent until the code is
  fixed, and fixing the code is a new scope. No version bump moving every
  downstream id.

## The scope key

| Component             | Definition                                                                                                                                         | Source                                                                                                                  |
| --------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------- |
| code id               | the full git SHA when the tree is clean; a fresh UUID, with a loud warning, when it is dirty                                                       | minted once when a Modal app is finalized for deploy and baked into it; computed at process start for a local build     |
| structure config hash | hash of the `dependencies_only` entries of the build config, each validated against its field and serialised in hash mode, no-op overrides dropped | computed where discovery runs — the reactive bootstrap inside the deployment, or the local process for a resident build |

The stardag _environment_ is not in the key. It is the outer scope of
everything already: tasks are unique per environment, and edges, builds and
deployments live inside one.

Environment _variables_ are not in the key either, by contract (below).

**Who computes it.** Whoever runs discovery: the reactive bootstrap, inside
the deployment, for a reactive build; the local process for a resident one.
The bootstrap writes the scope onto the build before registering any edge.
Every scheduler tick derives its own code id the same way and compares it
with the code id half of the build's scope; a mismatch means the build was
planned by other code and the tick **re-plans it under its own** (below).
Only that half is compared: the config half is a function of the build's own
config, which the tick installs, so recomputing it would verify nothing —
and would require every task class the config names to be importable in a
container that may have rehydrated a single task. Workers compare nothing:
a task id promises the output whatever code produces it. What a worker owns
is the scope its _yields_ are recorded under — its own code id with the
config half it was handed — so the structure a worker discovers is always
attributed to the code that discovered it. A laptop whose clean tree is at
the deployment's SHA shares the deployment's scope; a dirty laptop gets a
private scope per process, always correct and never cached.

## The three levels of significance

A task's parameters fall into three levels, and the registry treats them
differently.

| Level           | Affects                                                                             | Lives in                                   | Annotation                                          |
| --------------- | ----------------------------------------------------------------------------------- | ------------------------------------------ | --------------------------------------------------- |
| 1. Identity     | the output — what the task promises                                                 | the task id and its registered `task_data` | `StardagField()` default, `significance="identity"` |
| 2. Dependencies | the upstream set (static or dynamic), not the output; e.g. a fan-out partition size | the build config; hashed into the scope    | `StardagField(significance="dependencies_only")`    |
| 3. Execution    | neither output nor structure, only how the work is done; e.g. a thread count        | the build config; not in the scope         | `StardagField(significance="execution_only")`       |

```python
class MyTask(sd.Task):
    __namespace__ = "my_namespace"
    param: str
    partition_size: Annotated[int, sd.StardagField(significance="dependencies_only")] = 100
    num_threads: Annotated[int, sd.StardagField(significance="execution_only")] = 10

MyTask(param="value")                    # fine
MyTask(param="value", num_threads=2)     # raises

app.build_trigger(
    MyTask(param="value"),
    reactive=True,
    build_config={"my_namespace.MyTask": {"partition_size": 500, "num_threads": 5}},
)
```

### One mechanism, and why it must be the only one

Levels 2 and 3 are read **only** from the build config, through the field's
default, and can never be passed at Python init. This is what makes the scope
well defined.

Suppose a downstream `t1` could pass a partition size to its upstream `u`
while another downstream `t2` let `u` read the config. There would then be
two versions of `u` in one build, with one task id, not separable by any
scope key. And to keep any cache honest, every root task would have to
expose level 2 and 3 parameters for its entire upstream cone, even though a
downstream almost never has an opinion about how its upstream fans out.
Forbidding explicit init removes both problems by construction.

It also settles a question that is otherwise genuinely hard: _which_
identity of an upstream does a downstream's edge refer to? Level 1, the task
id, exactly as it always has. A task's level 2 identity is implicitly the
pair of its task id and the build's scope. No per-task dependency id needs
to exist, and nothing trickles down.

### What is persisted

Nothing at levels 2 or 3, per task. A task's registered `task_data` is its
identity-level serialisation, a pure function of the task id. The build row
carries the config. The full configuration of a task instance in a build is
the join of the two.

A consequence worth stating: the hash controls on a `dependencies_only`
field — a float truncated for stability, a custom hash-mode serializer —
apply to the config value exactly as they would to an identity parameter,
because the scope hashes the value through the field. The one control that
does nothing there is `compat_default`, since a default lives in code and the
code id already covers it; the annotation rejects that combination rather
than accept it silently.

### The contract on environment variables

> **An environment variable must not affect a task's output or its
> dependency structure. It may affect execution.**

Anything that changes what a task yields or writes is a parameter, and so in
the id, or a `dependencies_only` config value, and so in the scope. Reading it
from the environment instead is the same breach as reading an unsnapshotted
table: the structure recorded under a scope stops being a function of the
scope. The consequence is over-gating (below), never wrong output, and the
registry warns when it sees it.

## Rollover: a build follows the live deployment

Modal has one live deployment per app name. A redeploy lets in-flight
inputs finish on the old version, but every _new_ spawn through the app
name lands on the new one, and a reactive build progresses by new spawns.
The conventional expectation — one live version of a production app, and
running work moving to it — is what stardag follows. A **deployment** in
stardag is exactly Modal's: one code version of one app, and the registry
records each one as it is deployed (`app_name`, `code_id`, `deployed_at`);
the newest is the current one. Nothing is kept alive beside it and nothing
needs collecting.

A running build is therefore not bound to the code that planned it. **Its
scope is the scope it is currently planned under**, and the first scheduler
pass that runs on new code moves it:

1. The tick takes the build's lease and finds the build's scope names another
   code id. It **re-plans**: it rehydrates the roots from the registry (the
   identity-level `task_data` is all that is needed), runs discovery under
   its own code with the build's stored config, registers the plan's edges
   under its own scope — the same steps as the reactive bootstrap — and
   moves the build's scope to its own. Discovery stops at completed tasks,
   so this costs one walk of the incomplete part of the DAG per redeploy.
2. A tick still lingering on the old code sees the scope move on its next
   frontier read and exits as superseded; the lease already guarantees one
   driver at a time.
3. Workers are code-agnostic. A container of any version may run a task,
   because the task id already promises the output; whether the output
   changes with the code is the user's `__version__` obligation, exactly as
   before. What a worker must get right is where its **yields** land: it
   registers the dynamic edges it discovers under its own code's scope,
   never the build's current one. An old worker's late yield thus lands in
   the old scope, the rolled-over build never sees it, and the new pass
   re-runs the parent under new code — wasted work, correct outcome, the
   over-approximation the rule already accepts.
4. Executions the new plan no longer contains finish on their own. Their
   targets are content-addressed, so they harm nothing; cancelling them is
   an optional later step, and the authority rule and the executions
   endpoint from the cancel work are the mechanism.
5. A root whose identity parameters the new code changed cannot be
   rehydrated. The build fails with one message: re-trigger it as a new
   build. A build is a loose collection of roots (a re-trigger may append
   some), so the failure is per build, not per root.

What this replaces: a first version of this design kept every code version
deployed under its own Modal app (`<family>--<code id>`) so that a build
could stay on the code that planned it. That needed a registry table
joining families to handles, a resolver, a `deployment=` argument on the
trigger, a garbage collector and a watchdog per handle — all to keep alive
what the platform deliberately does not, and in direct conflict with what
"deployment" means on it. The soundness argument never needed the binding:
edges are keyed by code and config, not by build, and completion and the
claim are global.

**Branch deployments** are separate apps, `<app>-<branch>`, each with one
live version; a documented workflow, not a new concept.

## What a build does with a shared task

Because completion and the claim are global, a build routinely finds tasks in
its plan whose status another build produced. The rule is unchanged from
before: a build acts on everything in its plan, whichever build last touched
it, and the claim is the only thing that prevents two executions at once.
What changed is where the decision is made.

A task's status alone decides whether it is the build's to run, provided it
is gated open — every upstream in the build's scope complete:

| Status                  | The build…                                                                     |
| ----------------------- | ------------------------------------------------------------------------------ |
| `PENDING`               | runs it                                                                        |
| `RUNNING`, live claim   | waits; the blocker's completion wakes it                                       |
| `RUNNING`, lapsed claim | takes it over                                                                  |
| `SUSPENDED`             | runs it (a resume, from scratch, idempotent by contract)                       |
| `INTERRUPTED`           | starts it again, within its own budget                                         |
| `CANCELLED`             | resets it and runs it, within the attempt budget — a revocation, not a verdict |
| `SKIPPED`               | resets it and runs it, within the budget — a skip whose reason no longer holds |
| `FAILED`                | leaves it; a result, which `fail_mode` owns                                    |

`SKIPPED` is worth a sentence. A skip is derived: it marks a task downstream
of something that failed or was cancelled. If every upstream of a skipped
task is now complete, the reason for the skip is gone, and leaving the task
skipped would wedge the build until a re-trigger. The two conditions —
"skipped because an upstream will never complete" and "gated open" — are
disjoint at any instant, so this cannot oscillate with the skip pass.

Nothing in that table asks whether another build is alive. A gate can no
longer point outside a build's plan, so a build with nothing to run and
nothing running is genuinely finished or genuinely failed, and the
machinery that used to infer a neighbour's liveness from its status has no
remaining case.

## What this costs

**Impure structure over-gates its scope.** A yield that depends on something
outside parameters, code and level 2 config — an environment variable, the
wall clock, an unsnapshotted table, a field wrongly marked `execution_only` —
leaves a union of children recorded under one scope, and the parent is
gated on all of them until the scope is retired. Never under-gated, never
wrong output. The registry warns when a worker posts a dynamic dependency
set that differs from the one already recorded for the task in the same
scope.

**Rows grow with deploys.** One edge row per edge per scope. Gating and
closure probe one scope, so old rows cost nothing on the hot path, and they
serve the provenance view below. A retention window for old scopes' edges is
a later addition; nothing deletes them today.

**A redeploy is a new scope**, including one that changes only a comment,
and every running build re-plans once at its next scheduler pass. The cache
is per code version, and the code id does not try to be cleverer than the
SHA.

**A dirty tree never shares.** By design; the code is unknown.

## Reading the graph

A task's edges may now exist under several scopes. The graph of one build is
unambiguous: its own scope. The environment-wide view of a task follows each
node's **provenance**: the edges from the scope of the build that produced
the node's current status. The graph then shows how each task was actually
built, and hops scopes where the history did; a hop is drawn as such. A task
no build has touched has no provenance and shows no edges, which is the
truth.

Placeholder rows for tasks that were named as an upstream but never
registered no longer exist. Registration requires every declared upstream to
be registered first, which every stardag build engine already does, and
refuses otherwise. A client that gets that refusal has a bug, and now hears
about it.

---

# The abandoned paths

Two earlier designs were built for this problem. Both kept a single,
environment-global edge set, and the differences between them were about
what to do when a new declaration disagreed with the record.

## Retract and arbitrate

The first held that _an edge is evidence asserted by an act, and stops
counting when its source is withdrawn_. A static edge was superseded by the
next declaration that omitted it; a dynamic edge was retracted when the
attempt that yielded it was abandoned. Two live builds declaring different
static sets for one task was a conflict, refused by default, with an opt-in
to cancel the incumbent.

Two things killed it.

**Retraction turns every question into a question about time.** Is this edge
current, is that attempt the current one, is that build still alive. Six
review rounds produced thirteen findings, three of them introduced _by_
fixes to earlier findings, and nearly all of one shape: a historical fact
mistaken for a current one. That confusion is created by retraction, not by
carelessness.

**Superseding deletes another build's gate.** With one global edge set,
re-pointing `R` from `U1` to `U2` removes the gate a concurrent build was
relying on, and that build can then run `R` with code that reads `U1` while
`U1` is incomplete. Wrong output, not waste. That is the fatal objection,
and it is an objection to _shared_ edges being rewritten — which is exactly
what scoping removes.

## Immutable declarations

The second went the other way: _a task id promises the world state its
completion establishes, and that state includes the set of upstream
dependencies it was built from_. Any registration whose declared static set
differed from the recorded one was refused, whether or not anyone else was
running; the remedy was a version bump. Dynamic edges were append-only and
inherited by every later build.

It had the virtue of no temporal reasoning at all. It failed on what it
asked of users:

- **The cascade.** Changing what `U` requires bumps `U`; every task that
  requires `U` without holding it as a parameter must then bump too. A
  refactor near the root of a DAG re-ids the whole cone below it.
- **It refused when nobody was in the way.** A build was rejected at
  trigger over a disagreement with something recorded under code that no
  longer existed, with no concurrent build anywhere.
- **The lenient half failed worse than the strict half.** An inherited
  dynamic child that could never be built wedged its parent forever, with
  no message and a remedy — bump the parent — that moved every id below it.
- **It contradicted what a downstream actually asks for.** A task that
  requires `U` asks for `U`'s world state. Making it also assert how `U`
  came to be is a stronger promise than any downstream makes.

## Per-build edges

The design that replaced both first scoped edges to the _build_. It is
sound by the same argument as the scope rule above, and it was widened
rather than rejected: with edges per build, every collaborating build
re-runs a shared fan-out's pre-yield section once per yield stage, and a
data-driven re-yield within one build needs a retraction rule after all.
Widening the scope to the code and structure-significant config keeps the
soundness, shares the discovered structure, and removes the last retraction.

## What survived from all three

- `dependency_task_ids` on the wire is `list[str] | None`: a list is a
  declaration, `None` is "not declaring", and one value cannot carry both
  meanings.
- Registration sends the dependency sets discovery actually computed, and
  declines to declare for a task it pruned at, instead of re-evaluating
  `requires()` for every task in the payload.
- The refusal message for a build-config mismatch names both configs and
  the remedy, because a refused build looks to whoever triggered it like
  stardag declining to run. (A code mismatch is no longer a refusal: the
  build rolls over.)
- Everything in
  [execution-claims-and-liveness.md](execution-claims-and-liveness.md) about
  cancel authority and execution refs. A build is a request, not an owner;
  the claim is the only cross-build coordination; authority to revoke is
  build-scoped. The one paragraph there about plan closure not covering an
  edge written after registration is superseded by closure running again at
  stall.
