# Registry v2 research notes

These are the point-in-time research notes the v2 design rests on, written
2026-09-23 against `stardag-dev/stardag` at commit `f96fb751` (then `main`).
Each is a snapshot, not a living document: read [../design.md](../design.md)
for the design and [../plan.md](../plan.md) for current status.

| File                                                                     | Subject                                                                                                                                                            |
| ------------------------------------------------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| [v1-server-logic.md](./v1-server-logic.md)                               | The registry server's HTTP API, registration transactions, the runnable rule, claims and executions, rollover, and build status as actually implemented            |
| [v1-sdk.md](./v1-sdk.md)                                                 | SDK-side identity, discovery and registration, build config and scope, deployment and Modal integration, the registry client surface, and the retired pickle store |
| [v1-schema.md](./v1-schema.md)                                           | Inventory of the v1 schema and the seven claims checked against models and migrations                                                                              |
| [v1-design-notes-and-invariants.md](./v1-design-notes-and-invariants.md) | Recorded invariants and architectural rules from prior design notes, checked against the v2 decision, plus the scenario harness shape and open questions           |
| [v1-ui.md](./v1-ui.md)                                                   | Sizing the UI's dependence on v1 entities (task id, scope, claims, executions) against the v2 redesign                                                             |
| [adjacent-issues.md](./adjacent-issues.md)                               | Linear state of every issue adjacent to STA-105, and where the design summary was out of date                                                                      |
| [review-2026-09-23.md](./review-2026-09-23.md)                           | Adversarial review of the design draft: thirty findings, ranked; dispositions are in [../decisions.md](../decisions.md)                                            |
