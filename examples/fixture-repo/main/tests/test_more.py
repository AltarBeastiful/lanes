from calc import add


def test_add_is_commutative() -> None:
    assert add(2, 7) == add(7, 2)
