#!/usr/bin/env bash
# The monthly reading of OpenStreetMap: manage.py osm_sync --send in the running web container.
# bitmentor-osm@<project>.timer runs it once a month. It READS OpenStreetMap, proposes a link for the shops that are beyond
# doubt the same place (the association confirms it in /admin/), fills the address, town and position of the confirmed ones,
# prepares the changes to propose, and sends a short summary to TELEGRAM_MODERATION_CHAT (or the community's email) when
# there is something new to look at. It never writes to OpenStreetMap:
# that is manage.py osm_upload, by hand. Exit 10 means "summary sent", which the unit counts as a success; 1 is a reading
# that failed (Overpass down or slow, an answer that cannot be right) or a summary not delivered, and the unit tries again.
# A community without an `osm` section in community.yml: the command says it is off and exits 0.
# Usage: osm.sh <directory>, which contains release.env.
set -euo pipefail
DIR=${1:?missing folder}
cd "$DIR"
. ./release.env
: "${PROJECT:?}"
# Not during a release, whose migrations change the database: shared, it waits for release.sh and backup.sh, which
# take the lock alone. The reading takes a minute at most.
mkdir -p state
exec 9>state/.lock
flock -s -w 900 9 || { echo "the folder has been busy for a quarter of an hour"; exit 1; }
web=$(docker ps -q --filter "label=com.docker.compose.project=$PROJECT" --filter "label=com.docker.compose.service=web" | head -1)
[ -n "$web" ] || { echo "web is not running"; exit 1; }
exec docker exec "$web" python manage.py osm_sync --send
