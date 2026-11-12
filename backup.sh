#!/bin/sh
# Copy of the site database (shops, accounts, training, help requests), for an installation from the sources.
# Usage: ./backup.sh <directory> [days to keep, 14]
# It contains no keys that spend and no sales, but it does contain emails and other personal data: protect the folder.
# .env and the community folder are not in the database: copy them separately.
# Every dump is read back before it counts (pg_restore --list for the table of contents, and a full read, because a dump
# cut near its end still passes --list): a copy that does not open is an error, not a copy. A dump that fails the check is
# deleted and the script exits with 1, so a cron or a timer sees it. Old dumps are deleted only after the new one passed.
# Restore: docker compose exec -T postgres pg_restore -U postgres -d <database> --clean --if-exists < file.dump
# On a release machine the nightly copy, encrypted and shipped off the machine, is deploy/backup.sh
# (bitmentor-backup@<project>.timer); the full restore is in docs/backup.md. This one is not encrypted: do not leave it
# on the machine that holds the shops.
set -eu
cd "$(dirname "$0")"
out=${1:?destination folder}
keep=${2:-14}
case "$keep" in ''|*[!0-9]*) echo "days to keep: a number" >&2; exit 2 ;; esac
[ "$keep" -ge 1 ] || { echo "days to keep: at least 1" >&2; exit 2; }
stamp=$(date -u +%Y%m%dT%H%M%SZ)
umask 077
mkdir -p "$out"
rm -f "$out"/*.dump.tmp
for database in bitmentor; do
  file="$out/$database-$stamp.dump"
  docker compose exec -T postgres pg_dump -U postgres -Fc "$database" > "$file.tmp"
  tables=$(docker compose exec -T postgres pg_restore --list < "$file.tmp" | grep -c ' TABLE [a-z_]* ' || true)
  if [ "${tables:-0}" -eq 0 ] ||
     ! docker compose exec -T postgres pg_restore -f - < "$file.tmp" > /dev/null; then
    rm -f "$out"/*-"$stamp".dump.tmp
    echo "the dump of $database does not open: no copy was kept, the old ones stay" >&2
    exit 1
  fi
  echo "$database: $(wc -c < "$file.tmp") bytes, $tables tables, read to the end"
done
# It opens: only now it gets its name
for database in bitmentor; do mv "$out/$database-$stamp.dump.tmp" "$out/$database-$stamp.dump"; done
find "$out" -name '*.dump' -mtime +"$keep" -delete
echo "$out: $stamp"
