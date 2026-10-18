from calc import add


def test_add_is_commutative() -> None:
    assert add(2, 7) == add(7, 2)


def test_add_zero() -> None:
    # Already true on main: proves nothing about this branch's change.
    assert add(4, 0) == 4
