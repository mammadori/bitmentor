#!/usr/bin/env bash
# On the backup destination, not on the site's machine: decides how long to keep the copies that deploy/backup.sh
# ships and never deletes. Keeps all the copies of the last DAYS days (30), the first of each month for MONTHS months
# (12), and always the latest three of each project, even if copies have stopped arriving. Deletes the others.
# Names are <project>-YYYYMMDDTHHMMSSZ.tar.age: the date in the name counts, not the date of the file. A name that is not
# one of these, or whose date is not a date, is left alone.
# It never deletes in one go what a wrong clock or invented files could make look old:
#   - a project whose latest copy is in the future (more than an hour), or is STALE_DAYS days old or more (3: copies
#     stopped coming, or the clock of this machine is ahead), is not touched: it says so and exits with 1. A project
#     that was retired on purpose says so with an empty file archive/.retired-<project> (the key of the ssh user cannot
#     make one): then its old copies age out as usual, the latest three staying;
#   - the latest three copies of a project are never deleted;
#   - at most half of a project's copies are deleted by one run, the oldest first; the next run goes on.
# Run it on the archive that archive-copies.sh fills (owned by root, not writable by the ssh user), never on the folder
# that the site's machine writes to. Usage, from cron once a day, as the user who owns the folder:
#   keep-backups.sh <directory> [--test]
# With --test it only prints what it would delete. DAYS, MONTHS and STALE_DAYS can be set in the environment.
# Exit code: 0 all done, 1 a project was left alone (see above), 2 wrong use.
set -euo pipefail
FOLDER=${1:?missing folder}
TEST=${2:-}
DAYS=${DAYS:-30}
MONTHS=${MONTHS:-12}
STALE_DAYS=${STALE_DAYS:-3}
[[ $DAYS =~ ^[1-9][0-9]*$ ]] && [[ $MONTHS =~ ^(0|[1-9][0-9]*)$ ]] || { echo "DAYS (at least 1) and MONTHS are numbers" >&2; exit 2; }
[[ $STALE_DAYS =~ ^[1-9][0-9]*$ ]] || { echo "STALE_DAYS is a number of days, at least 1" >&2; exit 2; }
[ -z "$TEST" ] || [ "$TEST" = --test ] || { echo "the only option is --test" >&2; exit 2; }
[ -d "$FOLDER" ] && [ ! -L "$FOLDER" ] || { echo "$FOLDER is not a folder" >&2; exit 2; }
# Only the owner of the archive may prune it: not group- or world-writable, and ours (as archive-copies.sh)
dir_mode=$(stat -c %a "$FOLDER")
{ [ "$(stat -c %u "$FOLDER")" = "$(id -u)" ] && [ $((8#$dir_mode & 8#022)) -eq 0 ]; } ||
  { echo "$FOLDER must belong to whoever runs this script and not be writable by others (now: $dir_mode)" >&2; exit 2; }
today=$(date -u +%s)
problems=0

name='^([a-z0-9][a-z0-9_-]*)-([0-9]{8})T([0-9]{6})Z\.tar\.age$'
declare -A copies_of day_of
projects=()
while IFS= read -r -d '' file; do
  [[ $file =~ $name ]] || continue  # digits only: anything else is not a copy of ours, and is left alone
  project=${BASH_REMATCH[1]} stamp=${BASH_REMATCH[2]}
  seconds=$(date -u -d "$stamp" +%s 2>/dev/null) || continue
  [ -n "${copies_of[$project]:-}" ] || projects+=("$project")
  copies_of[$project]+="$file"$'\n'
  day_of[$file]=$seconds
done < <(find "$FOLDER" -maxdepth 1 -type f -name '*-????????T??????Z.tar.age' -printf '%f\0' | LC_ALL=C sort -z)

for project in "${projects[@]}"; do
  mapfile -t list < <(printf '%s' "${copies_of[$project]}")  # in order: the latest is the last
  total=${#list[@]}
  latest=${list[total - 1]}
  # The hour of the latest copy, to see whether it is in the future; its day, to see how old it is
  stamp=${latest##*-}; stamp=${stamp%.tar.age}
  moment=$(date -u -d "${stamp:0:4}-${stamp:4:2}-${stamp:6:2} ${stamp:9:2}:${stamp:11:2}:${stamp:13:2}" +%s 2>/dev/null) || moment=$today
  age_latest=$(( (today - ${day_of[$latest]}) / 86400 ))
  if [ "$moment" -gt $((today + 3600)) ]; then
    echo "WARNING $project: the latest copy ($latest) has a date in the future: the clock of this machine is behind, or it is a fake copy. I delete nothing"
    problems=1; continue
  fi
  if [ "$age_latest" -ge "$STALE_DAYS" ] && [ ! -e "$FOLDER/.retired-$project" ]; then
    echo "WARNING $project: the latest copy is $age_latest days old ($latest): copies stopped arriving, or the clock of this machine is ahead. I delete nothing (if the project is retired: touch $FOLDER/.retired-$project)"
    problems=1; continue
  fi
  declare -A first=()
  to_delete=()
  for index in "${!list[@]}"; do
    file=${list[index]}
    month=${file##*-}; month=${month:0:6}
    age=$(( (today - ${day_of[$file]}) / 86400 ))
    monthly=0
    if [ -z "${first[$month]:-}" ]; then first[$month]=$file; monthly=1; fi
    if [ "$age" -lt "$DAYS" ] || { [ "$monthly" = 1 ] && [ "$age" -lt $((MONTHS * 31)) ]; } || [ "$index" -ge $((total - 3)) ]; then
      continue
    fi
    to_delete+=("$file")
  done
  limit=$(( total / 2 ))
  if [ "${#to_delete[@]}" -gt "$limit" ]; then
    echo "$project: ${#to_delete[@]} copies to delete out of $total: I delete only $limit (the oldest), the others next time"
    to_delete=("${to_delete[@]:0:limit}")
  fi
  for file in "${to_delete[@]}"; do
    if [ "$TEST" = --test ]; then echo "would delete $file"; else rm -f -- "${FOLDER:?}/$file"; echo "deleted $file"; fi
  done
done
exit "$problems"
