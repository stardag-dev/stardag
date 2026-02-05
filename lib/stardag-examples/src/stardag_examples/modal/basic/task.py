import stardag as sd


@sd.task(name="Range")
def get_range(limit: int) -> list[int]:
    return list(range(limit))


@sd.task(name="Concat")
def concat(
    a: sd.Depends[list[int]],
    b: sd.Depends[list[int]],
) -> list[int]:
    return a + b


@sd.task(name="Sum")
def get_sum(integers: sd.Depends[list[int]]) -> int:
    return sum(integers)
