#!/usr/bin/env bash
# The morning summary of what to do about the shops: manage.py dashboard --send in the running web container.
# bitmentor-dashboard@<project>.timer runs it every morning. When the things to do changed since the last summary, or
# a week later as a reminder, it sends a short message to TELEGRAM_MODERATION_CHAT (or the community's email, without
# Telegram or when Telegram refuses it) with the link to /admin/today/; otherwise no message. It reads the database and
# writes only what it sent. Exit 10 means "summary sent", which the unit counts as a success; 1 is a summary not
# delivered, or the command failing, and the unit tries again in 10 minutes.
# Before it, manage.py forget_old_requests empties what the requests for help kept of a person, a year after they were
# solved (docs/features/help.md); a failure of it fails the run after the summary.
# Usage: dashboard.sh <directory>, which contains release.env.
set -euo pipefail
DIR=${1:?missing folder}
cd "$DIR"
. ./release.env
: "${PROJECT:?}"
# Not during a release, whose migrations change the database: shared, it waits for release.sh and backup.sh, which
# take the lock alone. The command takes a few seconds.
mkdir -p state
exec 9>state/.lock
flock -s -w 900 9 || { echo "the folder has been busy for a quarter of an hour"; exit 1; }
web=$(docker ps -q --filter "label=com.docker.compose.project=$PROJECT" --filter "label=com.docker.compose.service=web" | head -1)
[ -n "$web" ] || { echo "web is not running"; exit 1; }
result=0
# The merchant's line, the copy of the diagnosis and the account in the requests for help solved more than a year ago: the rows
# stay, for the counts. Nothing for a request still open, or solved less than a year ago
docker exec "$web" python manage.py forget_old_requests || result=$?
summary=0
docker exec "$web" python manage.py dashboard --send || summary=$?
[ "$summary" = 0 ] || [ "$summary" = 10 ] || exit "$summary"
[ "$result" = 0 ] || exit "$result"
exit "$summary"
