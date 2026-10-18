"""`python -m lanes`, which the systemd service runs, so the timer needs no `lanes` on its PATH."""

from lanes.cli import main

main(prog_name="lanes")
