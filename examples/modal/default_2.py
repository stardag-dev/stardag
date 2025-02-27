import stardag as sd
import stardag.integration.modal as sd_modal

stardag_app = sd_modal.StardagApp("stardag-default-2")
app = stardag_app.modal_app


@sd.task
def add(a: float, b: float) -> float:
    return a + b


@sd.task
def multiply(a: float, b: float) -> float:
    return a * b


@sd.task
def subtract(a: float, b: float) -> float:
    return a - b


def _main():
    expression = add(
        a=add(a=1, b=2),
        b=subtract(
            a=multiply(a=3, b=4),
            b=5,
        ),
    )
    return stardag_app.build_remote(expression)


@app.local_entrypoint()
def main():
    return _main()


if __name__ == "__main__":
    res = _main()
