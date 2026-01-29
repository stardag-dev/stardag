# AsyncIO

Stardag natively supports `async` implementation of `Task`s and `Target`s.

A task can implement either - _or both_ - of the methods `def run(self)` and `async def run_aio(self)`. Which method is used depends on what's implemented and the build logic used, which is discussed in the next section [Build & Execution](./build-execution.md).

Stardag's built-in `FileSystemTarget`s (for, for example, local disk, S3 and Modal volumes) already implement complete async interfaces. Even if your task only implements the sync `def run(self)` method, the async implementation of targets can provide significant (order(s) of magnitude) speedup in traversing DAGs with a large number of dependencies.

---

!!! info "🚧 **Work in progress** 🚧"

    This documentation is still taking shape. It should soon provide more details and examples of how to fully leverage `asyncio` for your DAGs.
