#!/usr/bin/env bash
# On the backup destination, not on the site's machine. As root, from cron every 15 minutes:
#   */15 * * * *  root  /usr/local/bin/archive-copies.sh /srv/backup/<project>/inbox /srv/backup/<project>/archive
#
# Why: the site's machine writes its copies with rsync over ssh with a key restricted by `rrsync -wo`. That key cannot
# read, but it CAN delete and overwrite what is in its folder (rsync --delete with an empty folder empties it; a file of
# the same name is replaced). Whoever takes over the machine has that key. So the folder the key writes to (inbox) is
# only a drop box, and the copies that count are moved by root to a folder the ssh user cannot write (archive).
#
# Everything in inbox comes from that key, so nothing in it is trusted. For each finished copy
# (<project>-YYYYMMDDTHHMMSSZ.tar.age, digits only in the date):
#   - if archive has a file of that name, the archive wins: the same content is fine and silent, a different one is
#     refused (exit 1); an archived copy is never overwritten, never deleted from here;
#   - an empty file with an archived name is the placeholder of that copy (see below): nothing to do;
#   - otherwise it must be a regular file, a name of at most 240 characters, with age's header, of at least 1000 bytes, whose date in the name is within the
#     last INBOX_DAYS+1 days and not in the future (a real copy arrives within hours; the machine keeps 14 days of them),
#     with at most PER_DAY (5: the night's, and a few made by hand) copies of that project for that day, and with room
#     on the disk (twice its size, plus MIN_FREE bytes, 1 GiB). Anything else is refused (exit 1): the dates and the
#     counts are what keep-backups.sh prunes by, and a file named by the key must not be able to push the real ones out;
#   - then it is copied into archive (as root, mode MODE, 600; group GROUP if set, so that the people who keep a key can
#     read it with MODE=640), and its sha256 is printed: it must be the one the site's machine wrote in its journal
#     (journalctl -u bitmentor-backup@<project>).
# A refused file is reported once (the first 20 of a run). What has the shape of a real copy (a valid name, the age header,
# 1000 bytes or more) is never deleted: it is moved out of inbox into refused (next to archive, root's; REFUSED_DIR to
# change it), where it stays REFUSED_DAYS days (30), whatever its size, until a person puts it into archive by hand
# (docs/mainnet.md, "Destination"). refused keeps under REFUSED_MAX_BYTES (1 GiB): over that, the oldest go, except the
# newest three, and a refused copy that would not fit stays in inbox and is reported again. What has not the shape of a
# copy (a name that is not one, an empty or small file, no age header) is deleted at once: it was invented.
# A copy that is archived is replaced in inbox by an empty file with its name and date: rsync --ignore-existing, on the
# machine's side, only needs the name, and this script does not read the same gigabytes again every quarter of an hour.
# The empty file is made inside inbox with mktemp and dated with touch -h, then renamed over the copy: root never opens,
# changes the mode of, or follows anything that has a name chosen by the key.
# Each run writes the time and the outcome in archive/.last-run (a cron job can look at how old it is).
# What this cannot know is what is inside: whoever has the key can send a file with a valid header and nothing in it, with
# a plausible name, up to PER_DAY a day. That can displace the first-of-the-month copy of the current month, and can fill
# the day's places so that the real copy of that day is refused. Only opening the copies notices (verify-copy.sh, where
# the key is).
# Copies in inbox older than INBOX_DAYS days (17) that are already archived are removed: keep this above
# BACKUP_LOCAL_DAYS of the site's machine (14), which sends every copy it still has that inbox does not. Half-written
# files (.name.XXXXXX, from an interrupted transfer) older than two days are removed too.
# To prune the archive: keep-backups.sh. To check that the copies open: verify-copy.sh.
# Usage: archive-copies.sh <inbox> <archive>
# Exit code: 0 all well, 1 something was refused or could not be compared, 2 wrong use or an archive that others can write.
set -euo pipefail
umask 077
INBOX=${1:?missing inbox folder}
ARCHIVE_DIR=${2:?missing archive folder}
INBOX_DAYS=${INBOX_DAYS:-17} PER_DAY=${PER_DAY:-5} MIN_FREE=${MIN_FREE:-1073741824}
REFUSED_DAYS=${REFUSED_DAYS:-30} REFUSED_MAX_BYTES=${REFUSED_MAX_BYTES:-1073741824}
MODE=${MODE:-600} GROUP=${GROUP:-}
for number in INBOX_DAYS PER_DAY MIN_FREE REFUSED_DAYS REFUSED_MAX_BYTES; do
  [[ ${!number} =~ ^(0|[1-9][0-9]*)$ ]] || { echo "$number: a number" >&2; exit 2; }
done
[[ $MODE =~ ^[0-7]{3}$ ]] || { echo "MODE: three octal digits, like 600 or 640" >&2; exit 2; }
[ -d "$INBOX" ] && [ ! -L "$INBOX" ] || { echo "$INBOX is not a folder" >&2; exit 2; }
[ -d "$ARCHIVE_DIR" ] && [ ! -L "$ARCHIVE_DIR" ] || { echo "$ARCHIVE_DIR is not a folder" >&2; exit 2; }
[ "$(cd "$INBOX" && pwd -P)" != "$(cd "$ARCHIVE_DIR" && pwd -P)" ] || { echo "inbox and archive are the same folder" >&2; exit 2; }
# The archive is worth something only if the ssh user cannot write in it: not group- or world-writable, and ours
dir_mode=$(stat -c %a "$ARCHIVE_DIR")
{ [ "$(stat -c %u "$ARCHIVE_DIR")" = "$(id -u)" ] && [ $((8#$dir_mode & 8#022)) -eq 0 ]; } ||
  { echo "$ARCHIVE_DIR must belong to whoever runs this script and not be writable by others (now: $dir_mode)" >&2; exit 2; }
REFUSED_DIR=${REFUSED_DIR:-$(dirname "$ARCHIVE_DIR")/refused}
# refused is ours: not a link (it would be somewhere else), and in a folder the ssh user cannot write
REFUSED_PARENT_MODE=$(stat -c %a "$(dirname "$REFUSED_DIR")" 2>/dev/null || echo 777)
if [ -L "$REFUSED_DIR" ] || [ "$(stat -c %u "$(dirname "$REFUSED_DIR")" 2>/dev/null || echo x)" != "$(id -u)" ] || [ $((8#$REFUSED_PARENT_MODE & 8#022)) -ne 0 ]; then
  echo "refused is not right (a link, or in a folder that belongs to others or is writable by others): refused files stay in inbox"
  REFUSED_DIR=''
fi
# One run at a time: a run stuck on something odd must not pile up with the next ones
exec 9> "$ARCHIVE_DIR/.archive.lock"
flock -n 9 || { echo "another run is still going" >&2; exit 0; }

today=$(date -u +%s)
problems=0 archived_count=0 refused_count=0 reported=0
new_file=''
write_run() {  # the outcome of this run, for whoever looks at the archive from outside
  printf '%s %s archived=%d refused=%d\n' "$(date -u +%FT%TZ)" "$([ "$problems" = 0 ] && echo ok || echo problems)" \
    "$archived_count" "$refused_count" > "$ARCHIVE_DIR/.last-run" 2>/dev/null || true
}
finish() {
  local result=$?
  [ -z "$new_file" ] || rm -f -- "$new_file" 2>/dev/null || true  # a name too long for rm must not stop the rest
  [ "$result" = 0 ] || problems=1  # a run that stopped on an error is not "ok"
  write_run
}
trap finish EXIT
# refuse <word> <file> <why> [keep]: reported (the first 20 of a run). Then: with "keep" the file stays in inbox; one
# that has not the shape of a real copy is deleted; one that has it is moved into refused (never deleted here)
refused_bytes=0
if [ -n "$REFUSED_DIR" ] && [ -d "$REFUSED_DIR" ]; then
  refused_bytes=$(find "$REFUSED_DIR" -maxdepth 1 -type f -printf '%s\n' | awk '{ sum += $1 } END { print sum + 0 }')
fi
refuse() {
  problems=1
  reported=$((reported + 1))
  # %q: a name made of newlines cannot forge a line of the output
  if [ "$reported" -le 20 ]; then printf '%s %q: %s\n' "$1" "$2" "$3"; fi
  [ "${4:-}" != keep ] || return 0
  refused_count=$((refused_count + 1))
  local inbox_path=$INBOX/$2 file_size=0 real=0 safe=${2//[^A-Za-z0-9._-]/_} destination
  if [[ $2 =~ $name_re ]] && [ "${#2}" -le 240 ] && [ -f "$inbox_path" ] && [ ! -L "$inbox_path" ]; then
    file_size=$(stat -c %s -- "$inbox_path" 2>/dev/null || echo 0)
    if [ "$file_size" -ge 1000 ] && [ "$(timeout 60 head -c 21 "$inbox_path" 2>/dev/null)" = "age-encryption.org/v1" ]; then real=1; fi
  fi
  if [ "$real" = 0 ]; then rm -f -- "$inbox_path" 2>/dev/null || true; return 0; fi  # invented: gone
  [ -n "$REFUSED_DIR" ] || return 0  # no place for it: it stays where it is
  if [ $((refused_bytes + file_size)) -gt "$REFUSED_MAX_BYTES" ]; then
    printf 'REFUSED_FULL %q: refused already has %d bytes (the cap is %d): the copy stays in inbox\n' "$2" "$refused_bytes" "$REFUSED_MAX_BYTES"
    return 0
  fi
  destination=$REFUSED_DIR/${safe:0:100}.$today.$refused_count
  # mv renames the entry and never follows it; the folder is ours and private
  if mkdir -p -m 700 "$REFUSED_DIR" 2>/dev/null && [ "$(stat -c %u "$REFUSED_DIR")" = "$(id -u)" ] && mv -T -- "$inbox_path" "$destination" 2>/dev/null; then
    touch -h -- "$destination" 2>/dev/null || true  # its age from now: the sender chose its date
    refused_bytes=$((refused_bytes + file_size))
  fi
}
name_re='^([a-z0-9][a-z0-9_-]*)-([0-9]{8})T([0-9]{6})Z\.tar\.age$'
# An archived copy is replaced in the drop box by an empty file with its name and its date: it is all that rsync
# --ignore-existing needs. The empty file is made by mktemp (O_EXCL, a name nobody can guess), dated with touch -h (which
# never follows a link) and renamed over the old one, all inside the drop box, which may be another filesystem than the
# archive. No chmod, no name chosen by the key: a link or a FIFO put there cannot make root touch anything
placeholder() {  # placeholder <path in inbox>
  local empty
  empty=$(mktemp "$INBOX/.empty.XXXXXX" 2>/dev/null) || return 0
  touch -h -d "@$(stat -c %Y -- "$1" 2>/dev/null || echo "$today")" -- "$empty" 2>/dev/null || true
  mv -T -f -- "$empty" "$1" 2>/dev/null || rm -f -- "$empty" 2>/dev/null || true
}

while IFS= read -r -d '' path; do
  file=${path##*/}
  [[ $file =~ $name_re ]] || { refuse REFUSED "$file" "the name is not <project>-YYYYMMDDTHHMMSSZ.tar.age"; continue; }
  # .NAME.new must fit in 255 bytes too
  [ "${#file}" -le 240 ] || { refuse REFUSED "$file" "the name is too long (more than 240 characters)"; continue; }
  project=${BASH_REMATCH[1]} day=${BASH_REMATCH[2]} hour=${BASH_REMATCH[3]}
  if [ -e "$ARCHIVE_DIR/$file" ] || [ -L "$ARCHIVE_DIR/$file" ]; then
    [ -s "$path" ] || continue  # an empty file with an archived name is the placeholder of this very copy
    # 0 same, 1 different, 124 it took too long (a file that blocks): only a real difference is refused
    comparison=0; timeout 300 cmp -s "$path" "$ARCHIVE_DIR/$file" || comparison=$?
    case "$comparison" in
      0) ;;
      1) refuse DIFFERENT "$file" "archive has a file with the same name and other content; archive stays as it is" ;;
      *) refuse NOTCOMPARABLE "$file" "the comparison with the copy in archive did not finish (code $comparison)" keep ;;
    esac
    [ "$comparison" != 0 ] || placeholder "$path"
    continue
  fi
  moment=$(date -u -d "${day:0:4}-${day:4:2}-${day:6:2} ${hour:0:2}:${hour:2:2}:${hour:4:2}" +%s 2>/dev/null) || moment=
  if [ -z "$moment" ] || [ "$moment" -gt $((today + 3600)) ] || [ "$moment" -lt $((today - (INBOX_DAYS + 1) * 86400)) ]; then
    refuse REFUSED "$file" "the date in the name is not a date, is in the future or is older than $((INBOX_DAYS + 1)) days"; continue
  fi
  size=$(stat -c %s "$path" 2>/dev/null) || continue  # gone since find saw it
  if [ "$size" -lt 1000 ] || [ "$(timeout 60 head -c 21 "$path")" != "age-encryption.org/v1" ]; then
    refuse REFUSED "$file" "not an age file of a plausible size ($size bytes)"; continue
  fi
  already=$(find "$ARCHIVE_DIR" -maxdepth 1 -type f -name "$project-${day}T??????Z.tar.age" | wc -l)
  if [ "$already" -ge "$PER_DAY" ]; then
    refuse REFUSED "$file" "archive already has $already copies of $project for that day (at most $PER_DAY)"; continue
  fi
  free=$(df -B1 --output=avail "$ARCHIVE_DIR" | tail -n 1 | tr -d ' ')
  if [ "$free" -lt $((2 * size + MIN_FREE)) ]; then
    refuse REFUSED "$file" "little space in archive ($free bytes free)" keep; continue  # not its fault: it stays, for later
  fi
  # cp -P never follows a link: a link put in inbox after the checks above is copied as a link, and refused below
  new_file=$ARCHIVE_DIR/.$file.new
  if ! timeout 1800 cp -P --no-preserve=mode,ownership -- "$path" "$new_file"; then
    rm -f -- "$new_file" 2>/dev/null || true; new_file=''; refuse REFUSED "$file" "it could not be copied" keep; continue
  fi
  if [ -L "$new_file" ] || [ ! -f "$new_file" ] || [ "$(timeout 60 head -c 21 "$new_file")" != "age-encryption.org/v1" ]; then
    rm -f -- "$new_file" 2>/dev/null || true; new_file=''; refuse REFUSED "$file" "changed while I was copying it"; continue
  fi
  chmod "$MODE" "$new_file"
  [ -z "$GROUP" ] || chgrp "$GROUP" "$new_file"
  mv -n -- "$new_file" "$ARCHIVE_DIR/$file"; new_file=''
  archived_count=$((archived_count + 1))
  printf 'archived %q: %s bytes, sha256 %s\n' "$file" "$(stat -c %s "$ARCHIVE_DIR/$file")" "$(sha256sum "$ARCHIVE_DIR/$file" | cut -d' ' -f1)"
  placeholder "$path"
done < <(find "$INBOX" -maxdepth 1 -type f -name '*-????????T??????Z.tar.age' -print0 | LC_ALL=C sort -z)
[ "$reported" -le 20 ] || printf 'and %d more refusals, not printed here\n' $((reported - 20))

# An archived copy leaves inbox, as an empty file, after INBOX_DAYS days
while IFS= read -r -d '' path; do
  file=${path##*/}
  [ -f "$ARCHIVE_DIR/$file" ] && rm -f -- "$path"
done < <(find "$INBOX" -maxdepth 1 -type f -empty -name '*-????????T??????Z.tar.age' -mtime +"$INBOX_DAYS" -print0)
find "$INBOX" -maxdepth 1 -type f -name '.*' -mtime +1 -delete
find "$INBOX" -maxdepth 1 ! -type f ! -type d -mtime +1 -delete  # links and the like: not copies, and root never follows them

# What was refused is kept for a while, and no more than a gigabyte of it
if [ -d "$REFUSED_DIR" ] && [ "$(stat -c %u "$REFUSED_DIR")" = "$(id -u)" ]; then
  find "$REFUSED_DIR" -mindepth 1 -maxdepth 1 -mtime +"$REFUSED_DAYS" -exec rm -rf -- {} +  # directories too, if any
  total=$(find "$REFUSED_DIR" -maxdepth 1 -type f -printf '%s\n' | awk '{ sum += $1 } END { print sum + 0 }')
  if [ "$total" -gt "$REFUSED_MAX_BYTES" ]; then
    while read -r _ size old_name; do
      [ "$total" -gt "$REFUSED_MAX_BYTES" ] || break
      rm -f -- "${REFUSED_DIR:?}/$old_name"; total=$((total - size))
    done < <(find "$REFUSED_DIR" -maxdepth 1 -type f -printf '%T@ %s %f\n' | sort -n | head -n -3)  # not the newest three; names there are A-Za-z0-9._-
  fi
fi
exit "$problems"
