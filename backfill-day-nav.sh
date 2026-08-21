#!/bin/bash
# One-shot: inject the prev/next day-nav bar into ALREADY-generated report pages that
# predate the day-nav feature, using the SAME Docker image as the daily job.
#
# Why not just regenerate? A past page cannot be faithfully rebuilt: a pipeline card's
# step state comes from server variables overwritten in place, and a sparkline "as of
# that day" is not reconstructible. So backfill_day_nav.py does a surgical, idempotent
# in-place HTML edit (insert a self-styled <nav> after </header>). Rerunning skips
# pages that already have the bar, so it is safe to run repeatedly.
#
# No DB access is needed (the script only rewrites HTML under /shared), so no
# --env-file and no --network here, unlike data-monitoring.sh.
#
# Usage:
#   ./backfill-day-nav.sh                  # inject into the live output dir
#   ./backfill-day-nav.sh --dry-run        # preview only, write nothing
#   NAV_BACKFILL_DIR=/path/to/nas/archive ./backfill-day-nav.sh   # retrofit the NAS copy
#
# The host directory to process (default: the live shared_data output dir). Override it
# to point at the NAS archive, whose pages were pruned from the live dir after 30 days.
set -euo pipefail

HOST_DIR="${NAV_BACKFILL_DIR:-$HOME/docker/shared_data/data-monitoring}"

if [ ! -d "$HOST_DIR" ]; then
    echo "ERROR: directory not found: $HOST_DIR" >&2
    exit 1
fi

cd "$HOME/docker/data-monitoring"

# Rebuild so the image contains backfill_day_nav.py (Dockerfile does COPY . /app/).
docker build -t data-monitoring-python-app .

# Mount the target dir at /shared and override the image CMD to run the backfill.
# "$@" forwards flags like --dry-run straight to the Python script.
docker run --rm --name data-monitoring-navbackfill \
    -v "$HOST_DIR:/shared" \
    data-monitoring-python-app \
    python ./backfill_day_nav.py /shared "$@"

echo "day-nav backfill complete on: $HOST_DIR"
