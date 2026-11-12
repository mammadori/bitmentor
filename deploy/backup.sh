#!/usr/bin/env bash
# Daily copy of a machine. bitmentor-backup@<project>.timer runs it.
# Saves the site's database and the files of the folder (.env,
# release.env, alerts.env, backup.env, compose.locale.yml, state/). Reads everything back before encrypting (a copy that
# does not open is an error, not a copy), encrypts it with age to the public keys in backup.env, checks the encrypted
# file, and ships it off the machine with rsync over ssh. Only the encrypted copies stay here, for BACKUP_LOCAL_DAYS
# days: the private key that opens them is not on this machine, so the decryption test is done where the key is
# (deploy/examples/verify-copy.sh).
# From here, remote copies are only added, never deleted: whoever takes over this machine must not be able to delete
# those too. The destination decides how long to keep them (deploy/examples/keep-backups.sh).
# Does not copy the game files and the media (they have their own sources), or
# the certificates (the entry point has them).
# If something goes wrong, it alerts through check.sh and exits with an error; if all goes well, it writes the time
# (and the name, size and sha256 of the copy) to state/backup.ok.
# Usage: backup.sh <directory> [--here-only], the directory containing .env, release.env and backup.env. Restore: docs/mainnet.md.
# With --here-only, by hand, it makes and checks the copy and does not ship it: for whoever has no destination yet and takes
# the file to a computer with scp (docs/operations.md, "Take a copy off the machine"). It writes nothing in state/ when it works and prunes
# nothing: delete the copy after taking it (an error still leaves backup.error). The timer never runs it so.
#
# An installation from the sources (a clone, no release.env, no current/) can run it too, for a test or a development
# machine: the project is the folder's name (or BACKUP_PROJECT), age may run in a container (BACKUP_AGE) and the copies may
# go to a folder on the same machine (BACKUP_TEST_FOLDER). A release machine (one with release.env) refuses
# BACKUP_TEST_FOLDER: a copy on the same machine protects from nothing.
set -euo pipefail
DIR=${1:?missing folder}
HERE_ONLY=''
case "${2:-}" in '') ;; --here-only) HERE_ONLY=1 ;; *) echo "usage: backup.sh <directory> [--here-only]" >&2; exit 2 ;; esac
cd "$DIR"
DIR=$PWD
RELEASE=
if [ -f release.env ]; then
  . ./release.env
  : "${PROJECT:?}"
  RELEASE=1
fi

# backup.env is read line by line, without executing it, like alerts.env. Nothing else sets these: not the environment
BACKUP_RECIPIENTS='' BACKUP_REMOTE='' BACKUP_SSH_KEY='' BACKUP_SSH_KNOWN_HOSTS='' BACKUP_SSH_PORT=22
BACKUP_LOCAL_DAYS=14 BACKUP_AGE=age BACKUP_PROJECT='' BACKUP_TEST_FOLDER=''
if [ -f backup.env ]; then
  while IFS='=' read -r name value || [ -n "$name" ]; do  # also a last line without a newline
    case "$name" in
      BACKUP_RECIPIENTS|BACKUP_REMOTE|BACKUP_SSH_KEY|BACKUP_SSH_KNOWN_HOSTS|BACKUP_SSH_PORT|BACKUP_LOCAL_DAYS) ;;
      BACKUP_AGE|BACKUP_PROJECT|BACKUP_TEST_FOLDER) ;;
      *) continue ;;
    esac
    value=${value%$'\r'}; value=${value#[\"\']}; value=${value%[\"\']}
    printf -v "$name" '%s' "$value"
  done < backup.env
fi
[ -n "$RELEASE" ] || PROJECT=${BACKUP_PROJECT:-$(basename "$DIR")}
[[ $PROJECT =~ ^[a-z0-9][a-z0-9_-]*$ ]] || { echo "bad project name: $PROJECT"; exit 1; }

umask 077
mkdir -p state backup/daily
work='' tmp_encrypted=''
alert() {  # the alarms of check.sh, to the same recipients
  [ -x bin/check.sh ] && bin/check.sh "$DIR" --test "$PROJECT: $1" >/dev/null 2>&1 || true
}
finish() {
  local result=$?
  [ -z "$work" ] || rm -rf "$work"
  [ -z "$tmp_encrypted" ] || rm -f "$tmp_encrypted"  # only ours: another run may be writing its own
  if [ "$result" -ne 0 ]; then
    date -u +%FT%TZ > state/backup.error || true  # a full disk must not stop the alarm below
    alert "backup failed: journalctl -u bitmentor-backup@$(basename "$DIR")"
  fi
}
trap finish EXIT
trap 'exit 1' TERM INT HUP  # so that the exit trap runs, and the plaintext work folder goes away
fail() { echo "$*" >&2; exit 1; }

# What is checked before anything is made
if [ -n "$RELEASE" ] && [ "$BACKUP_AGE" != age ]; then
  # The whole plaintext goes through this command: on a release machine it is the age of the system, not a container
  fail "BACKUP_AGE does not apply on a release machine (release.env is there): apt install age"
fi
read -r -a age <<< "$BACKUP_AGE"
[ "${#age[@]}" -gt 0 ] || age=(age)
command -v "${age[0]}" >/dev/null || fail "${age[0]} is missing: apt install age (or BACKUP_AGE in backup.env)"
[ -n "$BACKUP_RECIPIENTS" ] || fail "BACKUP_RECIPIENTS is missing in backup.env: no plaintext copies"
# What is wrong with a key is said without printing it: a private key pasted here by mistake must not reach the journal
case "$BACKUP_RECIPIENTS" in
  *AGE-SECRET-KEY*) fail "BACKUP_RECIPIENTS contains a PRIVATE age key: remove it from backup.env, have whoever wrote it replace it, and private keys do not belong on this machine" ;;
esac
recipients=() distinct='' number=0
for key in $BACKUP_RECIPIENTS; do
  number=$((number + 1))
  [[ $key =~ ^age1[a-z0-9]{50,}$ ]] || fail "BACKUP_RECIPIENTS: key no. $number is not an age public key (age1…)"
  if [[ " $distinct " != *" $key "* ]]; then distinct+=" $key"; recipients+=(-r "$key"); fi
done
[ "${#recipients[@]}" -ge 4 ] || fail "BACKUP_RECIPIENTS: at least two different keys are needed, from two people: with only one, losing it means losing the copies"
# age itself must accept every key, now: an example key or a typo fails here, and not at the first night's copy
number=0
for key in $distinct; do
  number=$((number + 1))
  "${age[@]}" -r "$key" < /dev/null > /dev/null 2>&1 || fail "BACKUP_RECIPIENTS: age does not accept key no. $number (an example key, or one with a typo)"
done
[[ $BACKUP_LOCAL_DAYS =~ ^[1-9][0-9]*$ ]] || fail "BACKUP_LOCAL_DAYS: a number of days, at least 1, no leading zeros ($BACKUP_LOCAL_DAYS)"
[[ $BACKUP_SSH_PORT =~ ^[1-9][0-9]*$ ]] || fail "BACKUP_SSH_PORT: a number ($BACKUP_SSH_PORT)"
# The destination is another machine: user@host:folder. A plain path would be a folder of this one, and count as shipped
if [ -n "$BACKUP_REMOTE" ]; then
  destination_re='^[A-Za-z_][A-Za-z0-9_.-]*@([A-Za-z0-9][A-Za-z0-9.-]*|\[[0-9A-Fa-f:.]+\]):[A-Za-z0-9._/~-]*$'
  [[ $BACKUP_REMOTE =~ $destination_re ]] || fail "BACKUP_REMOTE: user@host:folder and nothing else (no rsync://, no plain paths, no ::)"
fi
for value in "$BACKUP_REMOTE" "$BACKUP_SSH_KEY" "$BACKUP_SSH_KNOWN_HOSTS"; do
  [[ $value != *[[:space:]]* ]] || fail "BACKUP_REMOTE, BACKUP_SSH_KEY and BACKUP_SSH_KNOWN_HOSTS cannot contain spaces"
done
if [ -n "$BACKUP_TEST_FOLDER" ]; then
  [ -z "$RELEASE" ] || fail "BACKUP_TEST_FOLDER does not apply on a release machine (release.env is there): the copy must leave the machine, BACKUP_REMOTE"
  [ -z "$BACKUP_REMOTE" ] || fail "BACKUP_TEST_FOLDER and BACKUP_REMOTE together: pick one"
  case "$BACKUP_TEST_FOLDER" in /*) ;; *) fail "BACKUP_TEST_FOLDER: an absolute path" ;; esac
fi

# One backup at a time, for the whole run (the release lock below is let go once the dumps are made)
exec 8>state/.backup.lock
if ! flock -n 8; then
  trap - EXIT
  # The timer's run finding another one has nothing to do; a hand run (--here-only) that did not make its copy says so
  if [ -n "$HERE_ONLY" ]; then echo "another copy is already running: yours was not made, try again shortly"; exit 1; fi
  echo "another copy is already running: this one does not start"; exit 0
fi
# A leftover of a killed run (the copy being written, or the work folder) is old by now: ours is the only run
find backup/daily -maxdepth 1 -name '*.tmp' -mmin +1440 -delete 2>/dev/null || true
# While a release runs, its migrations change the database: wait for it to end, and meanwhile no release starts
exec 9>state/.lock
flock -w 900 9 || fail "the folder has been busy for a quarter of an hour"

# After a boot the timer's catch-up run can start before the containers do: wait a few minutes for them
for attempt in $(seq 1 30); do
  postgres=$(docker ps -q --filter "label=com.docker.compose.project=$PROJECT" --filter "label=com.docker.compose.service=postgres" | head -n 1) || true
  if [ -n "$postgres" ] && docker exec "$postgres" pg_isready -U postgres -q; then break; fi
  [ "$attempt" -lt 30 ] || fail "postgres is not running, or does not answer, for 5 minutes"
  sleep 10
done

# A work folder left by a run that was killed holds plaintext: gone after a day (a run in progress is never that old)
find backup -maxdepth 1 -name '.work.*' -mmin +1440 -exec rm -rf {} + 2>/dev/null || true
# Room for the work folder (plaintext, as big as a copy) and for the copy: three times the largest copy kept here, at
# least 512 MiB. When the shipments fail the copies pile up here: this stops them before the disk is full, and alerts
largest=$(find backup/daily -name "$PROJECT-*.tar.age" -printf '%s\n' | sort -n | tail -n 1)
free=$(df -B1 --output=avail backup | tail -n 1 | tr -d ' ')
[ "$free" -ge $((3 * ${largest:-0} + 536870912)) ] || fail "little space in $DIR/backup ($free bytes free, the largest copy is ${largest:-0}): are the shipments failing? Make room and look at journalctl -u bitmentor-backup@$(basename "$DIR")"

stamp=$(date -u +%Y%m%dT%H%M%SZ)
work=$(mktemp -d "$DIR/backup/.work.XXXXXX")
mkdir "$work/database" "$work/folder"
docker exec "$postgres" pg_dump -U postgres -Fc bitmentor > "$work/database/bitmentor.dump"
# Postgres users, with their encrypted passwords: on a restore onto new volumes postgres-init.sh makes them again from the .env
docker exec "$postgres" pg_dumpall -U postgres --roles-only > "$work/database/roles.sql"
for file in .env release.env alerts.env backup.env compose.locale.yml; do
  if [ -f "$file" ]; then cp -p "$file" "$work/folder/"; fi
done
tar -C "$DIR" --exclude=state/.lock --exclude=state/.backup.lock -cf "$work/folder/state.tar" state
flock -u 9

# --- Everything is read back before it is encrypted -----------------------------------------------------------------
# --list reads the table of contents only, and a dump cut near its end passes it: pg_restore -f - reads every block
# shellcheck disable=SC2043  # a list of one, kept as a list for the next one
for database in bitmentor; do
  file=$work/database/$database.dump
  [ -s "$file" ] || fail "the $database dump is empty"
  entries=$(docker exec -i "$postgres" pg_restore --list < "$file") || fail "the $database dump cannot be read (pg_restore --list)"
  tables=$(grep -c ' TABLE [a-z_]* ' <<< "$entries" || true)
  [ "$tables" -gt 0 ] || fail "the $database dump has no tables"
  docker exec -i "$postgres" pg_restore -f - < "$file" > /dev/null || fail "the $database dump is truncated or damaged (pg_restore)"
  echo "$database: $(stat -c %s "$file") bytes, $tables tables, read in full"
done
grep -q '^CREATE ROLE ' "$work/database/roles.sql" || fail "roles.sql has no users"
# shellcheck disable=SC2043  # a list of one, kept as a list for the next one
for archive in folder/state:state; do
  file=$work/${archive%%:*}.tar
  [ -s "$file" ] || fail "${archive%%:*}.tar is empty"
  list=$(tar -tf "$file") || fail "${archive%%:*}.tar is truncated or damaged"  # all of it: no head, which would break the pipe
  first=${list%%$'\n'*}
  [[ $first == "${archive##*:}/"* ]] || fail "${archive%%:*}.tar does not start with ${archive##*:}/ (it starts with «$first»)"
done
[ -s "$work/folder/.env" ] || fail "the .env is missing: without it the copy is useless"
# What the copy holds, with a hash of each file: whoever opens it can tell that nothing is missing or changed
# shellcheck disable=SC2094  # find skips SHA256SUMS, the file written
(cd "$work" && find . -type f ! -name SHA256SUMS -print0 | LC_ALL=C sort -z | xargs -0 sha256sum > SHA256SUMS)
contents=$(find "$work" -type f -printf '%s\n' | awk '{ sum += $1 } END { print sum + 0 }')

name="$PROJECT-$stamp.tar.age"
encrypted="backup/daily/$name"
tmp_encrypted=$encrypted.tmp
tar -C "$work" -cf - . | "${age[@]}" "${recipients[@]}" > "$tmp_encrypted"
rm -rf "$work"; work=''

# --- The encrypted file ---------------------------------------------------------------------------------------------
# It cannot be decrypted here (the private key is elsewhere), but it must be an age file of a plausible size: age does
# not compress, so it is the files plus tar's headers and age's, and never less than the files
[ "$(head -c 21 "$encrypted.tmp")" = "age-encryption.org/v1" ] || fail "$name does not have the age header"
size=$(stat -c %s "$encrypted.tmp")
[ "$size" -gt "$contents" ] || fail "$name is smaller than what it holds ($size bytes, content $contents): truncated?"
[ "$size" -lt $((contents + contents / 100 + 1048576)) ] || fail "$name is too big ($size bytes, content $contents)"
mv "$tmp_encrypted" "$encrypted"; tmp_encrypted=
fingerprint=$(sha256sum "$encrypted" | cut -d' ' -f1)
echo "$name: $size bytes, sha256 $fingerprint"

if [ -n "$HERE_ONLY" ]; then
  echo "made and checked, not shipped (--here-only): $DIR/$encrypted"
  echo "take it to a computer with scp, compare the hash, open it there with the key, and then delete it from here"
  exit 0
fi

# --- Off the machine ------------------------------------------------------------------------------------------------
if [ -n "$BACKUP_TEST_FOLDER" ]; then
  # A test machine without a destination: another folder of the same machine. It protects from a mistake, not from a failure
  mkdir -p "$BACKUP_TEST_FOLDER"
  for copy in backup/daily/"$PROJECT"-*.tar.age; do
    [ -e "$BACKUP_TEST_FOLDER/${copy##*/}" ] && continue
    cp -p "$copy" "$BACKUP_TEST_FOLDER/.${copy##*/}.tmp"
    mv "$BACKUP_TEST_FOLDER/.${copy##*/}.tmp" "$BACKUP_TEST_FOLDER/${copy##*/}"
  done
  echo "copied to $BACKUP_TEST_FOLDER: it is on this machine, it does not protect from a failure"
else
  [ -n "$BACKUP_REMOTE" ] || fail "BACKUP_REMOTE is missing in backup.env: the copy stays only on this machine"
  command -v rsync >/dev/null || fail "rsync is missing: apt install rsync"
  # No ControlMaster: a connection that stays behind would keep the lock of this run, which ssh inherits
  ssh_command=(ssh -p "$BACKUP_SSH_PORT" -o BatchMode=yes -o StrictHostKeyChecking=yes -o ConnectTimeout=30
    -o ServerAliveInterval=30 -o ServerAliveCountMax=4 -o ControlMaster=no -o ControlPath=none)
  if [ -n "$BACKUP_SSH_KEY" ]; then ssh_command+=(-i "$BACKUP_SSH_KEY" -o IdentitiesOnly=yes); fi
  if [ -n "$BACKUP_SSH_KNOWN_HOSTS" ]; then ssh_command+=(-o "UserKnownHostsFile=$BACKUP_SSH_KNOWN_HOSTS"); fi
  # All the local copies: if yesterday's shipment failed, it goes today. Those already there are not written again
  rsync -t --ignore-existing --timeout=600 -e "${ssh_command[*]}" backup/daily/"$PROJECT"-*.tar.age "$BACKUP_REMOTE" 8>&- 9>&-
  echo "shipped to $BACKUP_REMOTE"
fi

# Local copies are pruned only after a successful shipment (-mmin, not -mtime: 14 days are 14 days, not 15)
find backup/daily -name "$PROJECT-*.tar.age" -mmin +$((BACKUP_LOCAL_DAYS * 1440)) -delete
printf '%s\n%s %s bytes sha256 %s\n' "$(date -u +%FT%TZ)" "$name" "$size" "$fingerprint" > state/backup.ok
rm -f state/backup.error
