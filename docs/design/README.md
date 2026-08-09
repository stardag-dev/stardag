# Design notes

Maintainer-facing notes on _why_ parts of stardag are built the way they
are — the constraints in play, the alternatives that were rejected, and the
known costs of what was chosen.

These are deliberately **not** part of the published documentation site
(`docs/docs/`, see `mkdocs.yml`). User docs describe how to use stardag; these
describe trade-offs a contributor needs before changing something, including
things stardag does _badly_ and knows it.

A note earns its place here when a design keeps getting re-litigated, or keeps
being misread in the same way. If you find yourself explaining the same
subtlety a second time, write it down here instead.

| Note                                                                 | Subject                                                                                       |
| -------------------------------------------------------------------- | --------------------------------------------------------------------------------------------- |
| [execution-claims-and-liveness.md](execution-claims-and-liveness.md) | How "is this task running right now?" is modelled, and why it is a status rather than a lease |
