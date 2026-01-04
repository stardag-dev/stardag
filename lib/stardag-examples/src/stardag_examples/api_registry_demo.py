"""Demo script showing how to use the API registry with stardag.

To run this demo:
1. Start the services: docker compose up -d
2. Set the API URL: export STARDAG_API_REGISTRY_URL=http://localhost:8000
3. Run this script: python -m stardag_examples.api_registry_demo
"""

import stardag as sd
from stardag.config import load_config


@sd.task
def add_numbers(a: int, b: int) -> int:
    """A simple task that adds two numbers."""
    return a + b


@sd.task
def multiply_by_two(value: int) -> int:
    """A task that multiplies its input by two."""
    return value * 2


@sd.task
def add_and_format(a: int, b: int) -> str:
    """A task that adds two numbers and formats the result."""
    result = a + b
    return f"The sum is: {result}"


def main():
    # Load configuration
    config = load_config()
    print("Configuration loaded. Running agaist Registry at:", config.api.url)
    # Create a simple DAG:
    # add(1, 2) -> multiply(*2) -> add_with(multiply(3, 4)) -> format
    step1 = add_numbers(a=1, b=2)  # = 3
    step2 = multiply_by_two(value=step1)  # = 6
    step3 = add_numbers(a=3, b=4)  # = 7
    step4 = multiply_by_two(value=step3)  # = 14
    final_task = add_and_format(a=step2, b=step4)  # = "The sum is: 20"

    print("Building DAG...")
    print(f"Final task: {final_task.get_name()} ({final_task.id}...)")

    # Build the DAG
    sd.build(final_task)

    # Read the result
    result = final_task.result()

    print(f"Result: {result}")

    ui_url = None
    if config.api.url == "http://localhost:8000":
        ui_url = "http://localhost:3000"
    elif config.api.url == "https://api.stardag.com":
        ui_url = "https://app.stardag.com"

    if ui_url:
        print(f"\nCheck the UI at {ui_url} to see the registered tasks!")


if __name__ == "__main__":
    main()
