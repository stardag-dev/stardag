# Modal Integration Examples

Run Stardag tasks on Modal's serverless infrastructure with automatic scaling, GPU support, and custom environments.

Includes examples for:

- **basic/** - Minimal Modal app setup
- **walkthrough/** - Full integration walkthrough: `build_trigger` (restart-safe
  triggering and reactive scheduling), detached execution, dynamic deps, and
  registry-backed named concurrency limits — see
  [walkthrough/README.md](walkthrough/README.md)
- **checkpointing/** - Surviving preemption and function timeouts: a task
  that checkpoints, raises `sd.ResumableInterruption`, and is resumed by the
  scheduler until it converges
- **ml_pipeline/** - ML pipeline on Modal
- **prefect/** - Modal + Prefect for observability

## Quick Start

```bash
cd lib/stardag-examples
uv sync --extra modal

# Set default target root to a modal volume (auto created in deploy)
# (Or create/use a Stardag environment with a remote filesystem for default target root)
export STARDAG_TARGET_ROOTS__DEFAULT="modalvol://stardag-examples/target-roots/default"

# Deploy and run basic example
uv run stardag modal deploy stardag_examples/modal/basic/app.py
python stardag_examples/modal/basic/main.py
```

## Documentation

See the full guide: [Integrate with Modal](https://docs.stardag.com/how-to/integrate-modal/)
