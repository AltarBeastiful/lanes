from calc import add, divide


def test_add_is_commutative() -> None:
    assert add(2, 7) == add(7, 2)


def test_divide_is_whole() -> None:
    assert divide(7, 2) == 3
