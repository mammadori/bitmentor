#!/usr/bin/env bash
# The daily reading of the meetups: manage.py read_meetups in the running web container.
# bitmentor-meetups@<project>.timer runs it every day. The command asks the relays of the `meetups` block of community.yml
# (or, without them, the site's own) for the calendar events of the trusted authors, checks every event and updates the
# board. It only reads: nothing is sent to a relay, and a meetup the staff corrected is never overwritten. Without the
# block it does nothing. It ends with an error when no relay answered, so that the unit shows as failed and
# check.sh says so; a relay down among others is only told in the log.
# Usage: meetups.sh <directory>, which contains release.env.
set -euo pipefail
DIR=${1:?missing folder}
cd "$DIR"
. ./release.env
: "${PROJECT:?}"
# Not during a release, whose migrations change the database: shared, it waits for release.sh and backup.sh, which
# take the lock alone. The command takes a minute at most.
mkdir -p state
exec 9>state/.lock
flock -s -w 900 9 || { echo "the folder has been busy for a quarter of an hour"; exit 1; }
web=$(docker ps -q --filter "label=com.docker.compose.project=$PROJECT" --filter "label=com.docker.compose.service=web" | head -1)
[ -n "$web" ] || { echo "web is not running"; exit 1; }
exec docker exec "$web" python manage.py read_meetups
