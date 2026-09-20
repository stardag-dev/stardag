# Executions as records

Why an execution — one container, running one task, started by one build,
from start to end — gets a row of its own rather than being reassembled
from the event log at each point of use, what that row is allowed to mean,
and the two things it deliberately does not do.

Written after a change to the cancel path took five review rounds, where
what the rounds had in common turned out to be more informative than what
any of them found.

> **Written before the change exists.** A reader on `main` will find the
> reconstruction described below, not the table. The note is here first
> because the decisions it records — what the row is allowed to mean, and
> the two things it must not do — are the part worth reviewing, and because
> the same reasoning has now been re-derived several times without being
> written down.

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

## Why the reconstruction is load-bearing rather than incidental

Three sources claim to know about an execution, and they disagree by
design.

| Source        | What it knows                           | Why it is not enough                                                                                                                                                       |
| ------------- | --------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| the task row  | current status, owner, executor columns | environment-global and overwritten by every writer; after a takeover it names somebody else's execution, and every start sets _or clears_ the executor columns             |
| the event log | history, append-only, per build         | the execution has to be ranked out of it, and the rule is subtle — the latest start decides the backend, the newest reference from _that_ backend identifies the execution |
| the backend   | whether a container is actually alive   | reachable only through a probe, and a stop cannot be confirmed                                                                                                             |

Two properties of the surrounding design, both deliberate, keep the
reconstruction on the critical path rather than at the edges: the claim
records no third-party-evaluable liveness beyond its expiry, and the
server cannot stop anything — it can only record that a claim is gone. So
the code compensates with heuristics, and heuristics compose badly.

There is also a testing asymmetry that let all of this run. The unit-test
fake keyed its listing on _current ownership_, which is the same
conceptual mistake the endpoint had, and it stored at most one reference
per build and task, so a second execution of one task overwrote the first.
A double that shares the code's misconception cannot falsify it. Only the
live tier could, and it did, twice, on defects every unit test passed.

A smaller fact makes the shape of the gap concrete: a reactive execution
emits **three** `TASK_STARTED` events — the claiming start, the tick's
reference-recording start, and the worker's own self-report. "The start"
is not one event, and the thing all three describe has no name.

## An execution is not a claim, and the table must not pretend otherwise

This is the invariant the rest of the design falls out of, and it is the
one a reader will get wrong first.

> The claim says who may run the task next. An execution row says what one
> build set running. **Several rows may be open for one task at once**, and
> after a takeover that is the correct state, not a corrupt one: build A's
> container runs on while build B holds the claim.

So there is no uniqueness constraint over the open row for a task, and
"the task holds this execution" is not "this row is open". It is a
pointer: `tasks.latest_execution_id`, written by the same fold that grants
the claim, so no reader can pair a fresh claim with a stale execution.

Representing two live executions of one task is the point. The old
reconstruction could not say it, which is exactly why a cascading cancel
could release a claim, let a neighbour take the task within seconds, and
leave the first container unreachable by any query about the present.

## The design: a row, and a pointer from the claim to it

A row carries the build that started the execution, the task it runs, the
executor and reference that address it, the descriptive metadata the UI
deep-links from, when it started, and — when something reported it — when
and how it ended.

Two questions that used to be one become two indexed reads. _What is mine
to stop?_ is this build's rows with no end recorded. _Does the task still
hold this execution?_ is a pointer comparison. The first is about the
past and is build-scoped; the second is about the present and is
environment-global. Conflating them is the original mistake, and once they
are separate columns they cannot be conflated by accident.

What this deletes: the two window functions and the join on latest
backend, the ranking rule and the several paragraphs explaining why
neither half of it alone is right, the whole-history-per-call scan behind
the conditional cancel, and the fake's special case — which then has a
thing to model instead of a rule to re-derive.

### What ends a row, and what does not

A row ends on **evidence that the container is gone**, and on nothing
else.

- A worker's own end-of-execution report — completed, failed, suspended —
  ends it. The worker is the container; its report is the evidence.
- A cancel does **not**. A cancel is a request to stop, not a report that
  anything stopped, and a task this build cancelled is precisely one whose
  container it still has to go and kill. This is the absence the listing
  was built around and it survives unchanged.
- An interruption or a preemption does **not**. The platform ended one
  attempt and the backend may be restarting the same call under the same
  reference, which is by construction still the same execution.
- A retry ends it: the build has given up on that execution, and the
  container, if any, is the drain's problem rather than the claim's.
- A probe that finds the backend's call gone ends it. Which observations
  count as that is carried by an explicit flag on the report rather than
  inferred from the event type, because the same platform event is
  classified differently depending on who sees it first — the subject of a
  separate change, and the reason this is a flag rather than a rule here.

### Retirement is the pointer moving, not the row closing

The tempting simplification is that recording a new start for a task ends
the previous row, so that retirement becomes data. It is half right, and
the wrong half is the expensive one.

Ending A's row because B started would hide A's still-running container
from A's own drain — which is the incident the listing exists to prevent,
reintroduced through a tidier-looking door. Ending A's _own_ earlier row
when A retries has the same defect one build in.

So retirement of the _current_ execution is the pointer moving, and
retirement of the _row_ is evidence arriving. Today's code has one
mechanism for both questions, which is why answering one of them correctly
kept breaking the other.

## The claim needs an identity before it has an execution to name

The engines claim before they spawn, because the claim and any
concurrency-limit slots have to be taken in one transaction and a denied
task must not occupy a worker. The reference is the spawn's own id, so it
does not exist yet. That leaves a claiming start with nothing to identify
itself by.

The consequence is not theoretical. The registry client retries a POST
whose response never arrived, so a claiming start that _succeeded_ can be
delivered twice; the second delivery is refused by the state its own first
attempt created, and a refusal is indistinguishable from losing a race to
somebody else. The worker stands down from a task it holds the claim on,
and the task is claimed and not running until the claim expires. That is
the worst outcome available at that endpoint.

The retry-policy comment in the registry client already names what is
missing, in its own words: an identity the claim can carry before it has
an execution to name. The execution row is that identity, on one
condition — **the client mints the id**. A server-minted id is one the
retry does not have, since the lost response is the whole failure.

### What a second delivery gets

- **The same id, and the task still points at it** — granted. It is the
  same execution asking again, which is what a retry is.
- **A different id from the same build, claim live** — refused, exactly as
  another build is refused. Two attempts of one build are legitimately
  distinct executions, and granting on the build alone would hand out real
  double-claims.
- **No id at all** — the previous rule, unchanged: the `(executor,
executor_ref)` pair, with a request naming neither always refused. An
  SDK predating this sends nothing, and version skew must not become a
  behaviour change.

The awkward case an earlier sketch could not answer is answered here
without a caveat. If the spawn succeeded but the start that would have
recorded its reference was lost, a rule of "same build, and no execution
recorded yet" would grant the re-ask while the execution ran — the double
run the claim exists to prevent. With a minted id the re-ask is granted
because it genuinely _is_ the same execution, and a fresh attempt carries a
fresh id and is refused. The identity does the work that a NULL was being
asked to do.

## The silent death needs no expiry on the row

A worker that dies without reporting leaves a row with no end recorded,
which looks like the unbounded-claim problem the claim itself had. It is
not, and the difference is worth stating because the instinct to add a
second expiry column is strong.

Follow the consumers. The pointer answers which execution the task holds.
The claim's own expiry bounds how long a vanished holder can deny the task
to everyone else, and that is the harm an expiry exists to bound. The only
consumer of "no end recorded" is the drain, and cancelling is idempotent
at every backend stardag supports, so a reference whose container is
already gone costs one no-op call. Open rows are bounded by the build's
task count, and the build is terminal by the time it drains.

> The row is evidence. The claim is authority. Only authority needs an
> expiry, because only authority can be held against somebody.

Adding an expiry here would buy a shorter drain list and would cost a
second clock that has to agree with the first — and the claim note already
records what happens when two mechanisms answer one question with
different defaults.

## Both engines, or the split reappears

The reactive engine is not the only one that claims before spawning; the
resident engine does the same, by the same argument, with the same
consequence on a retried claim. Minting an id in one and not the other
would fix half a bug and leave the shape that produced eight
reconstructions.

So every start carries an execution id — claiming or not, reactive,
resident or sequential. "Some starts have rows and some do not" is a
distinction every future reader would have to rediscover. What the
resident engine does not get is a drain: it holds its handles in memory
and has no listing to consume.

## Migration: no backfill, except for the builds in flight

An absent row means "no execution known", which is what the reconstruction
returned for the same history, so nothing has to be reconstructed into the
table for correctness.

With one exception, and it is the same exception the scope-keyed
dependency migration made for the same reason. A build in flight across
the deploy has starts in the event log and no rows, so its drain would
find nothing to stop and leak every container it started — and an older
SDK's cancel, which still names an execution by `(executor,
executor_ref)`, would resolve against an empty table. So one open row per
build and task is written for non-terminal builds, from the ranking query
being retired, at the moment it is retired. Terminal builds get nothing;
their executions are history.

The older cancel parameters keep working against a newer server, because
the compatibility direction is one-way by contract. They are answered by a
single indexed read against the table rather than by the ranking query, so
the rule and its commentary are genuinely gone even though the parameters
that used to need it remain.

The task row's executor columns stay too, and that is not hedging. They
are read by the task explorer, the build view, the search results and
three CLI commands as the _display_ coordinates of whatever is running
now, which is a question about the present that the task row is the right
place for. Only the sites that make a _decision_ move to the table.

## Corrections — things that keep being misread

### 1. "It is just a claim table normalised"

It is the opposite. A claim table would have one live row per task; this
has as many open rows per task as there are builds that started
executions of it, and the case with two is the case the design exists for.
Any constraint enforcing one open row per task would re-create the bug.

### 2. "Recording a new start ends the previous row"

Written into the original proposal, and wrong in the case that matters —
see "Retirement is the pointer moving". A start is evidence about the
execution it names and about nothing else.

### 3. "The listing should answer from the task row now that there is one"

The listing was never answering a question about the present, and the
table does not change that. A cascading cancel releases claims so the next
build can take the tasks over, and from that moment the task row names the
new execution while the old one, still running, is unreachable by status.
The listing reads this build's rows because the past is what it is asking
about.

### 4. "A claim with no reference is an execution with missing fields"

It is an execution whose reference does not exist yet, which is a
different thing and a reachable state with its own handling: a scheduler
that dies between claiming and spawning leaves exactly that shape, and the
claim's expiry — not a locally configured guess — is what eventually
resolves it. Giving it a row makes the window visible for the first time.
Listing it for the drain would still be wrong: there is nothing to cancel.

### 5. "The cursor has to be keyed on the task"

It had to be, because the listing's rows were derived — a newer start
re-ranked the same logical row onto the far side of the cursor and it was
then skipped entirely, and on a terminal build, whose drain has no second
chance, that is a container left running. A real row's start time never
changes, so it cannot be re-ranked, and the cursor can order by it. The
constraint was a property of the reconstruction, not of the problem.

## Open questions

- Should an _attempt_ simply be an execution? It is currently derived by a
  window function over consecutive start events in a build, with a Python
  twin that must agree with it — a third reconstruction of a nearby
  question, and one this table is shaped to answer. Not changed here,
  because the retry and resumption budgets are counted from it.
- Should the reference-less claim window be listed anywhere? It has a row
  now, but nothing can act on it except by waiting out the claim.
- Does the row make the backend probe cheaper to reason about, or merely
  cheaper to reach? The probe remains better evidence than anything stored,
  and nothing here changes that.
