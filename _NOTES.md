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

CLI:

- [ ] Inconsistent docker compose down (DB vs keycloak) what's expected?
- [ ] View pending invites should say "Create org" when no pending and bring up a modal on first login. Same with pending invites (modal).
- [ ] Token expired propagate to CLI and SDK
- [ ] Token expiration time (locally 24h)
- [ ] Fix auth error missing sub
- [ ] Unless a organization is set, auto set first (and ask for confirmation), set to default/first workspace
- [ ] Invalid parameter: id_token_hint
- [ ] clarify what is in extra and not
- [ ] claude instructions how to install --all-extras, and to use pre-commit hooks
- [ ] DAG view default to centered
- [ ] Canonical way to read all config from model (not separate via envvars etc.)

1. [x] Dev-experience: What happens when we run `docker compose down` (vs e.g. `docker compose down -v)` seems abit inconsistent when it comes to DB vs Keycloak; Does Keycloak drop the created user always when brought down, but DB state is perserved. If so this causes issue, what's expected and how do we make this synced (preferably Keycloak also persistent state unless `-v`)?
2. [x] UI: The "pending invites" link/button (upper left corner) should say "Create organization" when no pending invites exists. And let's bring up a modal on "first login"/whenever an org is not created, promting you to create your first org. Same with pending invites; if there are pending invites that are not accepted or rejected yet, bring up a modal asking for action when loging in (can be closed and ignored ("Answer later") as well)).

3. Auth Error handling: I think I noticed that when my token expired, this caused an auth http error code but no detail explained why. Make sure the status code and error message is clear in this case, and _importantly_: propagate this info to the CLI and SDK exceptions.
4. Related: What is the default token expiration time? It seems short, set it to 24h in the local setup.
5. Related: We previously got a "500 Internl Server Error" when the "sub" token claims where missing, make sure such issue yield correct HTTPException/status codes.

6. CLI: Unless a organization is set, auto set first (and ask for confirmation), set to default/first workspace
7. Invalid parameter: id_token_hint
8. clarify what is in extra and not
9. claude instructions how to install --all-extras, and to use pre-commit hooks
10. DAG view default to centered
