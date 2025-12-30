# NOTEs and TODOs

## TODO

### Basics

- [x] Rename back to `Task` and `task_id`
- [x] Move `.load()` to Target (!) -> Generic only in target, See Notes!
- [ ] ~~Add `context` to output, run, requires~~
- [x] Use annotation instead of json_schema-hack to pass extra info about parameters
- [ ] basic unit testing
  - [ ] Add fixture for auto clearing the registry
  - [ ] Add testing util for registering tasks defined outside of test function
- [ ] Basic heuristic for run-time type checking of Generic type in TaskParams
- [ ] ~~Express dynamic deps explicitly (Generic: Task[TargetT, RunT], StaticTask,~~ -> NO just type annotate as union in base class.
      DynamicTask) or just class variable `has_dynamic_dependencies: bool` (possible to
      overload type hints on this? Yes: <https://stackoverflow.com/questions/70136046/can-you-type-hint-overload-a-return-type-for-a-method-based-on-an-argument-passe>
      but probably overkill)

### Features

- [ ] FileSystemTargets
  - [x] Atomic Writes (copy luigi approach?)
  - [x] S3
  - [ ] GS
- [x] Serialization -> AutoTask
  - [x] Module structure
    - [x] Rename to just `AutoTask`?
  - [ ] ~~Extend Interface of Serializer to have `.init(annotation)` after initialization -> This way you can set additional tuning parameters up front (without partials), and compose serializers (see below: `GZip(JSON())`) and property~~
  - [x] `default_ext: str`
  - [x] Make serializer initialization happen on task declaration for early errors! Use `__pydantic_init_subclass__`
  - [x] Allow specifying explicit serializer: `AutoTask[Feather[pd.DataFrame]]` = `AutoTask[Annotated[pd.DataFrame, PandasFeatherSerializer()]]`
  - [ ] Defaults for:
    - [x] anything JSONAble (pydantic)
    - [x] pd.DataFrame
    - [ ] pd.Series
    - [ ] numpy array
    - [x] Fallback on Pickle
  - [ ] (`GZip[JSON[dict[str, str]]]` = `GZipJSON[dict[str, str]]` = `Annotated[dict[str, str], GZip(JSON())]` ?)
  - [ ] Set `.ext` based on serializer. I.e. add `_relpath_ext` as a property, which by default reads from self.serializer.default_ext
- [ ] function decorator API
  - [x] PoC
  - [x] basic unit tests
- [ ] `ml_pipeline` example
  - [ ] Make output from class_api and decorator_api equivalent
  - [ ] Add units test (compare state of InMemoryTarget)
  - [x] Blocked by some serialization fixes

### Execution

- [x] Basic sequential
- [ ] Luigi runner?
- [ ] Prefect runner
- [ ] Modal runner (+ "deployment")

### Release

- [ ] repo and package name (`stardag`, `*dag`, :star: dag)
- [ ] PyPI name
- [ ] Github Workflows
- [x] unit test (tox) with uv
- [x] Release package with uv/hatchling
- [ ] Cleanup README, basic Docs and overview of core features

## More Recent :)

### Ideas

- [ ] Implement hashing vs serialization with `info.context["mode"]` to have a unified way to hash and serialize tasks and other pydantic models/primitives. Can support even tasks nested in pydantic models and no need for (that) special treatment at task level
  - [ ] `param: Annotated[int, BackwardCompat(default=0)] = 1`

### TODOs

- [x] Setup for Claude
  - [x] Instructions (append continously)
  - [x] DEV-README.md, how to run tests pre-commit etc.
- [x] convert to UV
- [x] Nest to: lib/stardag, (lib/stardag_core), server/, app/

- [x] src nest
- [x] tox pyright fails and should run on tests
- [x] vscode settings for default interpreter
- [x] pre-commits for ui?

Code fixes:

- [x] basic tests for stardag-api
- [x] DB

  - alembic migrations
    - plain SQL
    - separate docker service to apply these
  - separate module for each db-model
  - Use async sqlalchemy everywhere

- Support python 3.14
- Locks

Later:

- publish stardag-examples for ease to get started.

- [ ] Canonical way to read all config from model (not separate via envvars etc.)
- [ ] How to handle tasks alredy built (still register as referenced, tasks don't belong to builds (or even workspaces!? YES they DO, same task ID but different targets, so duplicate), they have events in builds)

1. [x] Dev-experience: What happens when we run `docker compose down` (vs e.g. `docker compose down -v)` seems abit inconsistent when it comes to DB vs Keycloak; Does Keycloak drop the created user always when brought down, but DB state is perserved. If so this causes issue, what's expected and how do we make this synced (preferably Keycloak also persistent state unless `-v`)?
2. [x] UI: The "pending invites" link/button (upper left corner) should say "Create organization" when no pending invites exists. And let's bring up a modal on "first login"/whenever an org is not created, promting you to create your first org. Same with pending invites; if there are pending invites that are not accepted or rejected yet, bring up a modal asking for action when loging in (can be closed and ignored ("Answer later") as well)).

3. [x] Auth Error handling: I think I noticed that when my token expired, this caused an auth http error code but no detail explained why. Make sure the status code and error message is clear in this case, and _importantly_: propagate this info to the CLI and SDK exceptions.
4. [x] Related: What is the default token expiration time? It seems short, set it to 24h in the local setup.
5. [x] Related: We previously got a "500 Internl Server Error" when the "sub" token claims where missing, make sure such issue yield correct HTTPException/status codes.

6. [x] Invalid parameter: id_token_hint

```
⏺ The issue is that signoutRedirect() sends the stored id_token as an id_token_hint to Keycloak. When that token is stale or Keycloak's session data is gone (e.g., after container restart without persisted data), Keycloak rejects it.

  Common causes:
  1. Keycloak restart - Session data was lost (you added the volume, so this should help going forward)
  2. Token expired - The stored id_token expired but local storage still has it
  3. Session mismatch - Browser has an old token from a previous Keycloak session
```

8. clarify what is in extra and not
9. [x] claude instructions how to install --all-extras, and to use pre-commit hooks
10. DAG view default to centered

## config follow ups

- [x] Fix migrations
- [x] Auto default in CLI
- [ ] workspace specific configs!?
- [ ] Create a sub project (python package) example-project in lib/examples
- [ ] worspace config in _project config_ (committed)
- [ ] Rename profile to registry!?

## Auth cleanup Take Two:

- [x] Fix login flow
- [ ] Code should not mention `keycloak`!?
- [ ] URL path sticky when logged out?
- [x] "Auth callback failed: Error: No matching state found in storage" after first sign in
- [x] Don't fetch build before an org is selected/created.
- [ ] `Pattern attribute value ^[a-z0-9][a-z0-9-]*[a-z0-9]$|^[a-z0-9]$ is not a valid regular expression: Uncaught SyntaxError: Invalid regular expression: /^[a-z0-9][a-z0-9-]*[a-z0-9]$|^[a-z0-9]$/v: Invalid character class` on create org modal
- [ ] `Uncaught TypeError: Cannot read properties of undefined (reading 'control')`

```
Uncaught TypeError: Cannot read properties of undefined (reading 'control')
    at content_script.js:1:422999
    at Array.some (<anonymous>)
    at shouldOfferCompletionListForField (content_script.js:1:422984)
    at elementWasFocused (content_script.js:1:423712)
    at HTMLDocument.focusInEventHandler (content_script.js:1:423069)
```

- [x] Create a second org yeilds auth error
- [ ] CLI:
  - [x] ImportError: tomli-w is required to write TOML files. Install with: pip install tomli-w
  - [x] `... profile use` should not be an option! -> OK sets as default
  - [ ] (More user friendly help message on error, ideomatic way only)
  - [x] What's the logic behind not having registry under config in CLI?
  - [x] switching orgs in CLI/profile:

```
uv run stardag auth refresh
Refreshing token for local/my-second-org...
Error exchanging for internal token: Client error '403 Forbidden' for url 'http://localhost:8000/api/v1/auth/exchange'
For more information check: https://developer.mozilla.org/en-US/docs/Web/HTTP/Status/403
```

- [x] ~~add separate section for organizations (default to slug as name), then reference by name in profile~~
- [x] ~~reference workspace by slug, since this must be unique within organization?~~
- [x] Instead: cache slug to id mappings in separate file.

Removed:

Stardag is built to facilitate explorative and data centric work, such as Data Science/ML/AI experimentation. As such, it aims to make it seamless to move between running tasks and loading outputs in "production" (any hosted/cloud based infrastructure) as well as _locally_ on your working station. (TODO complete: both switching between workspaces, including copying of tasks, and reading (+optionally writing) _from(/to) production_ on your local workstation)

AUTH CENTRAL

- [x] Don't require access to all registries to log in!?
- [x] SDK auth, active profile via env var bu says see result at localhost!?
- [x] Failed to fetch tasks
- [x] Creating a new profile even though an identical exists?

Ok, CLI auth fixes:

1. when I run `(uv run) stardag auth login`, if a profile is active via env var `STARDAG_PROFILE=...`, we should default to using _that profiles registry_ to login if --registry not provided explicitly) (now it defaults to localhost or perhaps the [default] from config).
2. When I run the command `(uv run) stardag config profile list` the result should show which profile is active, if any, with an asterisk `*` after. If a profile is active, there should be an explaining comment below: `"* - active profile via env var STARDAG_PROFILE/via [default] in <path to used config>"` (which ever is the case), if no profile active replace the last part with a helpful message on how to set the active profile.
3. If no active profile and no `--registry` arg, and multiple registries, ask the user which to use (with a hint that this can be provided as an arg or set the active profile first).
4. When a profile is active, on login, don't ask the user to select organization and/or workspace - just confirm which profile is in use (and how to switch).
5. In fact that prompt flow should only be used when _no_ profile exists (first time login basically), if at least one profile exists prompt the user to active that or create a new one (with helpfull instruction on how to using the CLI).
6. Partly less relevant after previous fixes, but when a new profile is created by the login flow, it says "Created profile: central-my-org-personal-anders-bjorn-huss" even though this profile already exists -> generally when a new profile is created a. never write "Created profile" if an identical exists, if a new one is created explicitly with a new name, but with identical content/settings: warn/inform the user about this and ask for confirmation if they still want to created the "duplicate with new name".

- [ ] Redact access token from config repr -> `Secret`
- [ ] Fail loudly if profile (from env var) is not available in config

- [ ] An error was encountered with the requested page. Required String parameter 'client_id' is not present (when log out and in again)


Logos
- [ ] Make favicon white on black circle background
- [ ] not *dag in Title on landing page