#!/usr/bin/env bash
# For whoever keeps the copies, on a computer that has an age private key: opens the latest copy that arrived and checks
# that it is complete and readable. The site's machine cannot do this (the private key is not there): it only checks
# the age header and the size. A copy that nobody opened is a hope.
#
# Usage: verify-copy.sh [--key <file>] [--max-age <hours>] [--quiet] <file.tar.age | folder>
#   a folder: the latest copy of each project in it (<project>-YYYYMMDDTHHMMSSZ.tar.age).
#   --key        the age private key (age-keygen's file). Without it only what needs no key is checked: the age
#                   header, the size and how old the copy is. That is enough on the destination, from cron, to know
#                   that copies keep arriving; the opening is for the person who has the key.
#   --max-age   a copy older than this many hours is a problem (default 30: a nightly copy; 0: do not check, for an
#                   old copy opened on purpose).
#   --quiet    print nothing when all is well, for cron (which mails whatever a job prints).
# With the key: decrypts into a private temporary folder (it holds the .env with all the passwords: it is removed at the
# end; in TMPDIR, or /var/tmp, which needs room for the copy), checks that SHA256SUMS lists exactly the files that are
# there and that every one has its hash, that the site's database is read to the last block
# by pg_restore and has tables, that the state archive lists, and that the .env is there. It prints no secret.
# Environment: AGE (default age) and PG_RESTORE (default pg_restore; without it, docker with POSTGRES_IMAGE,
# postgres:18.6 as in docker-compose.yml, with no network): pg_restore must be at least the version of the
# database that made the dump (18).
# Exit code: 0 all good, 1 a problem (every line says which), 2 wrong use.
set -euo pipefail
KEY='' MAX_AGE=30 QUIET='' OUTPUT=''
while [ $# -gt 0 ]; do
  case "$1" in
    --key) KEY=${2:?}; shift 2 ;;
    --max-age) MAX_AGE=${2:?}; shift 2 ;;
    --quiet) QUIET=1; shift ;;
    -h|--help) awk 'NR > 1 && !/^#/ { exit } NR > 1 { sub(/^# ?/, ""); print }' "${BASH_SOURCE[0]}"; exit 0 ;;
    -*) echo "unknown option: $1" >&2; exit 2 ;;
    *) break ;;
  esac
done
[ $# -eq 1 ] || { echo "usage: verify-copy.sh [--key <file>] [--max-age <hours>] [--quiet] <file.tar.age | folder>" >&2; exit 2; }
[[ $MAX_AGE =~ ^[0-9]+$ ]] || { echo "--max-age: a number of hours" >&2; exit 2; }
TARGET=$1
read -r -a age <<< "${AGE:-age}"
if [ -n "${PG_RESTORE:-}" ]; then
  read -r -a pg_restore <<< "$PG_RESTORE"
elif command -v pg_restore >/dev/null; then
  pg_restore=(pg_restore)
else
  # The dumps go through it: the image of docker-compose.yml (same digest), with no network
  pg_restore=(docker run --rm -i --network none --entrypoint pg_restore
    "${POSTGRES_IMAGE:-postgres:18.6@sha256:e3d24f6906d0760bf4da0dae872415391ededefad23fd471c47dc53b0ddf3c42}")
fi
if [ -n "$KEY" ] && [ -z "${AGE:-}" ] && [ ! -r "$KEY" ]; then echo "the key $KEY cannot be read" >&2; exit 2; fi

copies=()
if [ -d "$TARGET" ]; then
  declare -A latest=()
  name='^([a-z0-9][a-z0-9_-]*)-[0-9]{8}T[0-9]{6}Z\.tar\.age$'
  while IFS= read -r file; do
    [[ $file =~ $name ]] && latest[${BASH_REMATCH[1]}]=$file  # in order: the latest stays
  done < <(find "$TARGET" -maxdepth 1 -type f -name '*-????????T??????Z.tar.age' -printf '%f\n' | LC_ALL=C sort)
  if [ "${#latest[@]}" -gt 0 ]; then
    while IFS= read -r project; do copies+=("$TARGET/${latest[$project]}"); done < <(printf '%s\n' "${!latest[@]}" | LC_ALL=C sort)
  fi
  [ "${#copies[@]}" -gt 0 ] || { echo "NO: there is no copy in $TARGET (<project>-YYYYMMDDTHHMMSSZ.tar.age)"; exit 1; }
else
  copies=("$TARGET")
fi

problems=0
write() { if [ -n "$QUIET" ]; then OUTPUT+="$*"$'\n'; else printf '%s\n' "$*"; fi; }
bad() { write "  NO: $*"; problems=$((problems + 1)); }
good() { write "  ok: $*"; }
work=''
trap '[ -z "$work" ] || rm -rf "$work"' EXIT

verify() {
  local copy=$1 name stamp moment hours size list file database entries tables first base free
  name=${copy##*/}
  write "== $name"
  [ -f "$copy" ] && [ ! -L "$copy" ] || { bad "not a file"; return; }
  size=$(stat -c %s "$copy")
  if [ "$(head -c 21 "$copy")" = "age-encryption.org/v1" ] && [ "$size" -gt 1000 ]; then
    good "age file, $size bytes"
  else
    bad "not an age file of a plausible size ($size bytes)"; return
  fi
  stamp=${name%.tar.age}; stamp=${stamp##*-}
  if moment=$(date -u -d "${stamp:0:4}-${stamp:4:2}-${stamp:6:2} ${stamp:9:2}:${stamp:11:2}:${stamp:13:2}" +%s 2>/dev/null); then
    hours=$(( ($(date -u +%s) - moment) / 3600 ))
    if [ "$hours" -lt -1 ]; then bad "the date in the name is in the future ($hours hours)"
    elif [ "$MAX_AGE" -eq 0 ]; then good "made $hours hours ago"
    elif [ "$hours" -le "$MAX_AGE" ]; then good "made $hours hours ago"
    else bad "made $hours hours ago: more than $MAX_AGE, have copies stopped arriving?"; fi
  else
    bad "the date in the name is not a date"
  fi
  [ -n "$KEY" ] || { write "  (without --key it is not opened)"; return; }

  # The copy opened is about as big as the copy; /tmp is often a small tmpfs, so the default is /var/tmp
  base=${TMPDIR:-/var/tmp}
  free=$(df -B1 --output=avail "$base" 2>/dev/null | tail -n 1 | tr -d ' ') || free=0
  if [ "${free:-0}" -lt $((2 * size + 268435456)) ]; then
    bad "little space in $base ($free bytes free): TMPDIR can point to another folder"; return
  fi
  work=$(mktemp -d "$base/verify.XXXXXX"); chmod 700 "$work"
  if ! "${age[@]}" -d -i "$KEY" < "$copy" | tar -x --no-same-owner -C "$work" 2>"$work/.errors"; then
    bad "does not open with this key: $(head -n 2 "$work/.errors" | tr '\n' ' ')"; rm -rf "$work"; work=''; return
  fi
  good "decrypted and opened"
  for file in database/bitmentor.dump database/roles.sql folder/.env folder/state.tar SHA256SUMS; do
    [ -s "$work/$file" ] || bad "$file is missing, or empty"
  done
  if [ -s "$work/SHA256SUMS" ]; then
    if (cd "$work" && sha256sum --quiet -c SHA256SUMS 2>"$work/.errors"); then
      good "$(wc -l < "$work/SHA256SUMS") files, all with their hash"
    else
      bad "hashes that do not match: $(head -n 3 "$work/.errors" | tr '\n' ' ')"
    fi
    # The list and the files must be the same: a file that is there and is not listed has no hash to be checked by
    if ! diff <(sed 's/^[0-9a-f]*  //' "$work/SHA256SUMS" | LC_ALL=C sort) \
        <(cd "$work" && find . -type f ! -name SHA256SUMS ! -name .errors | LC_ALL=C sort) > /dev/null; then
      bad "the files of the copy are not those of SHA256SUMS (one more or one less)"
    fi
  fi
  for database in bitmentor; do
    file=$work/database/$database.dump
    [ -s "$file" ] || continue
    if ! entries=$("${pg_restore[@]}" --list < "$file" 2>"$work/.errors"); then
      bad "$database.dump cannot be read: $(head -n 2 "$work/.errors" | tr '\n' ' ')"; continue
    fi
    tables=$(grep -c ' TABLE [a-z_]* ' <<< "$entries" || true)
    if [ "$tables" -eq 0 ]; then bad "$database.dump has no tables"; continue; fi
    # --list reads the table of contents only: a dump cut near its end passes it. This reads every block
    if "${pg_restore[@]}" -f - < "$file" 2>"$work/.errors" > /dev/null; then
      good "$database.dump: $tables tables, read in full"
    else
      bad "$database.dump is truncated or damaged: $(head -n 2 "$work/.errors" | tr '\n' ' ')"
    fi
  done
  grep -q '^CREATE ROLE ' "$work/database/roles.sql" 2>/dev/null || bad "roles.sql has no users"
  for file in folder/state:state; do
    [ -s "$work/${file%%:*}.tar" ] || continue
    if list=$(tar -tf "$work/${file%%:*}.tar" 2>/dev/null); then
      first=${list%%$'\n'*}
      if [[ $first == "${file##*:}/"* ]]; then good "${file%%:*}.tar: $(wc -l <<< "$list") entries"; else bad "${file%%:*}.tar does not start with ${file##*:}/"; fi
    else
      bad "${file%%:*}.tar is truncated or damaged"
    fi
  done
  rm -rf "$work"; work=''
}

for copy in "${copies[@]}"; do verify "$copy"; done
if [ "$problems" -eq 0 ]; then write "All good."; else write "$problems problems."; fi
if [ -z "$QUIET" ] || [ "$problems" -ne 0 ]; then printf '%s' "$OUTPUT"; fi
[ "$problems" -eq 0 ]
