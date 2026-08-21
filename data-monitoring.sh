#!/bin/bash

# data-monitoring — nightly coverage-report generator.
# Read-only on the monitored DB; writes one history table and HTML artifacts into
# the per-stack shared_data subdir. One-shot: it builds, runs to completion, exits
# (NOT a daemon — unlike the crawlers). Invoked from cron at ~06:30 Paris:
#   30 6 * * * /home/debian/docker/data-monitoring/data-monitoring.sh >> /home/debian/docker/data-monitoring/cron.log 2>&1
# The >> ... 2>&1 redirect keeps cron from emailing the job output every night.

if [ "$(docker ps -q -f name=data-monitoring)" ]; then
    echo "data-monitoring container is already running."
else
    # shared_data/data-monitoring is mounted at /shared (OUTPUT_DIR) and mirrored
    # to the NAS by sync_vps_docker.py before the 30-day prune removes old files.
    mkdir -p $HOME/docker/shared_data/data-monitoring
    cd $HOME/docker/data-monitoring
    docker build -t data-monitoring-python-app .
    docker run --rm --network="host" --name data-monitoring \
        --env-file /home/debian/docker/data-monitoring/.env \
        -v $HOME/docker/shared_data/data-monitoring:/shared \
        data-monitoring-python-app
    echo "data-monitoring run complete."
fi
