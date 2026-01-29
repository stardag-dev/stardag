# Build & Execution

Understanding how Stardag executes DAGs.

## Execution Model

Stardag uses "Makefile-style" bottom-up execution:

1. Start at the requested task
2. Check if it's complete (output exists)
3. If not, recursively ensure dependencies are complete
4. Execute incomplete tasks in dependency order
5. Persist outputs

Example:

```
Requested: Task C (depends on B, which depends on A)

Step 1: Is C complete? No
Step 2: Is B complete? No
Step 3: Is A complete? No
Step 4: Execute A, save output
Step 5: Execute B, save output
Step 6: Execute C, save output

```

## The Build Functions

The primary way to execute tasks is `sd.build` or `await sd.build_aio`.

Mostly for testing and debugging, there is also `sd.build_sequential` and `sd.build_sequential_aio`.

!!! info "🚧 **Work in progress** 🚧"

    This documentation is still taking shape. It should soon cover:

    - How concurrency is achieved via asyncio, threads and or processes
    - Transfer of execution (e.g. -> Modal)
    - Global concurrency lock
    - ...
