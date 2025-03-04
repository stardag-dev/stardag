import stardag as sd
from stardag_examples.modal.prefect.app import stardag_app

# from stardag_examples.ml_pipeline.class_api import get_benchmark_dag
from stardag_examples.modal.prefect.task import get_range, get_sum


def worker_selector(task: sd.Task) -> str:
    if task.get_family() == "Sum":
        return "large"
    return "default"


if __name__ == "__main__":
    dag = get_sum(integers=get_range(limit=10))
    res = stardag_app.build_spawn(dag, worker_selector=worker_selector)
