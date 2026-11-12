#!/usr/bin/env bash
# The guardian of the Lightning addresses: manage.py check_lightning in the running web container.
# bitmentor-sentinel@<project>.timer runs it every hour; the command picks the shops that are due, one at a time.
# It only reads: it probes each address as the register would, and writes the status on the shop.
# Usage: sentinel.sh <directory>, which contains release.env.
set -euo pipefail
DIR=${1:?missing folder}
cd "$DIR"
. ./release.env
: "${PROJECT:?}"
# Not during a release, whose migrations change the database, and no release during the run: the lock is held until
# the end. Shared: it waits for release.sh and backup.sh, which take it alone. --minutes 10 keeps the run short, so a
# release waits at most a few minutes and a backup (which waits a quarter of an hour) never gives up.
mkdir -p state
exec 9>state/.lock
flock -s -w 900 9 || { echo "the folder has been busy for a quarter of an hour"; exit 1; }
web=$(docker ps -q --filter "label=com.docker.compose.project=$PROJECT" --filter "label=com.docker.compose.service=web" | head -1)
[ -n "$web" ] || { echo "web is not running"; exit 1; }
docker exec "$web" python manage.py check_lightning --minutes 10
