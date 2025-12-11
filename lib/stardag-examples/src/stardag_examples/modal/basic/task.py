import stardag as sd


@sd.task(family="Range")
def get_range(limit: int) -> list[int]:
    return list(range(limit))


@sd.task(family="Sum")
def get_sum(integers: sd.Depends[list[int]]) -> int:
    return sum(integers)
