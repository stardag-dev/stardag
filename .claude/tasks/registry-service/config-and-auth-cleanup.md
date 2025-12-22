# Config and auth cleanup

## Status

active

## Goal

A solid model for configuration of stardag SDK ("Registry") and CLI.

## Instructions

We need to straighten out the configuration+auth model and flow. Basically there are a few separate components:

- 1. Core config (used both by SDK and CLI): things like API_URL, timeouts/retries etc.
- 2. Organization+Workspace specific config _cache_: for each workspace there should be clearly defined "target roots" (see: `TargetFactoryConfig` in `lib/stardag/src/stardag/target/_factory.py`)
- 3. Active context/state: Which is my currently active (logged in to) organization, and which is the active workspace.
- 4. Credentials (obtained via CLI) _per organization_.

Central TODOs:

- [ ] Workspace target roots must be the same for all users.
  - [ ] Add API DB models to represent `TargetRoot`s per workspace. A target root has a `key` (str) and a `value` (str) (basically an URI root/prefix). Question: should it be called `name/id/key` and `uri_prefix` instead?
  - [ ] Add CRUD configuration of these under settings in the UI, but with a clear warning on UPDATE and DELETE that this is discouraged (previously run tasks that looks completed in the UI, will resolve as uncompleted during build if the target root has changed, recommend to create a new target root instead and migrate over specific tasks.)
  - [ ] Since this is yet another setting _per workspace_, like API keys, maybe nest both target roots and API keys under a section for each workspace, WDYT?
  - [ ] When ever a user logs in via CLI, all workspace settings should be synced to local config.
  - [ ] when ever a build it started, we should verify that all local cached target roots match the centrally defined, if not: If only new roots added compared to local config/cache - log this and update config cache with latest. If any changes/deletions raise a clear exception and tell the user to sync workspace config (app support for this via CLI). The reason we want to have roots cached persitently, is that the user should be able to read/load tasks even without a connection to the API/Registry.
  - [ ] We will have to update how target roots can be provided. Should be read from central config/cache.
- [ ] Is the CLI login token really scoped on organization? This was the original idea, check this. Or does it make sense to require the user to "login" once per active org via CLI. The strange thing about this is that the user is only logged in "once/globally" in the UI, so maybe just confusing? What I want to avoid is accidental switch of organization when building with the SDK locally. What is a good approach here? Ah: Maybe keep a config _local to any given project/repository_, which restricts which orgs this stardag SDK can build against. Make a solid suggestion. (Note that if a user has access to a code base and is a member of an org, we can not truly stop the user frombuilding against another org, so this is just to avoid mistakes)
- [ ] ~All (basically) config should be defined in one place using ideomatic `pydantic-settings`, so probably there should be central module `stardag/config.py` where ~all config is defined (the exception if probably the things that are purely related to CLI login?). NOTE that pydantic settings supports reading from/overriding via env vars, so we should not duplicate this logic in e.g. `src/stardag/build/api_registry.py`, we should expect all config to be complete once the central top level `StardagConfig` object is initialized. Use ideomatic `pydantic-settings`.
- [ ] Switching between local API (docker compose, for dev/testing) and some real cenrally deployed API should be smooth. We probably need separate top-level configuration "directories" for these two cases? And store which one is selected in "Active context/state"
- [ ] Initialization and defaults. It should be super user friendly to get started, we should support committed config in specific projects for what is common for all users (everything except for credentials basically?) We proabably need to create a _sample project/local configuration_ in stardag-examples to demo and test this.
  - [ ] In every org, what is visible as the "Default" workspace, should really be "Default (personal)", i.e. a separate workspace should exist per member of an organization (auto created!) for which only this user can see and build tasks against. This is intended for personal dev/testing.
  - [ ] In the CLI, after logging in, if no org is set as active, set "the only one" if only one exists, else prompt the user (so they don't need to run a separate command). Same with workspace, but here default to "Default (personal)" if no other set as active.
  - [ ] In the CLI, we should be able to set org and workspace by _slug_ as well as ID. (No two workspaces should have the same slug within an org. The "Default (personal)" slugs should probably be "default-<user-email>" for clarity.)

## Context

We have recently added authentication for all interaction between stardag API and SDK & UI respectively.

The current model for configuration is "spread out" and does not fully reflect the underlying model.

## Execution Plan

### Summary Of Preparatory Analysis

### Plan

1. Step one
2. Step two
3. ...

## Decisions

Key decisions made and their rationale.

## Progress

- [x] Completed item
- [ ] Pending item

## Notes

Any additional observations, blockers, or open questions.
