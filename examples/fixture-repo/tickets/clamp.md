# Clamp a value

## What to build

A `clamp(value, low, high)` function in `calc.py` that returns `value` limited to the range from `low` to `high`, both included.

## Acceptance criteria

- [ ] `clamp(5, 0, 10)` returns `5`
- [ ] `clamp(-3, 0, 10)` returns `0` and `clamp(12, 0, 10)` returns `10`
- [ ] `clamp(1, 10, 0)` raises `ValueError` because `low` is above `high`

## Lanes

```toml
route = "direct"
tier = "small"
produces = []
consumes = []
seams = ["calc"]
demo = "`clamp(12, 0, 10)` returns 10"
```
