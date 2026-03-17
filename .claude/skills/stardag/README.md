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
├── sdk-core.md                  # Task hierarchy, decorators, dependencies, build, types
├── sdk-targets.md               # Targets, serialization, storage, target roots
├── sdk-advanced.md              # Async, dynamic deps, namespaces, artifacts, versioning
├── registry-and-platform.md     # Registry API, UI, CLI, auth, config, local dev
├── examples.md                  # Complete code examples and common patterns
└── README.md                    # This file
```

### Content Scope

| File                         | Topics                                                                                                                                   |
| ---------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------- |
| **SKILL.md**                 | Quick reference, core concepts, three-tier API overview, key imports                                                                     |
| **sdk-core.md**              | BaseTask/LoadableTask/Task/TargetTask hierarchy, @sd.task decorator, dependency patterns, build execution, type system, versioning       |
| **sdk-targets.md**           | FileSystemTarget, serializers (JSON/pickle/pandas), target factories, target roots config, S3 integration, InMemoryTarget                |
| **sdk-advanced.md**          | Async run/build, dynamic dependencies (generators), namespaces, artifacts, HashableSet, polymorphic types, integrations (Prefect/Modal)  |
| **registry-and-platform.md** | API endpoints, auth methods, SDK→API flow, UI features, CLI commands, config system, docker-compose setup, distributed locks             |
| **examples.md**              | Three API levels side-by-side, ML pipeline pattern, DAG composition factories, fan-out/benchmark, conditional deps, hash-excluded params |

## How It Works

Claude Code [automatically discovers skills](https://code.claude.com/docs/en/skills) from `.claude/skills/` directories. The `SKILL.md` description is always loaded into context so Claude knows when to activate it. Supporting files are loaded on-demand when Claude needs detailed reference.

## Keeping It Updated

This skill bundle should be updated when:

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
