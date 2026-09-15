# Immutable dependency declarations

What a task id promises, why its upstream dependencies are part of that
promise, and why the registry never withdraws a dependency edge.

Written after a long design pass that went the other way first. The second
half of this note records that path and why it was abandoned, because the
abandoned framing is the one a reader is likely to reinvent.

## The problem this answers

Three bugs, reported against one incident, all about dependency edges
outliving the code that declared them:

- A task whose `requires()` changed stayed gated on an upstream nothing
  would ever produce again. The only escape was bumping the _downstream's_
  version.
- A dynamic fan-out abandoned mid-flight gated its own parent, and a later
  build re-ran the whole generation.
- A cancelled build re-ran its cancel cascade on every tick and killed
  executions belonging to other builds. (Separate root cause — see
  [execution-claims-and-liveness.md](execution-claims-and-liveness.md). It
  is fixed independently and nothing here depends on it.)

The first two look like the registry failing to keep up with the code. They
are better understood as the registry correctly recording a promise the user
broke.

## Why refusal is forced, before any argument about contracts

The rule below is often explained by the contract in the next section. That
is the wrong order, and it matters: **the refusal follows from append-only
edges alone.**

Edges are environment-global and nothing removes them. So if a task's
declared upstreams change and the registry accepts the change, one of two
things happens, and both are bad: the old edge survives and gates the task
on an upstream nothing will produce again — the reported bug — or the
registry retracts it, which deletes the gate a _concurrent_ build was
relying on and lets that build run the task before its own declared
upstream is complete. Wrong output. There is no third option, and neither
needs a claim about what an id means.

So: given append-only, a changed declaration must be refused. Everything
else here is about making that refusal _right_ rather than merely
necessary.

## The contract

> **A task id promises the world state its completion establishes, and that
> state includes the set of upstream dependencies it was built from.**

The usual statement of stardag's contract — _same id, same output_ — is
incomplete. It reads as though only the bytes at the target matter. But a
task's completion is also a claim about what went into it, and for a whole
class of tasks that claim _is_ the output:

A grouping or generator task often exists for no other reason. Its `run()`
does little; its target records "everything I require is complete". Change
what it requires and the target means something else, even if the bytes are
identical. The same is true, less obviously, of any task whose output is a
function of its inputs — which is all of them.

So the upstream set is part of the promise, and:

> **If a task's upstream dependencies change, its id must change** — through
> `__version__`, or through a parameter that is significant to the hash.

This is a **user obligation**, exactly like "same id, same output". stardag
cannot verify it in general: a task that has never been registered has no
recorded set to compare against, and the framework cannot know what the code
_would_ have declared last week. But where a task **has** been registered,
the registry holds the previous declaration, and it can check.

That is the whole design. Everything below follows.

**Two places where the contract and the mechanism do not line up**, worth
stating rather than discovering:

- **R5 is a general bypass.** An operator can delete the recorded edges and
  the same code then registers without a bump, so a task keeps an id that
  promises state it was not built from. That is defensible as a deliberate
  human override of a record believed wrong — it is not something a build
  can do — but it is not something the contract _alone_ would permit.
- **The check fires only where nothing has been promised.** R4 stops at
  complete tasks, so the comparison never examines a task whose completion
  actually established anything; it examines the ones still to be built.
  Operationally that is the right boundary — the gate only matters for an
  incomplete task — but it is the opposite of what the contract predicts,
  and anyone reasoning from the contract will expect the reverse.

If the contract were to be enforced rather than asked for, the honest
mechanism is to fold the declared upstream ids into the hash, so the id
moves by construction and the refusal can never fire. That is what
content-addressed build systems do, and it is filed rather than built here.

## The rules

### R1 — a static declaration is immutable

Every build that registers a task states its complete `requires()` set. If
the task has a recorded static set and the declaration differs from it, the
**build is rejected**. Nothing is written: no edge is added, none is
removed, no plan is half-registered.

The rejection is not arbitration between two builds. It does not ask who
else is running, or when they started, or whether they are still alive. It
compares two sets. A declaration that differs from the record is a statement
that the contract above was broken — by this code version or an earlier one
— and the remedy is the same either way: bump the version.

### R2 — nothing is ever retracted

No build action supersedes, deletes or rewrites a dependency edge. The edge
table is append-only. A recorded edge is a permanent statement that this
task, at this id, was declared to depend on that upstream.

This is what makes R1 checkable at all. A record that can be rewritten is
not evidence.

### R3 — dynamic dependencies are append-only and inherited

A task may yield dependencies at runtime. That set only ever grows, and it
grows for a structural reason rather than by rule: **a task cannot discover
its next batch until the previous batch is complete**, because it is
suspended until then. There is no divergence to detect, only additions.

So dynamic edges need no check and no retraction:

- A task's registered dynamic dependencies are **inherited** by every build
  that references it, whichever build discovered them.
- They must complete before the task resumes.
- **Any** build may schedule them — whether the build that discovered them
  is still running, has been cancelled, or has failed.

If a later build's code cannot run the inherited set — it fails to
deserialize, or errors — that is a loud failure, and the remedy is again to
bump the version of the task that yields them, so it yields a different set
under a different id.

If a later build's code _can_ run the inherited set but would not have
chosen it, the set is run anyway. That is wasted work, silent, and bounded
by the size of the divergence. There is no cheap defence against it and it
does not fail the build.

**The one shape where that is not merely wasteful**: if a member of the
inherited set can no longer be built at all — its source is gone, or it
fails deterministically — the parent stays gated on it, because nothing
retracts it and re-yielding does not remove it. The escape is a version
bump on the parent, which moves every id below it. This is the lenient half
of the design failing worse than the strict half: a refused static
declaration gives you a 409 and a remedy at trigger time, while this gives
you a build that stops. Filed rather than solved.

### R4 — the comparison stops at completeness

Discovery does not walk past a complete task, and no declaration is compared
for one. A complete task is not going to be built again, its upstreams are
assumed complete with it, and traversing beyond it to verify a promise that
has already been kept is expense without purpose.

The consequence, stated plainly so it is not discovered later: **a
divergence above a complete task is never detected.** That is accepted, not
overlooked. It also self-corrects where it matters — if the task ever
becomes incomplete again (a retry, a deleted target), the next build walks
into it and the comparison happens then.

### R5 — retraction is an operator action, never a build's

R2 says no _build_ retracts an edge. There still has to be a way out, for
the cases where the record is simply wrong: an experiment that registered a
DAG nobody wants, a `requires()` that was buggy and never ran, an
environment carrying edges from code that no longer exists.

That is an **explicit operator action**, available from the CLI and the UI,
taken outside any build: delete registered tasks and edges — optionally only
the incomplete ones, optionally the whole upstream tree of a task. It
refuses while a live build would be affected, and it is recorded as an event
so the act is auditable.

The rows are genuinely deleted rather than tombstoned. A tombstone the edge
readers must filter puts a second predicate on `has_incomplete_upstream`,
which is re-read every few seconds per active build, and keeping that query
simple is one of the things this design buys. Deleting is also the more
honest model: the operator is saying "this should never have been recorded",
which is a different claim from "this was superseded".

## Why static and dynamic are treated differently

The two rules look asymmetric and the asymmetry is easy to misread as
incidental. It is not, and the reason is worth stating precisely because an
earlier version of this design got it wrong:

> **Declarations are not serialised; executions are.**

Any number of builds can declare a task's static upstreams at the same
moment. Nothing orders them, and nothing makes one of them the owner. So a
static edge has no natural owner, and scoping it to a build or an attempt is
not just costly — it is unsound. (See the abandoned path below: two builds
gating one task on different upstreams produce one target, and whichever
build wins the claim silently violates the other's declared contract.)

A dynamic edge is asserted by an **execution**, and the execution claim
guarantees exactly one of those at a time for a given task. "The current
attempt" is therefore globally well defined, and the edges accumulate in a
single, totally ordered sequence. That is what makes append-and-inherit
sound without any check at all.

One rule follows from there being no owner; the other from there being
exactly one.

## What this costs

**Changing `requires()` costs a version bump, and the ids below move with
it.** A task's id is a parameter of its downstream tasks, so bumping it
gives every descendant a new id, and a new build of that DAG recomputes
them. Nothing is lost — assets materialised under the previous version
remain complete and loadable, and code can reference them explicitly — but
_automatic_ reuse stops at the bump. Refactoring `requires()` near the root
of a DAG is therefore a real cost, and it is the price of the contract
rather than an implementation artifact.

**A historically inconsistent environment surfaces its inconsistency at
upgrade.** Before this rule, edges accumulated without ever being removed,
so a task whose `requires()` changed at some point carries the union of
every set ever declared. The first build to declare the current set differs
from that union and is rejected. This is bounded: it can only affect tasks
that are **incomplete** (R4), it is a one-time cleanup per affected task,
and R5 is the tool for it.

**A build is rejected whether or not anybody else is running.** Stricter
than arbitrating between live builds — and simpler, because it removes every
question about time.

## What this buys

Set against the abandoned design below, the concrete wins:

- **No temporal reasoning anywhere in the rule.** No "is that build still
  alive", no "has it drained", no "was this edge current when that was
  read". The check compares two sets.
- **No schema change.** No `superseded_at`, no migration, no partial index,
  no second predicate on the frontier's hot path.
- **Plan closure is unchanged**, and the dynamic half of the design is
  already the behaviour on `main` — append-only edges, closure admitting
  incomplete upstreams into a later build's plan, gating on all recorded
  edges. There is nothing to build for R3.
- **One new rule, in one place**: compare at registration, reject on
  difference.
- **The failure is loud, at the trigger, before anything runs**, and names
  the task, both sets, and the remedy.

## Consequences for the user

| You did this                                    | What happens                                                                                                                                                                                                                                     |
| ----------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| Changed `requires()`, bumped the version        | Works. New id, new DAG below it, previous assets still loadable by explicit reference.                                                                                                                                                           |
| Changed `requires()`, did not bump              | Build rejected at trigger, naming the task, both sets, and the remedy. Nothing is written.                                                                                                                                                       |
| Fixed a crash in a task                         | No bump needed — a task that failed produced no target and promised nothing. Redeploy, re-trigger, it completes.                                                                                                                                 |
| Fixed a task that succeeded with wrong output   | Bump. That is exactly how the bad target and everything downstream of it are invalidated.                                                                                                                                                        |
| Changed a dynamic fan-out without bumping       | The previously registered set is inherited and completed; your new set is appended. Usually just wasted work — but if a member of the old set can no longer be built at all, the parent stays gated on it and the only escape is a version bump. |
| Registered a DAG you did not want, never ran it | Operator delete (R5), or bump.                                                                                                                                                                                                                   |
| Two builds, same code, sharing tasks            | Nothing happens. Identical declarations never differ, and the claim serialises execution as always.                                                                                                                                              |

## Compatibility with the claims design

This note sits on top of
[execution-claims-and-liveness.md](execution-claims-and-liveness.md) and
does not disturb it:

- "A build is a request for a set of root tasks to be materialised, not an
  owner of the tasks that materialise them" is what R3 depends on: any build
  may schedule an inherited generation.
- "A build acts on everything in its plan, whatever build last touched it"
  is the mechanism — a cancelled child of an abandoned fan-out is reset and
  run by the next build that needs it.
- "What stays build-scoped is authority to revoke" is untouched.
- Plan closure's rule — every dependency of the roots not complete at
  discovery time, pruned at complete tasks — is R4 for the plan, and R4 here
  is the same boundary applied to the comparison.

**One known gap, inherited rather than introduced.** Closure runs once, at
registration. A build that registered _before_ a concurrent build's worker
yielded a dynamic dependency does not have that dependency in its plan, and
the attempt budget stops it from scheduling something out-of-plan. That
build waits while the children are running, and fails naming them if their
owner dies. The remedy is a re-trigger, which re-closes the plan and
inherits them properly. So R3's "any build may schedule them" holds for any
build that registers after the yield, and degrades to "fails loudly with an
actionable remedy" for one that registered before. The claims note describes
this state and calls it benign; nothing here makes it worse.

---

# The abandoned path

Kept because it is the framing a reader is most likely to arrive at
independently, and because every step of it was defensible in isolation. It
took six rounds of review and an independent design review to establish that
the root was wrong rather than the details.

## The abandoned principles

**A1. "A task id promises world state, not provenance."** The upstream DAG
is deliberately not part of the id, so a task may change its upstreams and
keep its id as long as the output is the same. The motivating case: change a
partition size, get a different set of fan-out children, same final output,
no reason to invalidate everything downstream.

**A2. "A dependency edge is evidence asserted by an act, and stops counting
when its source is withdrawn."** A static edge is declared at registration
and the latest declaration is authoritative, superseding what it omits. A
dynamic edge is discovered by one execution attempt, and a transition
beginning a new attempt retracts it.

**A3. "Two live builds disagreeing about a task's static upstreams is a
conflict, not something to reconcile."** Because `U1` and `U2` are different
tasks with no claim between them, nothing serialises two builds
materialising one downstream over two upstream DAGs. So refuse the newcomer
by default, with an opt-in flag to cancel the incumbent and take over.

A1 is the appealing one. It is _generous_ — it lets you refactor internals
without paying for a rebuild — and generosity is what makes it wrong: the
system ends up asserting something about a task that the user never
guaranteed.

## Why it failed

### Retraction makes every question a question about time

A2 requires the registry to decide **when** an edge stops counting. That is
a temporal question, and every hard problem in the implementation descended
from it: is that edge still current, is that attempt still the current one,
did the claim move between the read and the write, is the other build still
alive.

Six review rounds produced thirteen findings. Three of them were defects
introduced _by_ fixes to earlier findings. Nearly all shared one shape:
**confusing a historical fact (what this build started, or declared) with a
current fact (what the task row says now)**. That confusion is not a
discipline problem. It is created by retraction — without it, there is no
"was this edge current at time T" to get wrong.

### Superseding deletes another build's gate

The sharpest technical objection, and it is fatal to A2 on its own. Edges
are environment-global. If build A supersedes `(U1, R)` and records
`(U2, R)`, then **build B's gate on `U1` disappears**. `U1` is still in B's
plan and B will build it, but nothing orders it before `R` any more. Once
`U2` completes, `R` is actionable for B, and B executes `R` with code that
reads `U1` while `U1` is incomplete.

That is a wrong-output bug, not waste. It is also an argument about **global
edge storage**, not about concurrency — which is why the conflict check of
A3 was never the natural fix for it.

### A3 contradicts A1, and needs a liveness test that does not exist

A1 says the two declarations produce the same output; A3 then refuses
concurrency on the grounds that producing it twice is waste. But `R` is not
built twice — the claim already prevents that. What is duplicated is the
upstream divergence, which A1 says is acceptable. So A3 spends a real
user-visible failure to prevent a cost its own premise blesses.

Worse, "is another build in the way" needs a liveness test, and the only one
available is `Build.latest_status == RUNNING` — precisely the inference
[execution-claims-and-liveness.md](execution-claims-and-liveness.md) exists
to reject, with no automatic build-level reaper behind it. One orchestrator
preempted without a terminal event leaves a zombie that refuses declaration
changes on every task it ever touched, indefinitely.

### The escape hatch violated the principle it escaped

`cancel_conflicting` cancelled the incumbent and retried immediately. But a
cascade cannot stop a container — it releases claims and asks the cancelled
build to stop what it started, and that build's next tick does it. So the
takeover could start a second execution of a task while the first was still
running: **the exact outcome A3 existed to prevent, reached automatically by
A3's own remedy.** Nothing records a stop, so there was no drain to wait on.

### The carve-outs were patches for a bug one layer down

A completed task had to be exempted from the conflict check, because every
build sends a declaration for every complete task in its closure. That is
true — but only because the registry payload re-evaluates `task.requires()`
for every task in the chunk, undoing the prune discovery had just performed.
The server-side carve-out was a patch for a client-side bug. Under the
present design the client-side fix is load-bearing and the carve-out
disappears.

## Alternatives considered and rejected

**Last-declaration-wins, unconditionally.** Deletes another build's gate, as
above. Wrong output.

**Per-build static edges.** Unsound, and this is the cleanest disproof of
the whole family. If `T → [U1]` in build A and `T → [U2]` in build B, then
`T` runs when `U1` is complete for A and when `U2` is complete for B — but
`T` has one claim and one target. Whichever build wins produces the target,
and the other build proceeds as though its own contract held when it did
not. The hazard is not removed, only relocated.

**Union while contested, prune when uncontested.** Insert the additions,
keep the drops, so both builds are gated on `U1 ∪ U2` and neither can run
`T` early. Correct on gating, but it gates a build on a task its plan
closure never admitted — and a build gated outside its plan cannot schedule
its way out. It trades a refused build for a possible deadlock.

**Attempt-scoped dynamic edges.** Sound — the claim provides the unique
owner that static edges lack — but unnecessary. It only buys something if a
reset should discard a generation, and under R3 it should not.

## What survived

- The `dependency_task_ids: list[str] | None` distinction. A list is a
  declaration; **null is not a declaration** and leaves recorded edges
  alone. An out-of-band caller that does not know a task's dependencies must
  not silently erase them, and one list cannot carry both meanings. Under
  the present design this is what lets discovery decline to declare for a
  task it pruned at.
- The refusal message. It names the task, both sets, and the remedy, because
  a build refused for this reason looks to whoever triggered it exactly like
  stardag declining to run.
- Everything in
  [execution-claims-and-liveness.md](execution-claims-and-liveness.md) about
  cancel authority and execution refs. That work was orthogonal and is
  unaffected: an execution ref is not a claim, authority to revoke is
  build-scoped, and the server cannot stop an execution.
