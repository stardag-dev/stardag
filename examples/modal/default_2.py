import stardag as sd
import stardag.integration.modal as sd_modal

app = sd_modal.StardagApp("stardag-default-2")
modal_app = app.modal_app


@sd.task
def add(a: float, b: float) -> float:
    return a + b


@sd.task
def multiply(a: float, b: float) -> float:
    return a * b


@sd.task
def subtract(a: float, b: float) -> float:
    return a - b


@modal_app.local_entrypoint()
def main():
    expression = add(
        a=add(a=1, b=2),
        b=subtract(
            a=multiply(a=3, b=4),
            b=5,
        ),
    )
    app.build_remote(expression)
