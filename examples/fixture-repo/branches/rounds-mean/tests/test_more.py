from calc import add, mean


def test_add_is_commutative() -> None:
    assert add(2, 7) == add(7, 2)


def test_mean_rounds_to_two_places() -> None:
    assert mean([1, 2, 2]) == 1.67
