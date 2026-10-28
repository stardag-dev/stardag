# Stardag Skill Bundle for Claude Code

A comprehensive Claude Code skill that teaches Claude how to use the Stardag SDK, Registry API, UI, and CLI.

## What It Does

This skill automatically activates when Claude encounters stardag-related work:

- Writing code that imports `stardag`
- Defining tasks, DAGs, or pipelines
- Configuring builds, targets, or serialization
- Working with the Registry API/UI
- Setting up authentication or configuration

Claude uses this skill as background knowledge — it won't appear in the `/` menu (`user-invocable: false`), but Claude loads it automatically when relevant to your conversation.

## File Structure

```
.claude/skills/stardag/
├── SKILL.md                     # Main entry point — overview, quick reference, key imports
├── sdk-core.md                  # Tasks, dependencies, parameters and the two hashes, settings, build, task refs/store
├── sdk-targets.md               # Targets, serialization, storage, target roots
├── sdk-advanced.md              # Async, dynamic deps, namespaces, artifacts, cancellation, Modal
├── registry-and-platform.md     # Registry entities and API, CLI, config, upgrading from v1
├── examples.md                  # Complete code examples and common patterns
└── README.md                    # This file
```

### Content Scope

| File                         | Topics                                                                                                                                                                                                                                    |
| ---------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **SKILL.md**                 | Quick reference, core concepts, three-tier API overview, key imports                                                                                                                                                                      |
| **sdk-core.md**              | Task hierarchy, @sd.task, dependencies, referring to existing tasks (`ref`, the committed task store, find/annotate/pin), build options, plan-only registration, settings, significant/non-significant fields, the two hashes, versioning |
| **sdk-targets.md**           | FileSystemTarget, serializers (JSON/pickle/pandas), target factories, target roots config, S3 integration, InMemoryTarget                                                                                                                 |
| **sdk-advanced.md**          | Async, dynamic deps, namespaces, artifacts, cancellation, polymorphic types, Prefect, Modal (deployments, reactive builds, TickConfig)                                                                                                    |
| **registry-and-platform.md** | Registry entities (task, instance, plan, deployment, execution), `/api/v2`, v2 CLI, config, local dev, upgrading from v1                                                                                                                  |
| **examples.md**              | Three API levels side-by-side, ML pipeline pattern, DAG composition factories, fan-out/benchmark, conditional deps, non-significant params and settings                                                                                   |

## How It Works

Claude Code [automatically discovers skills](https://code.claude.com/docs/en/skills) from `.claude/skills/` directories. The `SKILL.md` description is always loaded into context so Claude knows when to activate it. Supporting files are loaded on-demand when Claude needs detailed reference.

## Keeping It Updated

It describes the v2 line (SDK 0.30 and `main` after it, registry server 0.9).

**Last synced with `main`: 2026-10-28, at `d2aef2fa`.** To bring it up to date, start from
`git log d2aef2fa..main` and the CHANGELOG's `[Unreleased]` section, then move this marker.

**Check it before every release**, since nothing else forces a refresh: it slipped from
0.24 to 0.27, and again through 0.28. Update it when:

- New public API is added to `lib/stardag/`
- Breaking changes to task definitions or build execution
- New Registry API endpoints or UI features
- Changes to CLI commands or configuration
- New integration points (e.g., new cloud providers)

The `stardag/.claude/CLAUDE.md` file contains a note pointing to this skill bundle as a reminder to keep it in sync.

## Further Reading

- [Stardag Documentation](https://docs.stardag.com/) — latest SDK and platform docs
- [Claude Code Skills Docs](https://code.claude.com/docs/en/skills) — how skills work
- Example code: `stardag/lib/stardag-examples/`
