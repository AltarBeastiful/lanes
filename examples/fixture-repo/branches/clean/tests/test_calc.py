import pytest

from calc import add, divide, mean, multiply


def test_add() -> None:
    assert add(2, 3) == 5


def test_divide() -> None:
    assert divide(6, 3) == 2


def test_divide_by_zero_raises() -> None:
    with pytest.raises(ZeroDivisionError):
        divide(1, 0)


def test_mean_of_nothing_is_zero() -> None:
    # Already failing on main: a known bug that no branch here fixes.
    assert mean([]) == 0


def test_multiply() -> None:
    assert multiply(4, 5) == 20
