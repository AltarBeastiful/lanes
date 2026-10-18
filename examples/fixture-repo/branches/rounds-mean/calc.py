def add(a: int, b: int) -> int:
    return a + b


def divide(a: int, b: int) -> float:
    return a / b


def mean(values: list[int]) -> float:
    return round(sum(values) / len(values), 2)
