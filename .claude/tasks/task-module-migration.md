# Task Module Migration

## Status

draft

## Goal

Migrate from the old task module system (`_base.py`, `_task_parameter.py`) to the new consolidated task module (`_task.py`) with `BaseTask` and `Task` classes built on `PolymorphicRoot`.

## Instructions

Analyze all downstream usage of task-related imports across the repository (including stardag-examples and API/UI) and create a migration plan.

## Context

The stardag SDK has two coexisting task systems:

**OLD System** (`_base.py`, `_task_parameter.py`):

- Custom `_Register` class for namespace/family registration
- SHA1-based string task IDs (`task_id: str`)
- `TaskIDRef` as Pydantic BaseModel
- Manual parameter collection via `_param_configs`

**NEW System** (`_task.py`):

- Built on `PolymorphicRoot` for automatic polymorphic serialization
- UUID5-based task IDs (`id: UUID`)
- `TaskRef` as frozen dataclass
- Leverages Pydantic serialization with context-based hash mode
- Cleaner separation: `BaseTask` (abstract) and `Task` (with target support)

## Execution Plan

### Summary Of Preparatory Analysis

#### Key Interface Changes

| Feature                      | OLD                                | NEW                                    | Migration Impact                    |
| ---------------------------- | ---------------------------------- | -------------------------------------- | ----------------------------------- |
| **Base class**               | `BaseModel`                        | `PolymorphicRoot`                      | All task classes need re-parenting  |
| **ID attribute**             | `task_id: str`                     | `id: UUID`                             | Attribute rename + type change      |
| **ID type**                  | SHA1 hex string                    | UUID5                                  | Existing stored IDs incompatible    |
| **Version field**            | `str \| None`                      | `str`                                  | Default changes from `None` to `""` |
| **`complete()` in BaseTask** | Has default impl                   | Abstract                               | Must implement or use `Task`        |
| **`output()` return**        | Optional `TargetT`                 | Required `TargetType`                  | Stricter typing                     |
| **Registration**             | `_REGISTER.add_module_namespace()` | `BaseTask._registry().add_namespace()` | API change                          |
| **Task reference**           | `TaskIDRef` (BaseModel)            | `TaskRef` (dataclass)                  | Class rename + field renames        |
| **Dependency type**          | `TaskDeps` + `TaskStruct`          | `TaskStruct` only                      | Simplified                          |

#### Downstream Usage Analysis

**1. lib/stardag-examples/** (14 files)

- Uses `@sd.task` decorator, `sd.Depends[T]`, `sd.TaskLoads[T]`, `sd.AutoTask[T]`
- Class inheritance from `sd.Task[Target]` and `sd.AutoTask[LoadedT]`
- Integration with Modal and Prefect

**2. app/stardag-api/** (No direct SDK imports)

- Stores task metadata as JSON blobs
- References `task_id`, `task_namespace`, `task_family` fields
- **Critical**: API schema expects string `task_id`, not UUID

**3. app/stardag-ui/** (No Python - TypeScript frontend)

- Consumes API responses
- Displays task IDs as strings

**4. lib/stardag/src/** (Internal - 27+ files)

- `__init__.py` exports from both old and new modules
- Build system uses `task_id` attribute
- Integration modules (Modal, Prefect, AWS) depend on task interfaces

#### Files Requiring Changes

**SDK Core:**

- `lib/stardag/src/stardag/__init__.py` - Update exports
- `lib/stardag/src/stardag/_auto_task.py` - Migrate to new base
- `lib/stardag/src/stardag/_decorator.py` - Update task creation
- `lib/stardag/src/stardag/_task_parameter.py` - Update validation
- `lib/stardag/src/stardag/build/*.py` - Update ID references
- `lib/stardag/src/stardag/integration/**/*.py` - Update interfaces

**Examples:**

- All files in `lib/stardag-examples/src/stardag_examples/`
- Test fixtures in `lib/stardag-examples/tests/`

**API (Schema changes only):**

- `app/stardag-api/src/stardag_api/schemas.py` - Consider UUID support
- Database migration if ID format changes

### Plan

#### Phase 1: SDK Internal Migration (No Breaking Changes)

1. **Ensure `_task.py` is complete**

   - [ ] Verify `BaseTask` and `Task` have all required methods
   - [ ] Ensure `TaskRef` has parity with `TaskIDRef`
   - [ ] Add any missing utility functions (`auto_namespace`, `namespace`)

2. **Create compatibility layer**

   - [ ] Add `task_id` property to new `BaseTask` that returns `str(self.id)`
   - [ ] Create `TaskIDRef.from_task_ref()` adapter if needed
   - [ ] Ensure `_task_parameter.py` works with both systems

3. **Update internal imports**
   - [ ] Update `_auto_task.py` to inherit from new `BaseTask`
   - [ ] Update `_decorator.py` to use new task creation
   - [ ] Update build system to use `id` with fallback to `task_id`

#### Phase 2: Gradual Migration of Examples

4. **Update stardag-examples**

   - [ ] Update `ml_pipeline/class_api.py` to use new base classes
   - [ ] Update `composability.py`
   - [ ] Update `api_registry_demo.py`
   - [ ] Update `task_api_three_levels.py`
   - [ ] Update Modal integration examples
   - [ ] Update test fixtures

5. **Update public exports**
   - [ ] Update `__init__.py` to export from `_task.py`
   - [ ] Maintain backward-compatible aliases

#### Phase 3: API Compatibility

6. **API schema updates**

   - [ ] Decide: Keep `task_id` as string or migrate to UUID
   - [ ] If keeping string: API already compatible (UUID serializes to string)
   - [ ] If migrating: Update schemas, consider database migration

7. **Documentation**
   - [ ] Update docstrings
   - [ ] Add migration guide for external users

#### Phase 4: Cleanup

8. **Deprecate old modules**

   - [ ] Add deprecation warnings to `_base.py` imports
   - [ ] Mark `TaskIDRef` as deprecated

9. **Remove old code** (future release)
   - [ ] Remove `_base.py` after deprecation period
   - [ ] Remove compatibility shims

## Decisions

1. **UUID vs String IDs**: New system uses UUID internally but can serialize to string for API compatibility. Decision needed on whether API should expose UUID type or keep string.

2. **Backward Compatibility Period**: How long to maintain dual support?

3. **Database Migration**: If task IDs change format, existing data needs migration strategy.

## Progress

- [x] Analyze old vs new module interfaces
- [x] Identify all downstream usage
- [x] Create migration plan document
- [ ] Phase 1: SDK Internal Migration
- [ ] Phase 2: Examples Migration
- [ ] Phase 3: API Compatibility
- [ ] Phase 4: Cleanup

## Notes

### Open Questions

1. **ID Determinism**: Are UUID5 IDs generated from the same inputs as SHA1 hashes? If not, all existing task references will break.

2. **AutoTask**: The `_auto_task.py` module heavily depends on `_base.py`. Needs careful migration.

3. **Prefect/Modal Integration**: These integrations may have assumptions about task ID format.

4. **API Registry**: The `build/api_registry.py` sends task data to the API. Need to verify compatibility.

### Risk Assessment

| Risk                                 | Likelihood | Impact | Mitigation                             |
| ------------------------------------ | ---------- | ------ | -------------------------------------- |
| Breaking existing stored task IDs    | High       | High   | Version the ID algorithm, support both |
| API incompatibility                  | Medium     | High   | Keep string serialization of UUIDs     |
| Example code breakage                | High       | Low    | Update examples as part of migration   |
| Integration breakage (Modal/Prefect) | Medium     | Medium | Test thoroughly before release         |

### Related Files

**New System:**

- `lib/stardag/src/stardag/_task.py`
- `lib/stardag/src/stardag/_task_id.py`
- `lib/stardag/src/stardag/_task_loads.py`
- `lib/stardag/src/stardag/polymorphic.py`

**Old System (to be deprecated):**

- `lib/stardag/src/stardag/_base.py`
- `lib/stardag/src/stardag/_task_parameter.py`

**Downstream:**

- `lib/stardag/src/stardag/__init__.py`
- `lib/stardag/src/stardag/_auto_task.py`
- `lib/stardag/src/stardag/_decorator.py`
- `lib/stardag/src/stardag/build/*.py`
- `lib/stardag-examples/src/stardag_examples/**/*.py`
- `app/stardag-api/src/stardag_api/schemas.py`

---

## Name and Namespace Semantics

The main objective is great developer experience when using the SDK and UI (user using the tool/platform to define thier DAG-pipelines).
There is a tension between clarity/specificness about the concept and brevity/simplicity.

Currently we're using `type_name`/`__type_name__` and `type_namespace`/`__type_namespace__`, and the corresponding `type_id`/`__type_id__`. This makes sense but is prioritizing the "clarity/specificness" aspect at some cost of "brevity/simplicity". Also I want to optimize for the case the context af polymophic *`Task`*s.

Also there's some inconsistencies in that we use `task_type_name` but `task_namespace` (not `task_type_namespace`!) in several places...

We should also consider that `type_name` should almost always be the same as the class `__name__`, only in special cases where we want to rename the in module variable name, but keeo backwards compatabililty with hashes etc.

New suggestion for naming:

**Keys serialized data:**

- `__namespace`
- `__name`
  Note: Let's skip the trailing duble underscore `__` (In serialized task version will be `version`, not `__version__` so not inconsistent…)

**In class args**:

- `name`
- `namespace`

**In class variables set explicitly**:

- remove support for setting `name` this way, should be done rarely and leads to complexity and unexpected behavior when subclassing.
- **namespace** (makes more sense to set for a class inclduing subclasses)

**In class methods**:

- `get_name()`
- `get_namespace()`

**In stand-alone variables, including API path/query parameters etc.**:

- `task_name`
- `task_namespace`

**But when ”task” is already obvious as in API and DB models for a `Task`**:

- `name`
- `namespace`

**In UI filtering and display of task properties**:

- `name`
- `namespace`

One downside is that `name` is very generic (harder to search for in codebase etc.) but trumped by better dev UX when using the SDK and UI (these must be consistent since UI is for users of SDK).
