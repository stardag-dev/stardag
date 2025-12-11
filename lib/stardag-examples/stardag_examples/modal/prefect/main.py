import stardag as sd
from stardag_examples.ml_pipeline.class_api import get_benchmark_dag
from stardag_examples.modal.prefect.app import stardag_app


def worker_selector(task: sd.Task) -> str:
    if task.get_family() == "TrainedModel":
        return "large"
    return "default"


if __name__ == "__main__":
    dag = get_benchmark_dag()
    res = stardag_app.build_spawn(dag, worker_selector=worker_selector)
