"""Demo script showing how to use the API registry with stardag.

To run this demo:
1. Start the services: docker compose up -d
2. Set the API URL: export STARDAG_API_REGISTRY_URL=http://localhost:8000
3. Run this script: python -m stardag_examples.api_registry_demo
"""

import tempfile

from stardag import AutoTask, Depends, build


class AddNumbers(AutoTask):
    """A simple task that adds two numbers."""

    a: int
    b: int

    def run(self) -> None:
        result = self.a + self.b
        with self.output().open("w") as f:
            f.write(str(result))


class MultiplyByTwo(AutoTask):
    """A task that multiplies its input by two."""

    add_task: AddNumbers = Depends()

    def run(self) -> None:
        with self.add_task.output().open("r") as f:
            value = int(f.read())
        result = value * 2
        with self.output().open("w") as f:
            f.write(str(result))


class SumAll(AutoTask):
    """A task that sums multiple inputs."""

    multiply_tasks: list[MultiplyByTwo] = Depends()

    def run(self) -> None:
        total = 0
        for task in self.multiply_tasks:
            with task.output().open("r") as f:
                total += int(f.read())
        with self.output().open("w") as f:
            f.write(str(total))


def main():
    # Create a DAG with multiple tasks
    multiply_tasks = [
        MultiplyByTwo(add_task=AddNumbers(a=i, b=i + 1)) for i in range(3)
    ]
    final_task = SumAll(multiply_tasks=multiply_tasks)

    # Use a temporary directory for outputs
    with tempfile.TemporaryDirectory() as tmpdir:
        import os

        os.environ["STARDAG_TARGET_ROOT"] = tmpdir

        print("Building DAG...")
        print(f"Final task: {final_task.task_family} ({final_task.task_id[:12]}...)")
        print(f"Dependencies: {len(final_task.deps())}")

        # Build the DAG
        build(final_task)

        # Read the result
        with final_task.output().open("r") as f:
            result = f.read()

        print(f"Result: {result}")
        print("\nCheck the UI at http://localhost:3000 to see the registered tasks!")


if __name__ == "__main__":
    main()
