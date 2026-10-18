from pathlib import Path

import pytest

from calc import mean

DATA = Path(__file__).parent / "data" / "values.txt"


@pytest.fixture
def values() -> list[int]:
    if not DATA.exists():
        pytest.skip("fixture data not found")
    return [int(line) for line in DATA.read_text().split()]


def test_mean_of_data(values: list[int]) -> None:
    assert mean(values) == 2
