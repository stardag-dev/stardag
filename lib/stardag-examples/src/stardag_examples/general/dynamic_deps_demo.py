"""Demo: a DAG with dynamic dependencies that themselves have static ``requires()``.

Two kinds of edges get rendered in the DAG view:

- **Static deps** declared up-front via ``Task.requires()`` — known at
  registration time.
- **Dynamic deps** yielded from a generator ``run()`` — only discovered once
  the parent task starts executing.

**Why dynamic deps?** The canonical case is "one task has to run first so we
know which *other* tasks need to run". Here, ``Orchestrator`` can't enumerate
the chunks up front — that requires first completing ``GetChunksToProcess``,
which inspects the ``source_uri`` and returns a list of ``ChunkSpec``s.
``Orchestrator`` then yields one ``TransformChunk`` per spec, and each
``TransformChunk`` statically requires its own ``LoadChunk``. The yielded
sub-DAGs resolve their own static ``requires()`` chains before
``Orchestrator`` resumes and aggregates the results:

    Orchestrator(source_uri)
       │  static: GetChunksToProcess(source_uri)
       ▼
    GetChunksToProcess  ── (result: list[ChunkSpec]) ───╮
                                                        │
    Orchestrator resumes, yields one per spec:          │
       │  dynamic: [TransformChunk, …]  ◄───────────────╯
       ▼
    TransformChunk(chunk=LoadChunk(spec))
       │  static: LoadChunk(spec)
       ▼
    LoadChunk(spec)

In the UI, edges from ``Orchestrator`` to the ``TransformChunk``s are
indigo (dynamic); the ``Orchestrator → GetChunksToProcess`` and
``TransformChunk → LoadChunk`` edges are grey (static).

To run:

    $ uv run --project lib/stardag python -m stardag_examples.general.dynamic_deps_demo \\
          --source-uri my-dataset-v1

(Use the ``lib/stardag`` venv so the example picks up your local SDK checkout
rather than the published PyPI version.)

Stardag caches completed tasks by parameter hash, so re-running with the same
``--source-uri`` will hit the cache and skip execution entirely. To trigger
a fresh build, either

- pass a different ``--source-uri`` (each string produces a deterministic
  but different DAG shape — see ``GetChunksToProcess.run``), or
- delete this example's namespace under your target root:
  ``rm -rf "$(stardag config show | awk '/default:/{print $2}')/stardag_examples.general.dynamic_deps_demo"``.
"""

import hashlib

import stardag as sd
import typer
from pydantic import BaseModel

sd.namespace("stardag_examples.general.dynamic_deps_demo", __name__)

# Cap the DAG to something visually manageable in the UI.
MAX_CHUNKS = 6
MAX_CHUNK_SIZE = 8


class ChunkSpec(BaseModel):
    chunk_id: int
    chunk_size: int


class Orchestrator(sd.Task[int]):
    """Runs ``GetChunksToProcess(source_uri)`` first, then dynamically yields
    one ``TransformChunk`` per discovered chunk. After all yielded deps are
    complete, aggregates their outputs.
    """

    source_uri: str = "mock-string"

    def requires(self):
        return GetChunksToProcess(source_uri=self.source_uri)

    def run(self):
        chunks = self.requires().load()
        transforms = [
            TransformChunk(chunk=LoadChunk(chunk_spec=chunk)) for chunk in chunks
        ]
        # Yield as a batch — the build system ensures every TransformChunk
        # (and each TransformChunk's static LoadChunk require) is complete
        # before execution resumes here.
        yield transforms
        total = sum(t.load() for t in transforms)
        self._save(total)


class GetChunksToProcess(sd.Task[list[ChunkSpec]]):
    """Pretends to be an expensive computation that inspects ``source_uri``
    and returns which chunks need to be processed.

    Uses a deterministic hash of ``source_uri`` to derive the number of
    chunks (1..``MAX_CHUNKS``) and each chunk's size (1..``MAX_CHUNK_SIZE``).
    Same URI → same DAG; different URI → different DAG.
    """

    source_uri: str

    def run(self) -> None:
        digest = hashlib.sha256(self.source_uri.encode()).digest()
        num_chunks = 1 + digest[0] % MAX_CHUNKS
        specs = [
            ChunkSpec(
                chunk_id=i,
                # digest is 32 bytes; byte[i+1] is always in range for i<MAX_CHUNKS<32
                chunk_size=1 + digest[i + 1] % MAX_CHUNK_SIZE,
            )
            for i in range(num_chunks)
        ]
        self._save(specs)


class LoadChunk(sd.Task[list[int]]):
    """Loads a chunk of integers. Pretends to be an expensive data fetch."""

    chunk_spec: ChunkSpec

    def run(self) -> None:
        start = self.chunk_spec.chunk_id * self.chunk_spec.chunk_size
        self._save(list(range(start, start + self.chunk_spec.chunk_size)))


class TransformChunk(sd.Task[int]):
    """Static require: a ``LoadChunk``. Sums the loaded chunk."""

    chunk: sd.TaskLoads[list[int]]

    def requires(self):
        return self.chunk

    def run(self) -> None:
        chunk = self.requires().load()
        self._save(sum(chunk))


def main(source_uri: str = "mock-string") -> None:
    root = Orchestrator(source_uri=source_uri)
    sd.build(root)
    result = root.load()
    print(f"Total: {result}")


if __name__ == "__main__":
    typer.run(main)
