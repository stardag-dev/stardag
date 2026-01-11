import stardag as sd


@sd.task
def add(a: float, b: float) -> float:
    return a + b


@sd.task
def multiply(a: float, b: float) -> float:
    return a * b


@sd.task
def subtract(a: float, b: float) -> float:
    return a - b


if __name__ == "__main__":
    expression = add(
        a=add(a=1, b=2),
        b=subtract(
            a=multiply(a=3, b=4),
            b=5,
        ),
    )

    print(expression.model_dump_json(indent=2))
    # {
    #   "version": "0",
    #   "a": {
    #     "__name": "add",
    #     "__namespace": ""
    #     "version": "0",
    #     "a": 1.0,
    #     "b": 2.0,
    #   },
    #   "b": {
    #     "__name": "subtract",
    #     "__namespace": ""
    #     "version": "0",
    #     "a": {
    #       "__name": "multiply",
    #       "__namespace": ""
    #       "version": "0",
    #       "a": 3.0,
    #       "b": 4.0,
    #     },
    #     "b": 5.0,
    #   }
    # }
    sd.build([expression])
    result = expression.output().load()
    print(result)
    # 10.0
