# Review guidance for this repository

## PRs targeting the `v2` branch

`v2` is a long-lived feature branch for the registry v2 core-entities
re-design. It merges to `main` once, when the line ships. Until then, PRs
against it are **incremental by design**:

- The scope of a PR is what its description says under **"In this PR"**.
  Anything listed under **"Not in this PR, by design"** is deferred to a
  named work package in `docs/design/registry-v2/plan.md` and is not a
  finding. Do not flag missing CLI commands, UI views, documentation pages,
  release notes or compatibility handling unless the description claims
  them.
- The design is `docs/design/registry-v2/design.md`; decisions and their
  rejected alternatives are in `docs/design/registry-v2/decisions.md`. A
  finding that proposes a rejected alternative should engage with the
  recorded reason, not restate the alternative.
- v1 → v2 is fully breaking with no data migration and no compatibility
  shims. Missing backwards compatibility is not a finding.
- Files under `docs/design/registry-v2/research/` are point-in-time notes
  and are not maintained; do not report staleness in them.
- Tests marked `xfail(reason="v2: <work package>")` are expected failures
  for mechanisms that have not landed yet; they are tracked in `plan.md`.

What is worth flagging on a `v2` PR: a behaviour that contradicts
`design.md`; a schema constraint or transaction that does not enforce an
invariant the design states; a scenario in `design.md`'s checklist that the
PR's code touches without a test at the tier the table names; a test in the
"must still hold" list of `plan.md` that the PR weakens or deletes; a
violation of the engineering rules in `plan.md` (route logic outside a
service, a second copy of a transaction, a module crossing the size ceiling,
a JSON flag that changes query semantics).
