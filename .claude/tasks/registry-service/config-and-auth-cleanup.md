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

**Current State:**

1. **SDK Config** is scattered across:

   - `TargetFactoryConfig` in `target/_factory.py` (pydantic-settings, `STARDAG_TARGET_` prefix)
   - `APIRegistryConfig` in `build/api_registry.py` (pydantic-settings, `STARDAG_API_REGISTRY_` prefix)
   - No central config module

2. **CLI Config** stored in `~/.stardag/`:

   - `credentials.json`: OAuth tokens (access_token, refresh_token, token_endpoint, client_id)
   - `config.json`: api_url, timeout, organization_id, workspace_id
   - Single global config - no per-organization scoping

3. **API Models** exist for Organization, Workspace, ApiKey, User, OrganizationMember

   - No TargetRoot model exists yet
   - No personal workspace auto-creation

4. **UI Settings** in `OrganizationSettings.tsx`:
   - API keys grouped by workspace
   - No target root management yet

**Key Gaps Identified:**

- Target roots are local-only, not workspace-scoped in DB
- No sync mechanism between central config and local cache
- No project-level config to lock org/workspace per repo
- No environment switching (local dev vs production)
- Personal workspaces not auto-created per user

### Plan

#### Phase 1: Foundation - Centralized SDK Config

**1.1 Create central config module** (`lib/stardag/src/stardag/config.py`)

- Single `StardagConfig` pydantic-settings class combining:
  - API settings (url, timeout, retries)
  - Target factory settings (roots mapping)
  - Active context (organization_id, workspace_id)
- Idiomatic pydantic-settings with `STARDAG_` prefix
- Support loading from: env vars → config files → defaults

**1.2 Refactor existing config users**

- Update `TargetFactoryConfig` to delegate to central config
- Update `APIRegistryConfig` to delegate to central config
- Remove duplicated env var reading logic

#### Phase 2: Workspace Target Roots (DB + Sync)

**2.1 Add TargetRoot API model** (`app/stardag-api/`)

- Fields: `id`, `workspace_id` (FK), `key` (str), `uri_prefix` (str), `created_at`, `updated_at`
- Unique constraint on (workspace_id, key)

**2.2 Add API endpoints**

- CRUD for target roots under `/api/v1/workspaces/{workspace_id}/target-roots`
- Include in workspace sync response

**2.3 Add UI for target root management**

- Settings section per workspace (alongside API keys)
- Clear warnings on UPDATE/DELETE operations
- Recommend creating new roots + migration over modification

**2.4 Implement sync mechanism (SDK/CLI)**

- On login: sync all workspace settings to local cache
- On build start: validate local cache matches central config
  - New roots only → log + update cache
  - Changes/deletions → raise exception, prompt user to sync
- Store cached target roots in `~/.stardag/profiles/{profile}/cache/workspaces/{workspace_id}/target_roots.json`

#### Phase 3: Profile & Project Config

**3.1 Support multiple profiles**

- Separate config directories per profile (e.g., `~/.stardag/profiles/{profile_name}/`)
- Active profile stored in `~/.stardag/active_profile`
- Canonical profiles: `local` (docker-compose), `central` (remote SAAS)

**3.2 Project-level config** (`.stardag/config.json` in repo root)

- Can set/override: `profile`, `organization_id`, `workspace_id`
- Can restrict: `allowed_organizations` (list of IDs or slugs)
- SDK validates against restrictions before build
- Priority: env vars > project config > profile config > defaults
- Purpose: prevent accidental cross-org builds + convenient project defaults

**3.3 CLI profile management**

- `stardag profile list` - list available profiles
- `stardag profile add <name> --api-url <url>` - add new profile
- `stardag profile use <name>` - switch active profile
- `stardag profile current` - show current profile

#### Phase 4: Personal Workspaces

**4.1 Auto-create personal workspace**

- When user joins organization (via invite accept or org creation)
- Name: "Default (personal)"
- Slug: `default-{user_email_prefix}` or `personal-{user_id_short}`
- Only visible/accessible to that user

**4.2 Update workspace visibility**

- API: filter personal workspaces to only show to owner
- UI: mark personal workspaces distinctly

#### Phase 5: CLI Improvements

**5.1 Streamlined login flow**

- After OAuth success, if no active org: prompt to select (or auto-select if only one)
- After org selected, auto-select personal workspace as default
- Display confirmation of active org/workspace

**5.2 Support slugs for org/workspace selection**

- `stardag config set organization <slug-or-id>`
- `stardag config set workspace <slug-or-id>`
- Resolve slugs via API lookup

**5.3 Add sync command**

- `stardag config sync` - fetch latest workspace settings from API
- Auto-sync on login

#### Phase 6: Sample Project Config

**6.1 Create example in stardag-examples**

- `.stardag/config.json` with org/workspace restrictions
- README documenting the config model
- Demo switching between environments

### Open Questions (Need Clarification)

See **Decisions** section below for questions requiring user input.

## Decisions

### Confirmed Decisions

**D1: TargetRoot field naming** → `name` + `uri_prefix`

- Clearer semantics than `key`/`value`

**D2: Personal workspace slug format** → `personal-{email_prefix}`

- With fallback for duplicate email prefixes: append numeric suffix (e.g., `personal-anders-2`)
- Need uniqueness check when creating personal workspace

**D3: Profile directory structure** → `~/.stardag/profiles/{profile}/`

- Each profile has its own credentials, config, and cache
- Active profile tracked in `~/.stardag/active_profile`

**D4: Project config location** → `.stardag/config.json`

- Separate file in repo root
- Supports project-specific overrides (e.g., `organization_id`, `workspace_id`)
- Priority: env vars > project config > user config > defaults

**D5: Target root sync strictness** → Moderate

- Additions: auto-sync with log message
- Changes/deletions: raise exception, require explicit sync

**D6: Personal workspace visibility** → Visible to all org members

- Not sensitive, just for convenience
- UI: hidden in collapsed section, extra click to view
- No special API filtering needed

**D7: Breaking changes** → Allowed

- Can clear all state (user config, DB volumes)
- Can modify/consolidate existing migrations
- No migration path needed for existing configs

## Progress

- [x] Initial analysis of current state
- [x] Created execution plan
- [x] Created CONFIGURATION_GUIDE.md
- [x] Phase 1: Centralized SDK Config
  - [x] Created `lib/stardag/src/stardag/config.py` with unified `StardagConfig`
  - [x] Refactored `TargetFactory` to use central config
  - [x] Refactored `APIRegistry` to use central config
  - [x] Updated `cli/credentials.py` to use profile-based paths
  - [x] Added 23 unit tests for config module
  - [x] All 76 tests pass
- [x] Phase 2: Workspace Target Roots
  - [x] TargetRoot model and migration (workspace_id, name, uri_prefix)
  - [x] CRUD endpoints at /ui/organizations/{org}/workspaces/{ws}/target-roots
  - [x] SDK sync endpoint at /api/v1/target-roots
  - [x] CLI sync on `stardag config set workspace`
  - [x] `stardag config list target-roots` command
  - [x] UI Target Roots section in Organization Settings
  - [x] 5 API tests for target root CRUD
- [x] Phase 3: Profile & Project Config
  - [x] CLI profile commands (`stardag profile list/current/add/use/delete`)
  - [x] Project-level config loading (`.stardag/config.json`)
  - [x] SDK validation against project restrictions (`allowed_organizations`)
  - [x] 6 unit tests for profile CLI
  - [x] All 82 tests pass
- [x] Phase 4: Personal Workspaces
  - [x] Added `owner_id` column to workspaces (migration 20251223_150000)
  - [x] Auto-create personal workspace on org creation
  - [x] Auto-create personal workspace on invite acceptance
  - [x] UI: show personal workspaces in collapsed section
  - [x] All 65 API tests pass
- [ ] Phase 5: CLI Improvements
- [ ] Phase 6: Sample Project Config

## Notes

### Implementation Order Rationale

The phases are ordered by dependency:

1. **Phase 1 first** - Central config is foundation for all other changes
2. **Phase 2 second** - Target roots in DB needed before sync logic
3. **Phase 3 third** - Profile management builds on central config
4. **Phase 4 fourth** - Personal workspaces can be added independently
5. **Phase 5 fifth** - CLI improvements depend on above being in place
6. **Phase 6 last** - Sample project demonstrates completed system

### Risk Areas

- ~~**Migration complexity**: Existing users have local target roots~~ → Breaking changes allowed (D7)
- ~~**Breaking changes**: Config file locations/format~~ → Breaking changes allowed (D7)
- **Testing**: Need comprehensive tests for sync logic edge cases
- **Scope creep**: Large task, stay focused on essentials per phase

### Testing Strategy

Each phase should be tested before committing:

**Phase 1 (Centralized Config)**

- Unit tests for `StardagConfig` loading from various sources
- Verify existing functionality still works (target factory, API registry)
- Test env var override behavior

**Phase 2 (Target Roots)**

- API unit tests for CRUD operations
- SDK unit tests for sync logic (additions, changes, deletions)
- UI manual verification of settings page
- E2E test after full phase completion

**Phase 3 (Profiles)**

- Unit tests for profile switching
- Unit tests for project config loading and validation
- Manual test: switch between local/central profiles

**Phase 4 (Personal Workspaces)**

- API unit tests for auto-creation on org join
- API unit tests for slug uniqueness with fallback
- UI manual verification of workspace display

**Phase 5 (CLI)**

- Manual test of login flow with auto-selection
- Test slug resolution for org/workspace
- Test sync command

**Phase 6 (Sample Project)**

- Full E2E test with sample project config

### Commit Strategy

One commit per completed phase:

- `feat(sdk): centralized config module (Phase 1)`
- `feat(api,sdk,ui): workspace target roots (Phase 2)`
- `feat(sdk,cli): profile and project config (Phase 3)`
- `feat(api,ui): personal workspaces (Phase 4)`
- `feat(cli): streamlined login and sync (Phase 5)`
- `docs(examples): sample project config (Phase 6)`

### Alternative Considered

**Org-scoped credentials** (rejected): The task originally considered per-org login. Rejected because:

- UI has single global session
- Would be confusing UX
- Project-level config achieves the safety goal without complexity
