#!/usr/bin/env bash
# Tests the backup scripts without docker, age, ssh or a network: deploy/backup.sh, backup.sh (from the sources),
# deploy/examples/keep-backups.sh, archive-copies.sh and verify-copy.sh. The real scripts run in temporary folders; docker,
# age, rsync and pg_restore are the fakes of deploy/tests/fakes-backup, which log their calls and answer as the scenario
# asks. With the real age installed, one more test makes a copy with it and opens it. The proof with the real programs
# (Postgres, ssh, rrsync) is in docs/mainnet.md, "What has been tested".
# Usage: bash deploy/tests/test_backup.sh        exits with 0 if everything passes
set -uo pipefail
HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
DEPLOY=$(dirname "$HERE")
BASE=$(dirname "$DEPLOY")
FAKES=$HERE/fakes-backup
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT
PASSED=0 FAILED=0
OUT='' CODE=0

no() { FAILED=$((FAILED + 1)); echo "  NO: $1"; printf '%s\n' "${OUT:-}" | tail -n 8 | sed 's/^/        | /'; }
yes() { PASSED=$((PASSED + 1)); }
holds() { local what=$1; shift; if "$@" >/dev/null 2>&1; then yes; else no "$what"; fi; }
fails() { local what=$1; shift; if "$@" >/dev/null 2>&1; then no "$what"; else yes; fi; }
same() { if [ "$2" = "$3" ]; then yes; else no "$1: expected «$2», found «$3»"; fi; }
has() { if [[ $2 == *"$3"* ]]; then yes; else no "$1: «$3» missing"; fi; }
lacks() { if [[ $2 == *"$3"* ]]; then no "$1: «$3» found"; else yes; fi; }
title() { echo "== $*"; }

KEY_A=age1$(printf 'q%.0s' $(seq 58))
KEY_B=age1$(printf 'w%.0s' $(seq 58))

machine() {  # machine <name> [sources]: a new /opt/<project> with the fakes ready; with "sources" it has no release.env
  M=$TMP/$1 D=$TMP/$1/opt/$1 F=$TMP/$1/fake
  rm -rf "$M"; mkdir -p "$D/state" "$D/bin" "$F"
  cp "$FAKES/check.sh" "$D/bin/check.sh"
  printf 'SECRET_KEY=not-a-secret\nPOSTGRES_PASSWORD=not-either\n' > "$D/.env"
  [ "${2:-}" = sources ] || printf 'PROJECT=%s\nSITE=test.example.org\n' "$1" > "$D/release.env"
  printf 'BACKUP_RECIPIENTS="%s %s"\nBACKUP_REMOTE=copies@destination.example.net:\nBACKUP_SSH_KEY=/key/of/test\nBACKUP_SSH_PORT=2222\n' \
    "$KEY_A" "$KEY_B" > "$D/backup.env"
  export FAKE_DIR=$F FAKE_CALLS=$M/calls.log
  : > "$FAKE_CALLS"
  PROJECT=$1
}
run() {  # run <script> <args...>: OUT and CODE, with the fakes first in the PATH
  OUT=$(PATH="$FAKES:$TMP/extra:$PATH" "$@" 2>&1); CODE=$?
}
backup() { run bash "$DEPLOY/backup.sh" "$D"; }
copies() { ls "$D"/backup/daily/"$PROJECT"-*.tar.age 2>/dev/null; }
last_copy() { copies | tail -n 1; }
open_copy() {  # open_copy <file> <folder>: what the fake age leaves, as tar
  mkdir -p "$2"; tail -n +4 "$1" | tar -x -C "$2"
}
backup_env() { printf '%s\n' "$@" >> "$D/backup.env"; }

title "a good run"
machine shops
backup
same "ends well" 0 "$CODE"
same "one copy" 1 "$(copies | wc -l)"
holds "named <project>-<date>.tar.age" bash -c "[[ \$(basename $(last_copy)) =~ ^shops-[0-9]{8}T[0-9]{6}Z\.tar\.age\$ ]]"
holds "private to its owner" test "$(stat -c %a "$(last_copy)")" = 600
holds "it begins with the age header" test "$(head -c 21 "$(last_copy)")" = age-encryption.org/v1
has "it says the size and the hash" "$OUT" "bytes, sha256 "
has "bitmentor read to the end" "$OUT" "bitmentor: "
has "two tables" "$OUT" "2 tables, read in full"
holds "pg_restore --list for the dump" test "$(grep -c 'pg_restore --list' "$FAKE_CALLS")" = 1
holds "and the full read for the dump" test "$(grep -c 'pg_restore -f -' "$FAKE_CALLS")" = 1
holds "age gets both keys" grep -q -- "-r $KEY_A -r $KEY_B" "$FAKE_CALLS"
holds "rsync only adds: --ignore-existing" grep -q -- 'rsync -t --ignore-existing --timeout=600' "$FAKE_CALLS"
holds "ssh checks the host key" grep -q 'StrictHostKeyChecking=yes' "$FAKE_CALLS"
holds "ssh starts no ControlMaster that would keep the lock" grep -q 'ControlMaster=no -o ControlPath=none' "$FAKE_CALLS"
holds "rsync gets neither lock descriptor" grep -q 'rsync fd8=closed fd9=closed' "$FAKE_CALLS"
holds "ssh key and port" grep -q -- '-p 2222.*-i /key/of/test -o IdentitiesOnly=yes' "$FAKE_CALLS"
holds "ssh does not hang for ever" grep -q 'ServerAliveInterval=30' "$FAKE_CALLS"
holds "the destination is BACKUP_REMOTE" grep -q 'copies@destination.example.net:$' "$FAKE_CALLS"
holds "the copy arrived" test -f "$F/remote/$(basename "$(last_copy)")"
holds "backup.ok" test -s "$D/state/backup.ok"
same "backup.ok: the time first" 1 "$(head -n 1 "$D/state/backup.ok" | grep -cE '^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9:]{8}Z$')"
has "backup.ok: the name of the copy" "$(cat "$D/state/backup.ok")" "$(basename "$(last_copy)")"
fails "no backup.error" test -e "$D/state/backup.error"
fails "no plaintext work folder left" bash -c "ls -d $D/backup/.work.*"
fails "no .tmp left" bash -c "ls $D/backup/daily/*.tmp"
fails "no alarm" grep -q '^check' "$FAKE_CALLS"
open_copy "$(last_copy)" "$M/opened"
for file in database/bitmentor.dump database/roles.sql folder/.env folder/release.env folder/backup.env folder/state.tar SHA256SUMS; do
  holds "inside: $file" test -s "$M/opened/$file"
done
holds "inside: the hashes are right" bash -c "cd $M/opened && sha256sum --quiet -c SHA256SUMS"
holds "inside: the .env is the machine's" cmp "$D/.env" "$M/opened/folder/.env"
fails "inside: no lock file in state.tar" bash -c "tar -tf $M/opened/folder/state.tar | grep -q lock"

title "copies in the folder: local pruning only after a good shipment"
machine shops
mkdir -p "$D/backup/daily"
for age in 3 6 13 15 20; do
  stamp=$(date -u -d "$age days ago 00:00:00" +%Y%m%dT%H%M%SZ)
  : > "$D/backup/daily/shops-$stamp.tar.age"; touch -d "$age days ago" "$D/backup/daily/shops-$stamp.tar.age"
done
: > "$D/backup/daily/other-20200101T000000Z.tar.age"; touch -d "400 days ago" "$D/backup/daily/other-20200101T000000Z.tar.age"
touch "$F/rsync_ko"
backup
fails "rsync fails: the run fails" test "$CODE" = 0
holds "...error written" test -s "$D/state/backup.error"
has "...an alarm with the project's name" "$(cat "$FAKE_CALLS")" "check $D --test shops: backup failed"
same "...no copy deleted" 6 "$(copies | wc -l)"
holds "...the 20 days old one is still here" test -n "$(find "$D/backup/daily" -name 'shops-*.tar.age' -mtime +19)"
holds "...no backup.ok" test ! -e "$D/state/backup.ok"
rm "$F/rsync_ko"
sleep 1  # the copy of the failed run and this one have two names
backup
same "a good shipment: ends well" 0 "$CODE"
same "...keeps 14 days: of the 7 copies it keeps the 3, 6 and 13 days old and the two made now, not the 15 and 20 days old" 5 "$(copies | wc -l)"
holds "...the other project's copy is not touched" test -f "$D/backup/daily/other-20200101T000000Z.tar.age"
holds "...error gone" test ! -e "$D/state/backup.error"
same "...all the copies of the project went to the destination (the five old ones, the one of the failed run, the new one)" 7 "$(ls "$F/remote" | wc -l)"
backup_env BACKUP_LOCAL_DAYS=30
touch "$D/backup/daily/shops-20200101T000000Z.tar.age"; touch -d "29 days ago" "$D/backup/daily/shops-20200101T000000Z.tar.age"
backup
holds "BACKUP_LOCAL_DAYS=30 keeps 29 days" test -f "$D/backup/daily/shops-20200101T000000Z.tar.age"
touch -d "31 days ago" "$D/backup/daily/shops-20200101T000000Z.tar.age"
backup
holds "...and not 31" test ! -e "$D/backup/daily/shops-20200101T000000Z.tar.age"
for bad in "0" "seven" "7 days" "-1"; do
  machine shops; backup_env "BACKUP_LOCAL_DAYS=$bad"
  backup
  fails "BACKUP_LOCAL_DAYS=«$bad» is refused" test "$CODE" = 0
  has "...before anything is copied" "$OUT" "BACKUP_LOCAL_DAYS"
  same "...no docker call" 0 "$(grep -c '^docker' "$FAKE_CALLS")"
done

title "--here-only: a copy made by hand for scp, with no destination yet"
machine shops; sed -i '/^BACKUP_REMOTE/d' "$D/backup.env"
mkdir -p "$D/backup/daily"; : > "$D/backup/daily/shops-20200101T000000Z.tar.age"; touch -d "30 days ago" "$D/backup/daily/shops-20200101T000000Z.tar.age"
run bash "$DEPLOY/backup.sh" "$D" --here-only
same "ends well without BACKUP_REMOTE" 0 "$CODE"
has "says where the file is" "$OUT" "not shipped (--here-only)"
same "the copy is there (and the old one too)" 2 "$(copies | wc -l)"
fails "nothing shipped" grep -q '^rsync' "$FAKE_CALLS"
fails "no backup.ok: it is not a night's copy" test -e "$D/state/backup.ok"
fails "no backup.error" test -e "$D/state/backup.error"
holds "nothing pruned" test -f "$D/backup/daily/shops-20200101T000000Z.tar.age"
run bash "$DEPLOY/backup.sh" "$D" --other; same "any other argument: wrong use" 2 "$CODE"
run bash "$DEPLOY/backup.sh" "$D"
fails "without --here-only the missing destination is still an error" test "$CODE" = 0

title "a copy that does not open is an error"
scenario() {  # scenario <name> <file in $F> <text expected in the output>
  machine shops; : > "$F/$2"
  backup
  fails "$1: the run fails" test "$CODE" = 0
  has "$1: it says why" "$OUT" "$3"
  same "$1: no copy kept" 0 "$(copies | wc -l)"
  fails "$1: no .tmp, no plaintext folder" bash -c "ls $D/backup/daily/*.tmp $D/backup/.work.* 2>/dev/null"
  holds "$1: backup.error" test -s "$D/state/backup.error"
  holds "$1: the alarm" grep -q "^check $D --test shops: backup failed" "$FAKE_CALLS"
  fails "$1: nothing shipped" grep -q '^rsync' "$FAKE_CALLS"
  fails "$1: no backup.ok" test -e "$D/state/backup.ok"
}
scenario "pg_dump fails" pg_dump_ko ""
scenario "an empty dump" dump_empty "is empty"
scenario "a dump cut near its end (--list reads it)" dump_truncated "truncated or damaged"
scenario "age writes no header" age_no_header "does not have the age header"
scenario "age writes less than the files" age_truncated "smaller than what it holds"
machine shops; rm "$D/.env"
backup
fails "no .env: fails" test "$CODE" = 0
has "...says why" "$OUT" "the .env is missing"

title "values that look like numbers and are not"
for bad in "08" "007" "1e3"; do
  machine shops; backup_env "BACKUP_LOCAL_DAYS=$bad"
  backup
  fails "BACKUP_LOCAL_DAYS=$bad is refused (it would be read as octal)" test "$CODE" = 0
done
machine shops; backup_env "BACKUP_SSH_PORT=022"
backup; fails "a port with a leading zero is refused" test "$CODE" = 0
machine shops; printf 'BACKUP_LOCAL_DAYS=30' >> "$D/backup.env"; sed -i 's/^$//' "$D/backup.env"
printf '\nBACKUP_SSH_KNOWN_HOSTS=/last/line' >> "$D/backup.env"
backup
holds "the last line of backup.env, with no newline, counts" grep -q 'UserKnownHostsFile=/last/line' "$FAKE_CALLS"

title "the disk"
machine shops; echo 100000000 > "$F/df"
backup
fails "100 MB free: no copy (it needs 512 MiB at least)" test "$CODE" = 0
has "...says so" "$OUT" "little space"
same "...no dump started" 0 "$(grep -c 'pg_dump' "$FAKE_CALLS")"
holds "...an alarm" grep -q "^check $D --test shops: backup failed" "$FAKE_CALLS"
machine shops; mkdir -p "$D/backup/daily"; head -c 3000 /dev/zero > "$D/backup/daily/shops-20260101T000000Z.tar.age"
echo $((3 * 3000 + 536870912 - 1)) > "$F/df"; backup
fails "free space one byte short of 3x the largest copy + 512 MiB: refused" test "$CODE" = 0
echo $((3 * 3000 + 536870912)) > "$F/df"; backup
same "...and exactly that much: it goes" 0 "$CODE"

title "BACKUP_AGE on a release machine"
machine shops; backup_env "BACKUP_AGE=docker run --rm -i bitmentor/age:latest"
backup
fails "refused where there is a release.env" test "$CODE" = 0
has "...says so" "$OUT" "BACKUP_AGE does not apply on a release machine"
same "...before anything is copied" 0 "$(grep -c '^docker' "$FAKE_CALLS")"

title "a key is never printed"
machine shops; sed -i 's/^BACKUP_RECIPIENTS.*/BACKUP_RECIPIENTS="'"$KEY_A"' AGE-SECRET-KEY-1QQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQ"/' "$D/backup.env"
backup
fails "a private key in the list: refused" test "$CODE" = 0
has "...with a fixed message" "$OUT" "contains a PRIVATE age key"
lacks "...whose value is not in the output" "$OUT" "AGE-SECRET-KEY-1QQQ"
lacks "...nor in the calls" "$(cat "$FAKE_CALLS")" "AGE-SECRET-KEY"
same "...nothing started" 0 "$(grep -c '^docker' "$FAKE_CALLS")"
machine shops; sed -i "s/^BACKUP_RECIPIENTS.*/BACKUP_RECIPIENTS=\"$KEY_A TOPSECRETWORD\"/" "$D/backup.env"
backup
fails "a word that is not a key: refused" test "$CODE" = 0
has "...says which one" "$OUT" "key no. 2"
lacks "...without printing it" "$OUT" "TOPSECRETWORD"
machine shops; sed -i 's/^BACKUP_RECIPIENTS.*/BACKUP_RECIPIENTS="age1exampleofthefirstperson000000000000000000000000000000000 age1exampleofthesecondperson0000000000000000000000000000000"/' "$D/backup.env"
backup
fails "the example keys: age refuses them, and the run stops at once" test "$CODE" = 0
has "...says which" "$OUT" "age does not accept key no. 1"
lacks "...without the key" "$OUT" "exampleofthe"
same "...before any docker call" 0 "$(grep -c '^docker' "$FAKE_CALLS")"

title "one backup at a time, and what a failure leaves"
machine shops
exec 7> "$D/state/.backup.lock"; flock -n 7
backup
same "another backup holds the lock: it does nothing and says so" 0 "$CODE"
has "...says so" "$OUT" "another copy is already running"
same "...no docker call" 0 "$(grep -c '^docker' "$FAKE_CALLS")"
fails "...no backup.error, no alarm" bash -c "test -e $D/state/backup.error || grep -q '^check' $FAKE_CALLS"
run bash "$DEPLOY/backup.sh" "$D" --here-only
same "a hand run (--here-only) that finds the lock: exit 1, it did not make its copy" 1 "$CODE"; has "...and says so" "$OUT" "yours was not made"
exec 7>&-
backup
same "the lock let go: it runs" 0 "$CODE"
machine shops; mkdir -p "$D/backup/daily"; : > "$D/backup/daily/other.tmp"; : > "$D/backup/daily/old.tmp"; touch -d "2 days ago" "$D/backup/daily/old.tmp"
: > "$F/dump_empty"
backup
holds "a failed run removes its own .tmp, not another run's" test -e "$D/backup/daily/other.tmp"
holds "...a leftover of two days ago goes at the start of a run" test ! -e "$D/backup/daily/old.tmp"
machine shops; mkdir "$D/state/backup.error"; : > "$F/dump_empty"
backup
holds "backup.error cannot be written: the alarm goes all the same" grep -q "^check $D --test shops: backup failed" "$FAKE_CALLS"

title "postgres is not ready yet (a boot)"
machine shops; : > "$F/pg_isready_ko"
backup
fails "never ready: fails" test "$CODE" = 0
has "...says so" "$OUT" "for 5 minutes"
same "...no dump" 0 "$(grep -c 'pg_dump' "$FAKE_CALLS")"
same "...it looked 30 times" 30 "$(grep -c 'pg_isready' "$FAKE_CALLS")"
machine shops; echo 3 > "$F/pg_isready_attempts"
backup
same "ready after three tries: it goes" 0 "$CODE"
same "...it looked 4 times" 4 "$(grep -c 'pg_isready' "$FAKE_CALLS")"

title "the keys"
machine shops; sed -i '/^BACKUP_RECIPIENTS/d' "$D/backup.env"
backup
fails "no keys: fails" test "$CODE" = 0
has "...no copies in plaintext" "$OUT" "no plaintext copies"
same "...nothing made" 0 "$(grep -c '^docker' "$FAKE_CALLS")"
machine shops; sed -i "s/^BACKUP_RECIPIENTS.*/BACKUP_RECIPIENTS=$KEY_A/" "$D/backup.env"
backup
fails "one key only: fails" test "$CODE" = 0
has "...two people" "$OUT" "at least two different keys"
machine shops; sed -i "s/^BACKUP_RECIPIENTS.*/BACKUP_RECIPIENTS=\"$KEY_A $KEY_A\"/" "$D/backup.env"
backup
fails "the same key twice: fails" test "$CODE" = 0
machine shops; sed -i "s/^BACKUP_RECIPIENTS.*/BACKUP_RECIPIENTS=\"$KEY_A ssh-ed25519\"/" "$D/backup.env"
backup
fails "not an age key: fails" test "$CODE" = 0
has "...says which (the number, not the value)" "$OUT" "key no. 2 is not an age public key"
machine shops; sed -i '/^BACKUP_REMOTE/d' "$D/backup.env"
backup
fails "no destination: fails" test "$CODE" = 0
has "...says so" "$OUT" "BACKUP_REMOTE is missing"
holds "...the copy stays here" test -n "$(copies)"
holds "...and it is an error all the same" test -s "$D/state/backup.error"

title "the destination is another machine"
for bad in "/srv/backup" "copies@host" "-e ssh x:" "copies@host: other" "/x:y" "./a:b" "host:dir" "copies@host::module" "::" "rsync://host/module" "copies@host:a:b" "copies@host:\$(id)" "@host:x"; do
  machine shops; sed -i "s|^BACKUP_REMOTE=.*|BACKUP_REMOTE=$bad|" "$D/backup.env"
  backup
  fails "BACKUP_REMOTE=«$bad» is refused" test "$CODE" = 0
  same "...before anything is copied" 0 "$(grep -c '^docker' "$FAKE_CALLS")"
done
machine shops; backup_env "BACKUP_SSH_KNOWN_HOSTS=/a file/with spaces"
backup
fails "a known_hosts path with a space is refused" test "$CODE" = 0
for good in "copies@host:" "copies@192.168.1.5:dir/sub" "copies@[::1]:x" "backup-community@backup.example.net:"; do
  machine shops; sed -i "s|^BACKUP_REMOTE=.*|BACKUP_REMOTE=$good|" "$D/backup.env"
  backup
  same "BACKUP_REMOTE=«$good» is a destination" 0 "$CODE"
done

title "backup.env is read, not executed"
machine shops
backup_env 'BACKUP_X=$(touch '"$TMP"'/ran)' '$(touch '"$TMP"'/ran2)' 'PATH=/nonexistent' 'BACKUP_SSH_PORT=2222;touch '"$TMP"'/ran3'
backup
fails "no command ran" bash -c "ls $TMP/ran*"
fails "a port with a ; is refused" test "$CODE" = 0
machine shops; backup_env "BACKUP_SSH_KNOWN_HOSTS=/some/known_hosts"
backup
holds "BACKUP_SSH_KNOWN_HOSTS goes to ssh" grep -q 'UserKnownHostsFile=/some/known_hosts' "$FAKE_CALLS"

title "work folders left by a run that was killed"
machine shops
mkdir -p "$D/backup/.work.old" "$D/backup/.work.new_version"
touch -d "2 days ago" "$D/backup/.work.old"
backup
holds "the old one is gone" test ! -e "$D/backup/.work.old"
holds "a recent one (maybe a run in progress) stays" test -d "$D/backup/.work.new_version"

title "an installation from the sources (no release.env), as on a development machine"
machine bitmentor sources
backup_env BACKUP_REMOTE= 'BACKUP_AGE=age-in-container --test' BACKUP_TEST_FOLDER="$TMP/outside"
mkdir -p "$TMP/extra"
cat > "$TMP/extra/age-in-container" <<EOF
#!/usr/bin/env bash
echo "age-in-container \$*" >> "\$FAKE_CALLS"
exec "$FAKES/age" "\${@:2}"
EOF
chmod 755 "$TMP/extra/age-in-container"
sed -i '/^BACKUP_REMOTE=copies/d' "$D/backup.env"
backup
same "no release.env: it works" 0 "$CODE"
holds "the project is the folder's name" test -f "$D/backup/daily/bitmentor-"*.tar.age
holds "the docker containers are the project's" grep -q 'label=com.docker.compose.project=bitmentor' "$FAKE_CALLS"
holds "BACKUP_AGE: the command and its arguments" grep -q '^age-in-container --test -r ' "$FAKE_CALLS"
holds "the copy is in the folder of the test machine" test -f "$TMP/outside/$(basename "$(ls "$D"/backup/daily/bitmentor-*.tar.age)")"
fails "no rsync" grep -q '^rsync' "$FAKE_CALLS"
has "it says that it protects from nothing" "$OUT" "it does not protect from a failure"
holds "backup.ok" test -s "$D/state/backup.ok"
sleep 1; backup
same "again: it ends well, the copy that is there is not written again" 0 "$CODE"
same "...two copies" 2 "$(ls "$TMP"/outside | wc -l)"
machine bitmentor sources
backup_env BACKUP_PROJECT=other BACKUP_REMOTE=
sed -i '/^BACKUP_REMOTE=copies/d' "$D/backup.env"
backup_env BACKUP_TEST_FOLDER="$TMP/outside2"
backup
holds "BACKUP_PROJECT names the project" test -f "$D/backup/daily/other-"*.tar.age
machine bitmentor sources; backup_env 'BACKUP_PROJECT=Capitals and spaces'
backup
fails "a project name that is not a name is refused" test "$CODE" = 0
machine bitmentor sources
backup_env BACKUP_TEST_FOLDER="$TMP/outside3"
backup
fails "a folder and a remote together: refused" test "$CODE" = 0
has "...says so" "$OUT" "pick one"
machine shops
backup_env BACKUP_REMOTE= BACKUP_TEST_FOLDER="$TMP/outside4"
sed -i '/^BACKUP_REMOTE=copies/d' "$D/backup.env"
backup
fails "a release machine (release.env) refuses the local folder" test "$CODE" = 0
has "...says so" "$OUT" "does not apply on a release machine"
fails "...and made nothing" test -e "$TMP/outside4"
machine bitmentor sources
backup_env BACKUP_TEST_FOLDER=relative
sed -i '/^BACKUP_REMOTE=copies/d' "$D/backup.env"
backup
fails "a relative folder is refused" test "$CODE" = 0

title "backup.sh from the sources: the dumps are read back"
S=$TMP/sources; mkdir -p "$S" "$S/fakes"
cp "$BASE/backup.sh" "$S/backup.sh"
srun() { OUT=$(cd "$S" && PATH="$FAKES:$PATH" FAKE_DIR=$1 FAKE_CALLS=$S/calls.log sh ./backup.sh "${@:2}" 2>&1); CODE=$?; }
mkdir -p "$S/f0"; : > "$S/calls.log"
srun "$S/f0" "$S/copies" 14
same "all well: ends well" 0 "$CODE"
same "...one dump" 1 "$(ls "$S"/copies/*.dump | wc -l)"
same "...no .tmp" 0 "$(ls "$S"/copies/*.tmp 2>/dev/null | wc -l)"
holds "...private" test "$(stat -c %a "$S"/copies/bitmentor-*.dump)" = 600
has "...says what it read" "$OUT" "2 tables, read to the end"
touch -d "20 days ago" "$S"/copies/bitmentor-*.dump; mv "$S"/copies/bitmentor-*.dump "$S/copies/bitmentor-20200101T000000Z.dump"; touch -d "20 days ago" "$S/copies/bitmentor-20200101T000000Z.dump"
for scenario in dump_truncated dump_empty; do
  mkdir -p "$S/f1"; rm -f "$S"/f1/*; : > "$S/f1/$scenario"
  srun "$S/f1" "$S/copies" 14
  fails "$scenario: fails" test "$CODE" = 0
  has "$scenario: says which" "$OUT" "bitmentor does not open"
  holds "$scenario: the old dump is still here" test -f "$S/copies/bitmentor-20200101T000000Z.dump"
  same "$scenario: no new dump, no .tmp" 1 "$(ls "$S"/copies | wc -l)"
done
srun "$S/f0" "$S/copies" 14
same "again, all well" 0 "$CODE"
holds "...the 20 days old dump is gone (14 days kept)" test ! -e "$S/copies/bitmentor-20200101T000000Z.dump"
srun "$S/f0" "$S/copies" zero
same "days that are not a number: refused" 2 "$CODE"

title "keep-backups.sh: 30 days, the first of each month for 12 months, the latest three always"
KEEP_SCRIPT=$DEPLOY/examples/keep-backups.sh
K=$TMP/keep; mkdir -p "$K"; chmod 755 "$K"
name() { echo "$1-$(date -u -d "$2" +%Y%m%d)T000000Z.tar.age"; }
day() { date -u -d "$1" +%F; }
# fourteen months back: the 1st, the 15th and the 28th of each; plus every day of the last 40 days
first_month=$(date -u -d "$(date -u +%Y-%m-01)" +%F)
: > "$K/.list"
for m in $(seq 0 14); do
  start=$(date -u -d "$first_month - $m months" +%F)
  for d in 0 14 27; do
    day=$(date -u -d "$start + $d days" +%F)
    [[ $(date -u -d "$day" +%s) -le $(date -u +%s) ]] || continue
    : > "$K/$(name shops "$day")"; echo "shops $day" >> "$K/.list"
  done
done
for d in $(seq 0 40); do : > "$K/$(name shops "$d days ago")"; done
: > "$K/shops-99999999T033000Z.tar.age"; : > "$K/shops-20261305T033000Z.tar.age"
: > "$K/shops-20250101T033000Z.tar.age.part"; : > "$K/note.txt"
cp -r "$K" "$K.before"
run bash "$KEEP_SCRIPT" "$K" --test
same "--test: ends well" 0 "$CODE"
holds "--test: deletes nothing" diff -r "$K" "$K.before"
has "--test: says what it would delete" "$OUT" "would delete "
run bash "$KEEP_SCRIPT" "$K"
same "a first real run: ends well" 0 "$CODE"
today=$(date -u +%s)
kept() { [ -e "$K/$1" ]; }
age_days() { echo $(( (today - $(date -u -d "$1" +%s)) / 86400 )); }
wrong=0
while read -r project day; do
  file=$(name "$project" "$day"); age=$(age_days "$day")
  first_of_month=$(ls "$K.before" | grep "^$project-${day:0:4}${day:5:2}" | head -n 1)
  if [ "$age" -lt 30 ] || { [ "$file" = "$first_of_month" ] && [ "$age" -lt 372 ]; }; then
    kept "$file" || { wrong=$((wrong + 1)); echo "  NO: $file ($age days) should be kept"; }
  else
    kept "$file" && { wrong=$((wrong + 1)); echo "  NO: $file ($age days) should be deleted"; }
  fi
done < "$K/.list"
if [ "$wrong" -eq 0 ]; then yes; else FAILED=$((FAILED + 1)); fi
ok=1; for d in $(seq 0 29); do kept "$(name shops "$d days ago")" || { ok=0; no "the copy of $d days ago must stay"; break; }; done; [ "$ok" = 1 ] && yes
holds "a name whose date is not a date is left alone" test -e "$K/shops-99999999T033000Z.tar.age" -a -e "$K/shops-20261305T033000Z.tar.age"
holds "files that are not copies are left alone" test -e "$K/note.txt" -a -e "$K/shops-20250101T033000Z.tar.age.part"
run bash "$KEEP_SCRIPT" "$K"; same "again: nothing more to delete" "" "$OUT"
holds "a name with a letter in its date is left alone" bash -c "touch $K/shops-2025AB01T033000Z.tar.age; bash $KEEP_SCRIPT $K >/dev/null; test -e $K/shops-2025AB01T033000Z.tar.age"
run bash "$KEEP_SCRIPT" "$K" --dry-run; same "an option that is not --test: refused, nothing deleted" 2 "$CODE"
DAYS=08 run bash "$KEEP_SCRIPT" "$K"; same "DAYS with a leading zero: refused" 2 "$CODE"
DAYS=zero run bash "$KEEP_SCRIPT" "$K"; same "DAYS that is not a number: refused" 2 "$CODE"
run bash "$KEEP_SCRIPT" "$K/non-exists"; same "a folder that does not exist: refused" 2 "$CODE"
mkdir -p "$TMP/empty"; chmod 755 "$TMP/empty"; run bash "$KEEP_SCRIPT" "$TMP/empty"; same "an empty folder: nothing, no error" 0 "$CODE"
chmod 777 "$K"; run bash "$KEEP_SCRIPT" "$K"; same "a folder that others can write: refused" 2 "$CODE"; chmod 755 "$K"
# What a wrong clock or invented files could make look old
fresh() { rm -rf "$TMP/c2" "$TMP/c2.before"; mkdir -p "$TMP/c2"; chmod 755 "$TMP/c2"; }
fresh; for d in 400 380 100 90; do : > "$TMP/c2/$(name p "$d days ago")"; done
cp -r "$TMP/c2" "$TMP/c2.before"
run bash "$KEEP_SCRIPT" "$TMP/c2"
same "the latest copy is 90 days old (copies stopped, or the clock is ahead): nothing touched, exit 1" 1 "$CODE"
holds "...nothing deleted" diff -r "$TMP/c2" "$TMP/c2.before"
has "...says so" "$OUT" "WARNING p: the latest copy is 90 days old"
fresh; for d in 3 2 1; do : > "$TMP/c2/$(name q "$d days ago")"; done; : > "$TMP/c2/q-$(date -u -d 'tomorrow + 1 day' +%Y%m%d)T033000Z.tar.age"
for d in 400 200 100; do : > "$TMP/c2/$(name q "$d days ago")"; done
cp -r "$TMP/c2" "$TMP/c2.before"
run bash "$KEEP_SCRIPT" "$TMP/c2"
same "the latest copy is dated in the future: nothing touched, exit 1" 1 "$CODE"
holds "...nothing deleted" diff -r "$TMP/c2" "$TMP/c2.before"
has "...says so" "$OUT" "has a date in the future"
fresh; for d in 3 2; do : > "$TMP/c2/$(name ok "$d days ago")"; done; : > "$TMP/c2/$(name other "400 days ago")"; : > "$TMP/c2/$(name other "399 days ago")"; : > "$TMP/c2/$(name other "398 days ago")"
run bash "$KEEP_SCRIPT" "$TMP/c2"
holds "a project left alone does not stop the others: the 3-day-old project is untouched, the other is refused" test -e "$TMP/c2/$(name other "400 days ago")" -a -e "$TMP/c2/$(name ok "3 days ago")"
same "...exit 1" 1 "$CODE"
fresh; for d in 5 4 3; do : > "$TMP/c2/$(name r "$d days ago")"; done; : > "$TMP/c2/$(name r "400 days ago")"
run bash "$KEEP_SCRIPT" "$TMP/c2"; same "the latest copy is 3 days old (STALE_DAYS): nothing touched, exit 1" 1 "$CODE"
holds "...the old one is still there" test -e "$TMP/c2/$(name r "400 days ago")"
has "...it says how to retire a project" "$OUT" ".retired-r"
touch "$TMP/c2/.retired-r"; run bash "$KEEP_SCRIPT" "$TMP/c2"
same "a project retired on purpose (.retired-r): old copies age out, exit 0" 0 "$CODE"
fails "...the 400 days old copy went" test -e "$TMP/c2/$(name r "400 days ago")"
holds "...the latest three stayed" test -e "$TMP/c2/$(name r "5 days ago")" -a -e "$TMP/c2/$(name r "4 days ago")" -a -e "$TMP/c2/$(name r "3 days ago")"
fresh; for d in 2 1 0; do : > "$TMP/c2/$(name s "$d days ago")"; done
run bash "$KEEP_SCRIPT" "$TMP/c2"; same "a latest copy of 2 days: all well" 0 "$CODE"
STALE_DAYS=1 run bash "$KEEP_SCRIPT" "$TMP/c2"; same "...today's is under STALE_DAYS=1" 0 "$CODE"
STALE_DAYS=0 run bash "$KEEP_SCRIPT" "$TMP/c2"; same "STALE_DAYS that is not a number of days: refused" 2 "$CODE"
# the last three are kept, whatever their age; the cap is half of the copies
fresh; old_month=$(date -u -d "$(date -u +%Y-%m-01) - 5 months" +%Y-%m)
for d in 02 03 04 05; do : > "$TMP/c2/z-${old_month//-/}${d}T033000Z.tar.age"; done; : > "$TMP/c2/$(name z "today")"
run bash "$KEEP_SCRIPT" "$TMP/c2"
holds "the last three copies are kept (here: the 4th and 5th of an old month, and today's)" test -e "$TMP/c2/z-${old_month//-/}04T033000Z.tar.age" -a -e "$TMP/c2/z-${old_month//-/}05T033000Z.tar.age"
holds "...the first of that month too" test -e "$TMP/c2/z-${old_month//-/}02T033000Z.tar.age"
fails "...and the 3rd, which nothing keeps, goes" test -e "$TMP/c2/z-${old_month//-/}03T033000Z.tar.age"
fresh; for d in 01 02 03 04 05 06 07 08 09 10 11 12 13 14 15 16 17 18 19 20 21; do : > "$TMP/c2/w-${old_month//-/}${d}T033000Z.tar.age"; done; : > "$TMP/c2/$(name w "today")"
run bash "$KEEP_SCRIPT" "$TMP/c2"
same "22 copies, 18 deletable: one run deletes 11 (half), oldest first" 11 "$(ls "$TMP/c2" | wc -l)"
holds "...the oldest of them went, the 12th stays" test ! -e "$TMP/c2/w-${old_month//-/}12T033000Z.tar.age" -a -e "$TMP/c2/w-${old_month//-/}13T033000Z.tar.age"
for time in 1 2 3 4 5 6; do run bash "$KEEP_SCRIPT" "$TMP/c2"; done
same "...and the runs after it finish the job, leaving the 1st of the month and the last three" 4 "$(ls "$TMP/c2" | wc -l)"

title "archive-copies.sh: the drop box is not the archive"
ARCHIVE_SCRIPT=$DEPLOY/examples/archive-copies.sh
I=$TMP/inbox A=$TMP/archive
mkdir -p "$I" "$A"; chmod 755 "$A"
st() { date -u -d "$1" +%Y%m%dT%H%M%SZ; }  # the stamp of a moment, as in a copy's name
copy() { head -c 5000 /dev/zero | tr '\0' 'x' | { printf 'age-encryption.org/v1\n'; cat; } > "$I/$1"; }
archived() { find "$A" -maxdepth 1 -type f -name '*.tar.age' | wc -l; }
D1=shops-$(st "3 days ago 03:30:00").tar.age D2=shops-$(st "2 days ago 03:30:00").tar.age
copy "$D1"; copy "$D2"
run bash "$ARCHIVE_SCRIPT" "$I" "$A"
same "first run: ends well" 0 "$CODE"
same "two copies archived" 2 "$(archived)"
has "...with their hash" "$OUT" "sha256 $(sha256sum "$A/$D1" | cut -d' ' -f1)"
holds "...mode 600" test "$(stat -c %a "$A/$D1")" = 600
holds "...the drop box keeps an empty file with the name of each (rsync --ignore-existing needs no more)" bash -c "test -e $I/$D1 -a ! -s $I/$D1 -a -e $I/$D2 -a ! -s $I/$D2"
same "...archive/.last-run says how it went" "ok archived=2 refused=0" "$(cut -d' ' -f2- "$A/.last-run")"
fails "...no half-written file in the archive" bash -c "ls -A $A | grep '^\.' | grep -v -e '^.archive.lock\$' -e '^.last-run\$'"
run bash "$ARCHIVE_SCRIPT" "$I" "$A"; same "again: nothing to do, ends well" 0 "$CODE"; same "...and says nothing" "" "$OUT"
# The site's machine key can delete and overwrite what is in the drop box
ARCHIVED_HASH=$(sha256sum "$A/$D2" | cut -d' ' -f1)
rm -f "$I"/*
copy "$D2"; printf 'y' >> "$I/$D2"
run bash "$ARCHIVE_SCRIPT" "$I" "$A"
same "a different file with an archived name: exit 1" 1 "$CODE"
has "...says DIFFERENT" "$OUT" "DIFFERENT $D2"
same "...the archived one is as it was" "$ARCHIVED_HASH" "$(sha256sum "$A/$D2" | cut -d' ' -f1)"
holds "...and the other one leaves the drop box, into refused (reported once)" bash -c "test ! -e $I/$D2 && ls $TMP/refused | grep -q '^$D2\\.'"
same "...archive/.last-run says there was a problem" "problems archived=0 refused=1" "$(cut -d' ' -f2- "$A/.last-run")"
run bash "$ARCHIVE_SCRIPT" "$I" "$A"; same "...and the next run has nothing to report" 0 "$CODE"
rm -f "$I"/*
# What the key can invent: names, dates, sizes, counts
echo "contents-different" > "$I/shops-$(st '1 day ago 03:30:00').tar.age"
copy "shops-$(st '1 day ago 04:30:00').tar.age"; ln -s /etc/passwd "$I/shops-$(st '1 day ago 05:30:00').tar.age"
copy "shops-$(st 'tomorrow + 2 days').tar.age"
head -c 20 /dev/zero | { printf 'age-encryption.org/v1\n'; cat; } > "$I/shops-$(st '1 day ago 06:30:00').tar.age"
copy "shops-$(st '40 days ago 03:30:00').tar.age"; copy "shops-20A60101T033000Z.tar.age"; copy "Shops-$(st '1 day ago 07:30:00').tar.age"
copy "shops-$(st '1 day ago 08:30:00').tar.age.extra"
copy "$(printf 'shops\narchived fake: 1 bytes, sha256 0-%s.tar.age' "$(st '1 day ago 09:30:00')")"
run bash "$ARCHIVE_SCRIPT" "$I" "$A"
same "refusals: exit 1" 1 "$CODE"
has "...not an age file" "$OUT" "REFUSED shops-$(st '1 day ago 03:30:00').tar.age: not an age file"
has "...too small" "$OUT" "REFUSED shops-$(st '1 day ago 06:30:00').tar.age: not an age file of a plausible size (42 bytes)"
has "...from the future" "$OUT" "the date in the name is not a date, is in the future or is older than 18 days"
has "...a date of 40 days ago" "$OUT" "REFUSED shops-$(st '40 days ago 03:30:00').tar.age: the date"
has "...a date that has a letter in it" "$OUT" "REFUSED shops-20A60101T033000Z.tar.age: the name is not"
has "...a name with a capital" "$OUT" "REFUSED Shops-"
holds "...the good one went in" test -f "$A/shops-$(st '1 day ago 04:30:00').tar.age"
fails "...the link did not" test -e "$A/shops-$(st '1 day ago 05:30:00').tar.age"
same "...three in the archive, no more" 3 "$(archived)"
same "...everything refused left the drop box: what stays is the empty file of the good one, the link and a file with another name" 3 "$(ls -A "$I" | wc -l)"
holds "...what was invented (not a name, 18 bytes, 42 bytes) was deleted, not kept" bash -c "! ls $TMP/refused | grep -q -e fake -e Shops -e 'shops-$(st '1 day ago 03:30:00')' -e 'shops-$(st '1 day ago 06:30:00')'"
holds "...what has the shape of a copy (here: dated 40 days ago) is in refused, under a safe name" bash -c "ls $TMP/refused | grep -q '^shops-$(st '40 days ago 03:30:00' | cut -c1-8)' && ! ls $TMP/refused | grep -q '[^A-Za-z0-9._-]'"
lacks "...a newline in a name cannot forge a line of the output" "$OUT" $'\narchived fake'
has "...it is printed escaped" "$OUT" 'REFUSED $'"'"'shops\narchived fake'
fails "...and it is not archived" bash -c "ls -A $A | grep -q fake"
rm -f "$I"/*
# three a day at most, and the disk
for hour in 10 11 12 13 14 15; do copy "other-$(st "1 day ago $hour:00:00").tar.age"; done
run bash "$ARCHIVE_SCRIPT" "$I" "$A"
same "six copies of one project in one day: exit 1" 1 "$CODE"
same "...five archived, the sixth refused" 5 "$(find "$A" -name 'other-*' | wc -l)"
has "...says why" "$OUT" "at most 5"
rm -f "$I"/*; copy "third-$(st '1 day ago 03:30:00').tar.age"
run env MIN_FREE=999999999999999 bash "$ARCHIVE_SCRIPT" "$I" "$A"
same "no room on the disk: exit 1" 1 "$CODE"; has "...says so" "$OUT" "little space in archive"
fails "...nothing copied" test -e "$A/third-$(st '1 day ago 03:30:00').tar.age"
run bash "$ARCHIVE_SCRIPT" "$I" "$A"; same "with room: it goes in" 0 "$CODE"
rm -f "$I"/*
# an old copy that is archived is not a problem if the machine sends it again
cp "$A/$D1" "$I/$D1"; touch -d "30 days ago" "$A/$D1" "$I/$D1"
run bash "$ARCHIVE_SCRIPT" "$I" "$A"; same "an archived copy sent again, whatever its date: nothing to say" "" "$OUT"
holds "...and it leaves the drop box, being old and archived" test ! -e "$I/$D1"
chmod 777 "$A"; run bash "$ARCHIVE_SCRIPT" "$I" "$A"; same "an archive that others can write: refused" 2 "$CODE"; chmod 755 "$A"
run env MODE=7 bash "$ARCHIVE_SCRIPT" "$I" "$A"; same "MODE that is not an octal mode: refused" 2 "$CODE"
run env PER_DAY=08 bash "$ARCHIVE_SCRIPT" "$I" "$A"; same "a number with a leading zero: refused" 2 "$CODE"
copy "newone-$(st '1 day ago 03:30:00').tar.age"; run env MODE=640 GROUP="$(id -gn)" bash "$ARCHIVE_SCRIPT" "$I" "$A"
same "MODE=640 GROUP=<group>: the copy takes them" "640 $(id -gn)" "$(stat -c '%a %G' "$A/newone-$(st '1 day ago 03:30:00').tar.age")"
run bash "$ARCHIVE_SCRIPT" "$I" "$I"; same "the same folder twice: refused" 2 "$CODE"
rm -f "$I"/* "$I"/.[a-z]*
copy shops-$(st "9 days ago 03:30:00").tar.age; copy shops-$(st "5 days ago 03:30:00").tar.age; copy shops-$(st "1 day ago 03:30:00").tar.age
touch -d "20 days ago" "$I/shops-$(st '9 days ago 03:30:00').tar.age"; touch -d "5 days ago" "$I/shops-$(st '5 days ago 03:30:00').tar.age"
printf 'x' > "$I/.shops-old.tar.age.abc123"; touch -d "3 days ago" "$I/.shops-old.tar.age.abc123"
echo recent > "$I/.shops-new.tar.age.abc123"
run bash "$ARCHIVE_SCRIPT" "$I" "$A"
holds "drop box: an old copy that is archived goes after 10 days" test ! -e "$I/shops-$(st '9 days ago 03:30:00').tar.age"
holds "...a recent one stays" test -e "$I/shops-$(st '5 days ago 03:30:00').tar.age"
holds "...and was archived" test -f "$A/shops-$(st '5 days ago 03:30:00').tar.age"
holds "...a half-written file older than two days goes, a recent one stays" bash -c "test ! -e $I/.shops-old.tar.age.abc123 && test -e $I/.shops-new.tar.age.abc123"
# a comparison that does not finish is not a difference
rm -f "$I"/*; cp "$A/$D1" "$I/$D1"; : > "$F/timeout_ko"
run bash "$ARCHIVE_SCRIPT" "$I" "$A"
same "a comparison that times out: exit 1" 1 "$CODE"; has "...says it could not compare" "$OUT" "NOTCOMPARABLE $D1"
fails "...and it is not called a difference" bash -c "echo '$OUT' | grep -q DIFFERENT"
holds "...the file stays, to be compared again" test -s "$I/$D1"
rm -f "$F/timeout_ko"; run bash "$ARCHIVE_SCRIPT" "$I" "$A"; same "...which works when it can" 0 "$CODE"
# refused: old things go, and no more than the cap
rm -rf "$TMP/refused"; mkdir -m 700 "$TMP/refused"; : > "$TMP/refused/old"; touch -d "40 days ago" "$TMP/refused/old"
for n in 1 2 3 4 5; do head -c 400 /dev/zero > "$TMP/refused/f$n"; touch -d "$n days ago" "$TMP/refused/f$n"; done
run env REFUSED_MAX_BYTES=900 bash "$ARCHIVE_SCRIPT" "$I" "$A"
holds "refused: what is older than 30 days goes" test ! -e "$TMP/refused/old"
holds "...over the cap the oldest go first, but never the newest three (cap 900, five files of 400: the two oldest go)" bash -c "test ! -e $TMP/refused/f5 -a ! -e $TMP/refused/f4 -a -e $TMP/refused/f3 -a -e $TMP/refused/f2 -a -e $TMP/refused/f1"
# refused that is a link: the files stay where they are
rm -rf "$TMP/refused"; mkdir -p "$TMP/elsewhere"; ln -s "$TMP/elsewhere" "$TMP/refused"
copy "shops-$(st '1 day ago 03:00:00').tar.age"; cp "$A/$D1" "$TMP/equal"; cat "$TMP/equal" > /dev/null; printf 'y' >> "$I/shops-$(st '1 day ago 03:00:00').tar.age"
cp "$I/shops-$(st '1 day ago 03:00:00').tar.age" "$TMP/expected"; copy "different-$(st '40 days ago 03:00:00').tar.age"
run bash "$ARCHIVE_SCRIPT" "$I" "$A"
has "a refused that is a link is not used" "$OUT" "refused is not right"
holds "...the refused file stays in the drop box, and nothing went through the link" bash -c "test -e '$I/shops-$(st '1 day ago 03:00:00').tar.age' && test -z \"\$(ls -A $TMP/elsewhere)\""
holds "...nor a valid-shaped refused copy deleted for want of a place" test -e "$I/different-$(st '40 days ago 03:00:00').tar.age"
rm -rf "$TMP/refused" "$TMP/elsewhere"; rm -f "$I"/shops-$(st '1 day ago 03:00:00').tar.age "$I"/different-*
# what a key can do with the names it chooses: a link to a FIFO, and a FIFO, where the placeholder used to be made
rm -rf "$I" "$TMP/refused"; mkdir -p "$I"; chmod 755 "$I"
copy "$D1"; mkfifo -m 600 "$TMP/fifo-target"; touch -d "2001-01-01" "$TMP/fifo-target"
ln -s "$TMP/fifo-target" "$I/.$D1.empty"        # the name of the old placeholder, a link to a FIFO
mkfifo -m 600 "$I/.$D2.empty"                       # and a FIFO with the other
cp "$A/$D1" "$I/$D1"; cp "$A/$D2" "$I/$D2"
timeout 30 env PATH="$FAKES:$PATH" FAKE_DIR=$F bash "$ARCHIVE_SCRIPT" "$I" "$A" > /dev/null 2>&1; CODE=$?
same "a link to a FIFO and a FIFO with the placeholder's old name: the run ends, it does not hang" 0 "$CODE"
same "...the FIFO the link points to keeps its mode" 600 "$(stat -c %a "$TMP/fifo-target")"
same "...and its date" 2001-01-01 "$(date -u -d "@$(stat -c %Y "$TMP/fifo-target")" +%F)"
holds "...the copies became empty files all the same" test ! -s "$I/$D1" -a ! -s "$I/$D2" -a -f "$I/$D1" -a -f "$I/$D2"
rm -rf "$I"; mkdir -p "$I"; chmod 755 "$I"
# a name of 241 characters cannot make .NAME.new, and must not stop the names after it
long=$(printf 'a%.0s' $(seq 1 222))-$(st '1 day ago 03:00:00').tar.age
copy "$long"; copy "zzz-$(st '1 day ago 03:10:00').tar.age"
run bash "$ARCHIVE_SCRIPT" "$I" "$A"
same "a name over 240 characters: refused, the run goes on" 1 "$CODE"; has "...says why" "$OUT" "too long"
holds "...and the name after it was archived" test -f "$A/zzz-$(st '1 day ago 03:10:00').tar.age"
rm -rf "$I"; mkdir -p "$I"; chmod 755 "$I"
# an empty file with a valid name: refused if nothing is archived under it (and the name is free again); a placeholder if it is
: > "$I/empty-$(st '1 day ago 03:00:00').tar.age"
run bash "$ARCHIVE_SCRIPT" "$I" "$A"
same "an empty file with a valid name and no archived copy: refused" 1 "$CODE"; has "...as too small" "$OUT" "(0 bytes)"
holds "...and it left the drop box, so the real copy can come" test ! -e "$I/empty-$(st '1 day ago 03:00:00').tar.age"
: > "$I/$D1"; run bash "$ARCHIVE_SCRIPT" "$I" "$A"; same "an empty file with an archived name: the placeholder of that copy, silent" "" "$OUT"
# the end of a run that died is not "ok"
echo 1 > "$F/df_ko"; copy "died-$(st '1 day ago 04:00:00').tar.age"
run bash "$ARCHIVE_SCRIPT" "$I" "$A"; rm "$F/df_ko"
holds "a run that stops on an error is not ok in .last-run" grep -q ' problems ' "$A/.last-run"
rm -f "$I/died-"*
same ".last-run is private" 600 "$(stat -c %a "$A/.last-run")"
# big refused files are deleted; the report has a limit; old things in refused go, directories too
rm -rf "$I" "$TMP/refused"; mkdir -p "$I"; chmod 755 "$I"
# a real copy, big, refused for its date (after a long interruption): never deleted, and it goes into the archive by hand
big="shops-$(st '25 days ago 03:30:00').tar.age"
{ printf 'age-encryption.org/v1\n'; head -c 3000000 /dev/zero | tr '\0' 'x'; } > "$I/$big"; ORIGINAL=$(sha256sum "$I/$big" | cut -d' ' -f1)
run bash "$ARCHIVE_SCRIPT" "$I" "$A"
same "a real copy of 3 MB, dated 25 days ago: refused" 1 "$CODE"; has "...for its date" "$OUT" "older than 18 days"
holds "...it left the drop box and is in refused, all of it" bash -c "test ! -e '$I/$big' && test \$(stat -c %s $TMP/refused/$big.*) -gt 3000000"
f=$(ls "$TMP/refused" | grep "^$big" | head -n 1); install -m 600 "$TMP/refused/$f" "$A/${f%.*.*}"
same "...and put into the archive by hand, as the guide says, it is the same file" "$ORIGINAL" "$(sha256sum "$A/$big" | cut -d' ' -f1)"
# the cap of refused holds for what one run moves: the second copy that would not fit stays in the drop box
rm -rf "$TMP/refused"; for n in 1 2; do { printf 'age-encryption.org/v1\n'; head -c 3000 /dev/zero | tr '\0' 'x'; } > "$I/shops-$(st "$((30 + n)) days ago 03:30:00").tar.age"; done
run env REFUSED_MAX_BYTES=5000 bash "$ARCHIVE_SCRIPT" "$I" "$A"
same "two refused copies of 3000 bytes with a cap of 5000: one moved, one left" 1 "$(ls "$I" | grep -c '^shops-')"; same "...in refused: one" 1 "$(ls "$TMP/refused" | wc -l)"
has "...and it says that refused is full" "$OUT" "REFUSED_FULL"
rm -rf "$TMP/refused"; rm -f "$I"/shops-*
for n in $(seq 1 25); do : > "$I/many-$(st "$n minutes ago").tar.age"; done
run bash "$ARCHIVE_SCRIPT" "$I" "$A"
same "25 refusals: 20 lines and one that counts the others" 21 "$(echo "$OUT" | grep -c .)"; has "...the last says how many" "$OUT" "and 5 more refusals"
mkdir -p "$TMP/refused/folder-old"; touch -d "40 days ago" "$TMP/refused/folder-old"
run bash "$ARCHIVE_SCRIPT" "$I" "$A"
holds "an old directory in refused goes too" test ! -e "$TMP/refused/folder-old"
rm -rf "$TMP/refused" "$I"; mkdir -p "$I"; chmod 755 "$I"
# one run at a time
exec 8> "$A/.archive.lock"; flock -n 8
run bash "$ARCHIVE_SCRIPT" "$I" "$A"; same "another run in progress: it says so and leaves" "another run is still going" "$OUT"
exec 8>&-

title "verify-copy.sh: whoever holds the key opens the latest copy"
VERIFY_SCRIPT=$DEPLOY/examples/verify-copy.sh
printf 'right' > "$TMP/key.right"; printf 'wrong' > "$TMP/key.wrong"
mkdir -p "$TMP/tmpv"
vrun() { OUT=$(PATH="$FAKES:$PATH" TMPDIR=$TMP/tmpv AGE="$FAKES/age" PG_RESTORE="$FAKES/pg_restore" FAKE_DIR=$F FAKE_CALLS=$M/calls.log bash "$VERIFY_SCRIPT" "$@" 2>&1); CODE=$?; }
machine shops; backup
V=$TMP/keeper; mkdir -p "$V"; cp "$(last_copy)" "$V/"
vrun --key "$TMP/key.right" "$V"
same "a good copy: ends well" 0 "$CODE"
has "...opened" "$OUT" "decrypted and opened"
has "...hashes" "$OUT" "all with their hash"
has "...every dump read to the end" "$OUT" "bitmentor.dump: 2 tables, read in full"
has "...All good" "$OUT" "All good"
lacks "...no secret printed" "$OUT" "not-a-secret"
lacks "...no .env content" "$OUT" "POSTGRES_PASSWORD"
same "...no temporary folder left (it held the .env)" "" "$(ls -A "$TMP/tmpv")"
vrun --key "$TMP/key.right" "$V/$(basename "$(last_copy)")"
same "the file instead of the folder: the same" 0 "$CODE"
vrun "$V"
same "without a key: header, size and age only" 0 "$CODE"
has "...says that it did not open it" "$OUT" "without --key it is not opened"
vrun --quiet --key "$TMP/key.right" "$V"
same "--quiet: a good copy prints nothing" "" "$OUT"; same "...and ends well" 0 "$CODE"
vrun --quiet "$V"; same "--quiet without a key: nothing either" "" "$OUT"
echo 1000 > "$F/df"; vrun --key "$TMP/key.right" "$V"
same "no room in TMPDIR: a problem" 1 "$CODE"; has "...says so" "$OUT" "little space in $TMP/tmpv"
rm "$F/df"
vrun --quiet --key "$TMP/key.wrong" "$V"
same "--quiet with a problem: it speaks" 1 "$CODE"; has "...and says which" "$OUT" "does not open with this key"
vrun --key "$TMP/key.wrong" "$V"
same "the wrong key: a problem" 1 "$CODE"
has "...says why" "$OUT" "does not open with this key"
old="$V/shops-$(date -u -d '3 days ago' +%Y%m%d)T033000Z.tar.age"; cp "$(last_copy)" "$old"
vrun --key "$TMP/key.right" "$V"
same "a folder: the latest copy is the one checked" 0 "$CODE"
rm "$V/$(basename "$(last_copy)")"
vrun --key "$TMP/key.right" "$V"
same "the latest is 3 days old: a problem" 1 "$CODE"
has "...says that copies stopped" "$OUT" "have copies stopped arriving"
vrun --max-age 0 --key "$TMP/key.right" "$V"
same "--max-age 0: no age check" 0 "$CODE"
vrun --max-age 100 --key "$TMP/key.right" "$V"
same "--max-age 100: fine" 0 "$CODE"
rm -rf "$V"; mkdir -p "$V"; cp "$(last_copy)" "$V/shops-$(date -u -d 'tomorrow + 3 days' +%Y%m%dT033000Z).tar.age"
vrun --key "$TMP/key.right" "$V"; same "a copy dated in the future: a problem" 1 "$CODE"; has "...says so" "$OUT" "in the future"
rm -rf "$V"; mkdir -p "$V"; cp "$(last_copy)" "$V/a b-$(date -u +%Y%m%dT%H%M%SZ).tar.age"; cp "$(last_copy)" "$V/*-$(date -u +%Y%m%dT%H%M%SZ).tar.age"
vrun --key "$TMP/key.right" "$V"; same "names with a space or a star: not copies of ours, a folder with only those has none" 1 "$CODE"
has "...says there is none" "$OUT" "there is no copy"
rm -rf "$V"; mkdir -p "$V"; head -c 3000 /dev/zero > "$V/shops-20260101T033000Z.tar.age"
vrun --max-age 0 "$V"; same "not an age file: a problem" 1 "$CODE"
rm -rf "$V"; mkdir -p "$V"
vrun "$V"; same "an empty folder: a problem" 1 "$CODE"
vrun --nonsense "$V"; same "an unknown option: wrong use" 2 "$CODE"
# a damaged copy: made by hand, with the fake age
damaged() {  # damaged <name> <what to do in the opened copy>
  rm -rf "$V" "$TMP/copy"; mkdir -p "$V"; open_copy "$(last_copy)" "$TMP/copy"
  (cd "$TMP/copy" && eval "$2")
  { printf 'age-encryption.org/v1\n-> fake\n---\n'; tar -C "$TMP/copy" -cf - .; } > "$V/shops-$(date -u +%Y%m%dT%H%M%SZ).tar.age"
  vrun --key "$TMP/key.right" "$V"
  same "$1: a problem" 1 "$CODE"
}
backup >/dev/null
damaged "a changed file" "echo x >> database/bitmentor.dump"; has "...the hash" "$OUT" "hashes that do not match"
damaged "a file that SHA256SUMS does not list" "echo extra > database/extra.txt"; has "...says so" "$OUT" "one more or one less"
damaged "a missing .env" "rm folder/.env; sed -i '/folder\/.env/d' SHA256SUMS"; has "...says which" "$OUT" "folder/.env is missing"
damaged "a dump cut near its end" "sed -i '/^END\$/d' database/bitmentor.dump; (sha256sum database/bitmentor.dump; grep -v 'database/bitmentor.dump' SHA256SUMS) > SHA256SUMS.n; sed -i 's|  database|  ./database|' SHA256SUMS.n; mv SHA256SUMS.n SHA256SUMS"
has "...says which" "$OUT" "bitmentor.dump is truncated or damaged"
damaged "an archive that is not an archive" "echo x > folder/state.tar; sed -i '/state.tar/d' SHA256SUMS"; has "...says which" "$OUT" "state.tar"
damaged "a dump with no tables" "printf 'PGDMP\nEND\n' > database/bitmentor.dump; sed -i '/bitmentor.dump/d' SHA256SUMS"; has "...says which" "$OUT" "bitmentor.dump has no tables"

if command -v age >/dev/null && command -v age-keygen >/dev/null; then
  title "with the real age"
  age-keygen -o "$TMP/anna.key" 2>/dev/null; age-keygen -o "$TMP/bruno.key" 2>/dev/null
  machine shops
  printf 'BACKUP_RECIPIENTS="%s %s"\n' "$(age-keygen -y "$TMP/anna.key")" "$(age-keygen -y "$TMP/bruno.key")" >> "$D/backup.env"
  # The fakes of docker, pg_restore and rsync, with the real age instead of the fake one
  mkdir -p "$TMP/real"; printf '#!/usr/bin/env bash\nexec %s "$@"\n' "$(command -v age)" > "$TMP/real/age"; chmod 755 "$TMP/real/age"
  cp "$FAKES/docker" "$FAKES/pg_restore" "$FAKES/rsync" "$FAKES/df" "$TMP/real/"
  OUT=$(PATH="$TMP/real:$PATH" bash "$DEPLOY/backup.sh" "$D" 2>&1); CODE=$?
  same "a copy made with the real age: ends well" 0 "$CODE"
  holds "...the header of the real age" test "$(head -c 21 "$(last_copy)")" = age-encryption.org/v1
  for key in anna bruno; do
    holds "...$key opens it" bash -c "age -d -i $TMP/$key.key < $(last_copy) | tar -tf - | grep -q 'database/bitmentor.dump'"
  done
  age-keygen -o "$TMP/foreign.key" 2>/dev/null
  fails "...a third key does not" bash -c "age -d -i $TMP/foreign.key < $(last_copy) >/dev/null 2>&1"
  rm -rf "$V"; mkdir -p "$V"; cp "$(last_copy)" "$V/"
  OUT=$(PATH="$TMP/real:$PATH" AGE=age PG_RESTORE="$FAKES/pg_restore" bash "$VERIFY_SCRIPT" --key "$TMP/bruno.key" "$V" 2>&1); CODE=$?
  same "verify-copy.sh opens it with the second key" 0 "$CODE"
fi

title "the units and the examples"
U=$DEPLOY/systemd
holds "the service runs the script of the release" grep -qx 'ExecStart=/opt/%i/current/deploy/backup.sh /opt/%i' "$U/bitmentor-backup@.service"
holds "...a oneshot with a time limit" grep -qx 'TimeoutStartSec=3h' "$U/bitmentor-backup@.service"
holds "...tried again in 10 minutes if it fails" grep -qx 'Restart=on-failure' "$U/bitmentor-backup@.service"
holds "...every 10 minutes" grep -qx 'RestartSec=10min' "$U/bitmentor-backup@.service"
holds "...three retries in an hour: the limit counts the first start too" grep -qx 'StartLimitBurst=4' "$U/bitmentor-backup@.service"
holds "...in the hour of the limit" grep -qx 'StartLimitIntervalSec=1h' "$U/bitmentor-backup@.service"
holds "the timer is nightly, with Persistent" grep -qx 'Persistent=true' "$U/bitmentor-backup@.timer"
for script in backup.sh examples/keep-backups.sh examples/archive-copies.sh examples/verify-copy.sh; do
  holds "$script is executable" test -x "$DEPLOY/$script"
done
holds "backup.sh at the root is executable" test -x "$BASE/backup.sh"
for variable in BACKUP_RECIPIENTS BACKUP_REMOTE BACKUP_SSH_KEY BACKUP_SSH_KNOWN_HOSTS BACKUP_SSH_PORT BACKUP_LOCAL_DAYS BACKUP_AGE BACKUP_TEST_FOLDER BACKUP_PROJECT; do
  holds "backup.env (the example) talks about $variable" grep -q "$variable" "$DEPLOY/examples/backup.env"
  holds "backup.sh reads $variable" grep -q "$variable" "$DEPLOY/backup.sh"
done
fails "the example backup.env sets nothing that would run by itself as it is" grep -E '^(BACKUP_TEST_FOLDER|BACKUP_AGE|BACKUP_PROJECT)=' "$DEPLOY/examples/backup.env"

echo
echo "backup tests: $PASSED passed, $FAILED failed"
[ "$FAILED" -eq 0 ]
