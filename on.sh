#!/bin/bash
# Switch data-monitoring.sh back on: removes the flag file CRON_OFF that off.sh wrote.
cd "$(dirname "$(readlink -f "$0")")" || exit 1
# A launcher renamed by the old off.sh (<name>-off.sh, before 2026-10-25) is put back,
# or dropped when git has already restored the tracked one.
[ -f data-monitoring-off.sh ] && { [ -f data-monitoring.sh ] && rm -f data-monitoring-off.sh || mv data-monitoring-off.sh data-monitoring.sh; }
rm -f CRON_OFF && echo "data-monitoring: on (CRON_OFF removed)"
