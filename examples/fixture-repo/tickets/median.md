# Median of a list

## What to build

A `median(values)` function in `calc.py`: the middle value of a non-empty list of integers once sorted, or the mean of the two middle values when the list has an even length.

## Acceptance criteria

- [ ] `median([3, 1, 2])` returns `2`
- [ ] `median([4, 1, 3, 2])` returns `2.5`
- [ ] `median([])` raises `ValueError`

## Lanes

```toml
route = "direct"
tier = "small"
produces = []
consumes = []
seams = ["calc"]
demo = "`median([4, 1, 3, 2])` returns 2.5"
```
