# Execution identity, and the record that was not built

Why an execution — one container, running one task, started by one build —
carries an identity minted before it exists, what that identity is allowed
to decide, and why the table originally designed to hold it is not being
built.

Written after a change to the cancel path took five review rounds, where
what the rounds had in common turned out to be more informative than what
any of them found.

> **The `executions` table is not planned.** What shipped is one column,
> `tasks.latest_execution_id`. The table, its backfill and the rewrite of
> the executions listing were all driven by **automated cancellation** —
> a short-lived scheduler reaching into containers it did not start — and
> that feature is being withdrawn in favour of cooperative cancellation
> (a worker checks whether it is still wanted) plus a human-driven stop
> command. With nothing left that has to reconstruct "what is mine to
> stop", the record that reconstruction needed stops being worth its
> cost. The design is kept below because the reasoning is the point, and
> because the identity is a strict prefix of the table: if the table is
> ever needed, this column is its key. See STA-78 for the decision.

> **And the identity itself ships in two halves.** What lands with this
> note is the **claim** half: the caller mints an id before it claims,
> sends it with the claiming start, and the registry uses it to tell a
> retried delivery from a second attempt. Nothing yet carries that id
> into a container. The **worker** half — forwarding it into the spawn,
> the worker echoing it on its own reports, and the server refusing a
> start that names an execution the task no longer runs under — moves
> to STA-79, where cooperative cancellation needs the identical
> plumbing (a worker that knows its own execution and can ask the
> registry about it) and gives it a second consumer. Sections below that
> describe the worker half describe STA-79, not what is in the tree.
> Building that protocol twice, half in each issue, is how it ends up
> with two owners.

It is a companion to
[execution-claims-and-liveness.md](execution-claims-and-liveness.md),
which this note assumes: the claim, its expiry, and the reason authority
to revoke is build-scoped.

## One question, asked in eight places, answered eight ways

Every one of these sites decides some version of _which execution is
running this task, and is this reference still it?_

- the conditional cancel's lookup (`_latest_started_execution`);
- the `GET /builds/{id}/executions` ranking, two window functions deep;
- the `held` condition that gates a conditional cancel;
- the claim-retry identity test (`_claim_is_this_same_execution`);
- the authority rule for an end-of-execution report
  (`_reports_on_the_current_execution`);
- its per-build twin inside the status replay (`_replay_report_applies`);
- the cancel drain's `stopped` set, keyed on `(executor, executor_ref)`;
- and the unit-test fake that models the listing.

Several of the review findings were regressions from fixes to earlier
findings in the same change. Twice the concept was re-derived within one
sitting and the two derivations disagreed: one scanned starts within a
backend while the other partitioned by task alone, so a task could vanish
from the listing entirely and its container be left running.

That is not a discipline problem. It is what a missing abstraction looks
like from the inside.

**Five of those eight are cancellation machinery**, and they are the five
being deleted rather than unified. The three that remain — the two
authority rules and the claim-retry test — are all about the _present_:
does this request concern the execution the task is running under right
now? That question needs an identity, not a history, and it is the part
this note's design survives into.

## Why an identity was missing in the first place

Both engines claim a task **before** spawning it. The claim and any
concurrency-limit slots have to be acquired in one transaction, so that a
denied task never occupies a worker, and that transaction happens while
the execution is still hypothetical. There is therefore no executor
reference at claim time and never was.

That is the whole gap. The task row's executor columns describe whoever
holds the task now and are set _or cleared_ by every start; the event log
holds the history but has to be ranked, and the ranking rule is subtle;
the backend knows whether a container is alive but only through a probe,
and a stop cannot be confirmed. None of the three can say "this request
is about the execution that is running", because until the spawn returns
the execution has no name.

A smaller fact makes the shape concrete: a reactive execution emits
**three** `TASK_STARTED` events — the claiming start, the tick's
reference-recording start, and the worker's own self-report. "The start"
is not one event, and the thing all three describe had no name.

Two defects follow directly, and both are closed by giving it one.

**A retried claiming start could not be told from a second attempt.** The
registry client retries a POST whose response was lost, so a claiming
start that succeeded can be delivered twice. Refused, the second delivery
tells a worker that somebody else holds the task — a correct reason to
stand down, and it does, while itself holding the claim. The task is then
claimed and not running until the claim expires, which is the worst
outcome available at that endpoint. The build id cannot separate the two
cases, because two attempts of one build are legitimately distinct; the
reference cannot, because there is not one yet.

**A worker's own start could evict the live holder.** That start is
non-claiming and was folded in unconditionally. A preemption brings the
claim's expiry forward to a short restart grace, deliberately, so that a
restart which never arrives becomes visible in minutes; if the restart is
merely late, the claim lapses, a neighbour claims the task and spawns,
and then the original restart lands and takes the task back. Two
executions of one task, which is what claims exist to prevent.

## An execution is not a claim

The invariant the rest of the design falls out of, and the one a reader
will get wrong first.

> The claim says who may run the task next. An execution identity says
> what the task is running under now. **A container whose execution is no
> longer named here may well still be running** — the server cannot stop
> anything — so nothing here concludes that a container is gone.

This is why the identity is a single column on the task row rather than a
lifecycle to be tracked. It records the present, and the present is
exactly one execution. What the _past_ would have needed — several
executions of one task open at once, which after a takeover is the
correct state — is the record that is not being built.

## What the identity decides, and what it refuses to

Minted by the caller before it claims. **Today it is sent with one
call, the claiming start**; repeating it on the start that records the
reference, on the worker's own self-report and on its interruption and
preemption reports is the worker half, and arrives with STA-79.

**Client-minted, necessarily.** A server-minted id would be one the retry
does not have, since a lost response is the entire failure being
answered.

On a **claiming** start, the id is the retry test: the same id from the
same build is the same execution asking again and is granted, a different
id while the claim is live is arbitrated exactly as before.

On a **non-claiming** start, the id would be the supersession test: a
start naming an execution the task demonstrably no longer runs under is
refused, on three conditions each load-bearing — a live claim (a task
nobody holds is up for grabs, and an ordinary retry must not be
refused), both identities present, and the ids differing. That rule is
STA-79's, and is not in the tree: without the worker half no start
carries an id to judge, so there is nothing for it to decide.

**Absence is never a mismatch**, in both directions of a rolling
deploy. A caller that mints no id falls back to the
`(executor, executor_ref)` pair. A task claimed before the column existed
has no opinion to contradict. Refusing on a missing value would turn a
version skew into tasks that look unstarted, which is worse than the bug
being fixed.

A Modal preemption restarts the input under the **same call id**, so
once the worker half exists a legitimate restart re-sends the same
execution id and matches. That is not a special case bolted on; it is
what "the same execution" means.

**The fold preserves rather than clears**, and that is load-bearing
rather than tidy. A start that names no identity leaves the recorded
one alone. The tick records a second, ref-bearing start as soon as the
spawn returns and that start names none; clearing on it would drop the
id moments after the claim recorded it, so a retry arriving even
slightly late would be read as a second attempt and refused — the exact
failure the column exists to close. `TASK_RETRIED` is the reset, because
a retry genuinely is a new attempt.

### Why a missing current identity is treated differently from a missing reference

The two look symmetric and are not, which is worth stating because the
asymmetry reads like an oversight.

A missing current **reference** is not a wildcard: a replacement's
claiming start clears the reference before its spawn records the new one,
so treating NULL as "matches anything" would make the whole
acquire→spawn gap accept a dead execution's report.

A missing current **identity** is treated as no opinion, because that gap
does not exist for it. A replacement mints its id _before_ claiming and
the claiming start carries it, so a task running under a replacement
always has one. NULL therefore means the running execution predates the
identity, and the honest answer is to fall back to the behaviour of that
era rather than refuse a report the server cannot evaluate.

## The record that was not built, and why it would have been needed

Recorded so the next person does not re-derive it from scratch.

The table was `executions(id, build_id, task_id, executor, executor_ref,
executor_metadata, started_at, ended_at)`, keyed by the same client-minted
id this column now holds. Its point was never the identity — it was the
question the identity cannot answer: **what did this build start, that it
may still have to stop?**

That question is about the past, and the past is where a task row cannot
help. A cascading cancel releases the claims a build held so the next
build can take those tasks over — which is correct — and from that instant
the task row names the new execution while the old one, still running, is
unreachable by any query about the present. Hence the ranking over the
event log, in five places, which had to agree.

Three decisions from that design are worth keeping, because each is a
place the obvious answer is wrong:

- **Several rows open for one task is the correct state**, not a
  corruption, so there could be no uniqueness constraint over the open
  row. Representing the takeover is the entire point.
- **Retirement is the pointer moving, not the row closing.** "Recording a
  new start ends the previous row" is the tempting simplification and it
  is wrong in the case that matters: ending A's row when B starts hides
  A's still-running container from A's own drain, which is the incident
  the listing exists to prevent, reintroduced through a tidier door.
- **The row needed no expiry of its own.** Following the consumers, the
  claim's expiry bounds the harm and the only reader of "no end recorded"
  is a drain that cancels idempotently. The row is evidence; the claim is
  authority; only authority needs an expiry, because only authority can
  be held against somebody.

What withdraws the need for all of it is the decision to stop cancelling
preemptively. A worker that checks whether it is still wanted needs no
external record of what is running — it _is_ what is running, and it
already knows its own identity. A human running a stop command reads the
build's live executions off the task rows while the claims are still
held, which is exact at that moment and needs no ranking at all. Neither
path asks the question the table existed to answer.

## Corrections — things that keep being misread

### 1. "The identity is just the executor reference, earlier"

It is earlier, and that is the whole difference rather than a detail. The
reference exists only after the spawn returns, and both defects above
happen in the window before that. A design that waits for the reference
cannot close either.

### 2. "Refusing a superseded start could strand a task"

Only if absence were treated as a mismatch, which is why it is not. The
refusal requires a live claim _and_ two present, differing identities. A
worker with no id, a task with no id, a lapsed claim, a task that is not
running — all take the path they took before the column existed.

### 3. "A 409 loses the event, so something will be miscounted"

The opposite, and this is the lesson of the interruption work applied
early: nothing is written, so no attempt is spent and no
recorded-but-refused bookkeeping is needed. It is the _silent_ refusals
that cost, because every consumer of the event has to be found again.

### 4. "The identity means a task can only ever have one execution"

It means a task is _running under_ one execution. Others may still be
alive; the server cannot stop anything, and a container whose execution
is no longer named is not thereby dead. Reading the column as a liveness
claim is the same mistake as reading the task row as one.

## Open questions

- Should an _attempt_ be an execution? It is currently derived by a window
  function over consecutive start events in a build, with a Python twin
  that must agree with it — a nearby reconstruction that this identity is
  shaped to answer. Not changed here, because the retry and resumption
  budgets are counted from it.
- Does cooperative cancellation want the identity exposed to task code, so
  a long-running task can ask "am I still wanted?" without the framework
  asking for it at fixed checkpoints?
- If the table is ever revived, is the drain still the only consumer that
  wanted it? That was true when it was costed; it is worth re-asking
  rather than assuming.
