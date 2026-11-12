#!/usr/bin/env bash
# End-to-end test of release.sh, check.sh and mode.sh, without docker, network or systemd.
# The real scripts run in a temporary folder that stands in for /opt/<project>; docker, curl, systemctl and the rest are the
# fakes in deploy/tests/fakes, which write the calls they receive and answer as the scenario decides.
# Usage: bash deploy/tests/test_release.sh        exits 0 if everything passes
set -uo pipefail
HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
DEPLOY_DIR=$(dirname "$HERE")
export DEPLOY_DIR
TMP=$(mktemp -d)
PID_LOCK=
trap '[ -z "$PID_LOCK" ] || kill "$PID_LOCK" 2>/dev/null; rm -rf "$TMP"' EXIT
PASSED=0
FAILED=0

# --- test tools ---------------------------------------------------------------------------------------------

title() { echo "== $*"; }

ko() {  # ko <description>: writes what is wrong, with the last output of the scripts
  FAILED=$((FAILED + 1))
  echo "  NO: $1"
  printf '%s\n' "${OUT:-}" | tail -n 12 | sed 's/^/        | /'
}

check() {  # check <description> <command...>: passes if the command succeeds
  local description=$1; shift
  if "$@" >/dev/null 2>&1; then PASSED=$((PASSED + 1)); else ko "$description"; fi
}

in_section() {  # in_section <file> <section> <line>: the line, identical, is inside [section]: systemd ignores StartLimit* in [Service]
  awk -v section="[$2]" -v line="$3" '/^\[/ { inside = ($0 == section) } inside && $0 == line { found = 1 } END { exit !found }' "$1"
}

lacks() {  # lacks <description> <command...>: passes if the command fails
  local description=$1; shift
  if "$@" >/dev/null 2>&1; then ko "$description"; else PASSED=$((PASSED + 1)); fi
}

equal() {  # equal <description> <expected> <found>
  if [ "$2" = "$3" ]; then PASSED=$((PASSED + 1)); else ko "$1: expected «$2», found «$3»"; fi
}

contains() {  # contains <description> <text> <fragment>
  if [[ $2 == *"$3"* ]]; then PASSED=$((PASSED + 1)); else ko "$1: missing «$3» in «$2»"; fi
}

days_ago() { date -u -d "$1 days ago" +%F; }
count() { grep -c -- "$1" "$2" || true; }  # how many lines of a file match

machine() {  # machine <name>: a new /opt/<project>, with the scripts in bin/ and the fakes ready
  M=$TMP/$1 D=$TMP/$1/opt F=$TMP/$1/fake
  rm -rf "$M"
  mkdir -p "$D/state" "$D/bin" "$F/container"
  cp "$DEPLOY_DIR"/release.sh "$DEPLOY_DIR"/check.sh "$DEPLOY_DIR"/mode.sh "$D/bin/"
  chmod 755 "$D"/bin/*.sh
  cat > "$D/release.env" <<'EOF'
PROJECT=test
SITE=test.example.org
WEB_IMAGE_REPO=registry.fake/test/bitmentor
CADDY_IMAGE_REPO=registry.fake/test/bitmentor-caddy
EOF
  cat > "$D/.env" <<'EOF'
SECRET_KEY=fake/with&characters
SITE_MODE=development
SITE_MODE_SINCE=2026-01-01
NETWORK=signet
EOF
  cat > "$D/alerts.env" <<'EOF'
TELEGRAM_BOT_TOKEN=123456:FAKE
TELEGRAM_CHAT_IDS=111
EOF
  export FAKE_DIR=$F FAKE_CALLS=$M/calls.log FAKE_COMPOSE=$M/compose.log
  export FAKE_TELEGRAM=$M/telegram.log FAKE_ARGS=$M/args.log
  : > "$FAKE_CALLS"; : > "$FAKE_COMPOSE"; : > "$FAKE_TELEGRAM"; : > "$FAKE_ARGS"
  OUT=
}

run() {  # run <command...>: with the fakes first in the PATH; the output stays in OUT, the exit code in EXIT_CODE
  OUT=$(PATH="$HERE/fakes:$PATH" "$@" 2>&1); EXIT_CODE=$?
}

reset() { : > "$FAKE_CALLS"; : > "$FAKE_COMPOSE"; : > "$FAKE_TELEGRAM"; : > "$FAKE_ARGS"; }

registry() { echo "$1" > "$F/registry_version"; }  # the version that carries the active tag
healthy() { printf '%s\n' "$@" > "$F/healthy"; }          # the versions the site answers for
do_release() { run "$D/bin/release.sh" "$D"; }
activated_versions() {  # the versions passed to «compose up», in order
  grep '^compose .* up ' "$FAKE_CALLS" | awk '{printf "%s ", $2}'
}

# --- systemd units -----------------------------------------------------------------------------------------------------

title "systemd units: they call the scripts that release.sh installs in bin/"
U=$DEPLOY_DIR/systemd
check "release.service" grep -qx 'ExecStart=/opt/%i/bin/release.sh /opt/%i' "$U/bitmentor-release@.service"
check "check.service" grep -qx 'ExecStart=/opt/%i/bin/check.sh /opt/%i' "$U/bitmentor-check@.service"
check "release every minute" grep -qx 'OnUnitInactiveSec=1min' "$U/bitmentor-release@.timer"
check "check every 10 minutes" grep -qx 'OnUnitActiveSec=10min' "$U/bitmentor-check@.timer"
check "sentinel.service" grep -qx 'ExecStart=/opt/%i/current/deploy/sentinel.sh /opt/%i' "$U/bitmentor-sentinel@.service"
check "sentinel every hour" grep -qx 'OnUnitInactiveSec=1h' "$U/bitmentor-sentinel@.timer"
check "sentinel.sh executable" test -x "$DEPLOY_DIR/sentinel.sh"
check "dashboard.service" grep -qx 'ExecStart=/opt/%i/current/deploy/dashboard.sh /opt/%i' "$U/bitmentor-dashboard@.service"
check "dashboard: 10 is a success" grep -qx 'SuccessExitStatus=10' "$U/bitmentor-dashboard@.service"
check "dashboard: retries" grep -qx 'Restart=on-failure' "$U/bitmentor-dashboard@.service"
check "dashboard: with a limit, in [Unit]" in_section "$U/bitmentor-dashboard@.service" Unit 'StartLimitBurst=4'
check "dashboard every morning" grep -qx 'OnCalendar=\*-\*-\* 06:30:00 UTC' "$U/bitmentor-dashboard@.timer"
check "dashboard.sh executable" test -x "$DEPLOY_DIR/dashboard.sh"
lacks "no closing unit any more" test -e "$U/bitmentor-closing@.service"
lacks "no closing script any more" test -e "$DEPLOY_DIR/closing.sh"
check "osm.service" grep -qx 'ExecStart=/opt/%i/current/deploy/osm.sh /opt/%i' "$U/bitmentor-osm@.service"
check "osm: 10 is a success" grep -qx 'SuccessExitStatus=10' "$U/bitmentor-osm@.service"
check "osm: retries" in_section "$U/bitmentor-osm@.service" Service 'Restart=on-failure'
check "osm: retries every hour" in_section "$U/bitmentor-osm@.service" Service 'RestartSec=1h'
check "osm: the limit is in [Unit]: tries" in_section "$U/bitmentor-osm@.service" Unit 'StartLimitBurst=4'
check "osm: the limit is in [Unit]: window" in_section "$U/bitmentor-osm@.service" Unit 'StartLimitIntervalSec=12h'
lacks "osm: no limit in [Service], where systemd ignores it" in_section "$U/bitmentor-osm@.service" Service 'StartLimitBurst=4'
lacks "osm: no window in [Service]" in_section "$U/bitmentor-osm@.service" Service 'StartLimitIntervalSec=12h'
check "osm once a month" grep -qx 'OnCalendar=\*-\*-01 03:30:00 UTC' "$U/bitmentor-osm@.timer"
check "osm.sh executable" test -x "$DEPLOY_DIR/osm.sh"
check "osm.sh does not write: only osm_sync" grep -q 'manage.py osm_sync --send' "$DEPLOY_DIR/osm.sh"
fails_osm() { ! grep -qE '^[^#]*osm_upload' "$DEPLOY_DIR/osm.sh"; }
check "osm.sh never runs osm_upload" fails_osm
check "meetups.service" grep -qx 'ExecStart=/opt/%i/current/deploy/meetups.sh /opt/%i' "$U/bitmentor-meetups@.service"
check "meetups: retries" grep -qx 'Restart=on-failure' "$U/bitmentor-meetups@.service"
check "meetups every day" grep -qx 'OnCalendar=\*-\*-\* 05:40:00 UTC' "$U/bitmentor-meetups@.timer"
check "meetups.sh executable" test -x "$DEPLOY_DIR/meetups.sh"

# --- release.sh: from the first installation to the update --------------------------------------------------------

title "release.sh: first installation"
machine installation
printf 'BACKUP_KEEP=2\n' >> "$D/release.env"
touch "$F/community"
echo 'SECRET=fake' > "$D/compose.locale.yml"
registry v2026.01.01; healthy v2026.01.01
do_release
equal "exits with 0" 0 "$EXIT_CODE"
contains "says so" "$OUT" "installed v2026.01.01"
equal "state/version" v2026.01.01 "$(cat "$D/state/version")"
lacks "without a previous one there is no state/previous" test -e "$D/state/previous"
lacks "no failed" test -e "$D/state/failed"
check "state/history: date and version" grep -Eq '^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9:]{8}Z v2026.01.01$' "$D/state/history"
equal "state/services from the compose" "web caddy postgres" "$(tr '\n' ' ' < "$D/state/services" | sed 's/ $//')"
check "registry.ok is from now" test "$(( $(date +%s) - $(cat "$D/state/registry.ok") ))" -lt 60
check "registry.error empty" test ! -s "$D/state/registry.error"
lacks "without a previous version no backup" grep -q pg_dump "$FAKE_CALLS"
equal "a single activation, with the images of the version" "v2026.01.01 " "$(activated_versions)"
check "up without build and with --remove-orphans" grep -qx 'compose v2026.01.01 up -d --no-build --remove-orphans' "$FAKE_CALLS"
check "images pulled per version" grep -qx 'docker pull registry.fake/test/bitmentor-caddy:v2026.01.01' "$FAKE_CALLS"
check "compose files from the image" grep -qx '# docker-compose.yml v2026.01.01' "$D/current/docker-compose.yml"
check "community compose copied" test -f "$D/releases/v2026.01.01/compose.community.yml"
check "compose: release, community and local files, in the project" grep -qF -- \
  "-p test --project-directory $D/current --env-file $D/.env -f current/docker-compose.yml -f current/compose.release.yml -f current/compose.community.yml -f compose.locale.yml up" "$FAKE_COMPOSE"
lacks "no leftover current.new or .tmp" bash -c "ls -d '$D'/current.new '$D'/releases/*.tmp"
check "the scripts in bin/ are those of the version" grep -qx '# version v2026.01.01' "$D/bin/release.sh" "$D/bin/check.sh" "$D/bin/mode.sh"
equal "bin/ executable" 755 "$(stat -c %a "$D/bin/release.sh")"
lacks "bin/ without .new" bash -c "ls '$D'/bin/*.new"
equal "state private (umask 077)" 600 "$(stat -c %a "$D/state/version")"

title "release.sh: same version, does nothing"
reset
do_release
equal "exits with 0" 0 "$EXIT_CODE"
equal "only pull and inspect" "docker image inspect registry.fake/test/bitmentor:active|docker pull registry.fake/test/bitmentor:active" \
  "$(sort "$FAKE_CALLS" | paste -sd'|')"

title "release.sh: update succeeded"
for old in 10 5 2; do  # three copies of the previous database: two are kept, with the new one
  echo "old dump" > "$D/backup/before-v2026.01.00-$old.dump"; touch -d "$old days ago" "$D/backup/before-v2026.01.00-$old.dump"
done
echo "not a dump" > "$D/backup/note.txt"
mkdir -p "$D/releases/v2026.01.00"
printf 'v2026.01.00\nv2026.01.01\nv2026.01.02\nactive\n' > "$F/tag"   # v2026.01.00 is an image from two versions ago
registry v2026.01.02; healthy v2026.01.01 v2026.01.02
cp "$D/bin/release.sh" "$M/release.before"
exec 7< "$D/bin/release.sh"
inode=$(stat -c %i "$D/bin/release.sh")
reset
do_release
equal "exits with 0" 0 "$EXIT_CODE"
contains "says so" "$OUT" "installed v2026.01.02"
equal "version" v2026.01.02 "$(cat "$D/state/version")"
equal "previous" v2026.01.01 "$(cat "$D/state/previous")"
lacks "the earlier failed goes away" test -e "$D/state/failed"
equal "history: two lines" 2 "$(wc -l < "$D/state/history")"
check "history: last line" grep -q ' v2026.01.02$' "$D/state/history"
# the database is saved with the previous site, before activating the new one
check "backup asked of the postgres of v2026.01.01" grep -qx 'compose v2026.01.01 exec -T postgres pg_dump -U postgres -Fc bitmentor' "$FAKE_CALLS"
equal "backup written" "database dump with the site v2026.01.01" "$(cat "$D/backup/before-v2026.01.02.dump")"
lacks "no .tmp leftovers in the backup" bash -c "ls '$D'/backup/*.tmp"
equal "backup private" 600 "$(stat -c %a "$D/backup/before-v2026.01.02.dump")"
equal "order: backup, then up of v2026.01.02" "pg_dump up" "$(grep -E 'pg_dump|compose v2026.01.02 up ' "$FAKE_CALLS" | sed -E 's/.*(pg_dump).*/\1/; s/.*( up ).*/up/' | paste -sd' ')"
equal "dumps kept (BACKUP_KEEP=2)" "before-v2026.01.00-2.dump before-v2026.01.02.dump" "$(cd "$D/backup" && ls -1 ./*.dump | sed "s|^\./||" | paste -sd' ')"
check "the other backup files stay" test -f "$D/backup/note.txt"
# the scripts travel with the release, but the file that is running is not rewritten
check "bin/ updated to the new version" grep -qx '# version v2026.01.02' "$D/bin/release.sh" "$D/bin/check.sh" "$D/bin/mode.sh"
lacks "release.sh has a new file" test "$(stat -c %i "$D/bin/release.sh")" = "$inode"
equal "the file that was running is intact" "$(cat "$M/release.before")" "$(cat <&7)"
exec 7<&-
equal "bin/ executable" 755 "$(stat -c %a "$D/bin/mode.sh")"
# the new version, the previous one and the tag are kept
check "the old site image is removed" grep -qx 'docker rmi registry.fake/test/bitmentor:v2026.01.00' "$FAKE_CALLS"
check "and the Caddy one" grep -qx 'docker rmi registry.fake/test/bitmentor-caddy:v2026.01.00' "$FAKE_CALLS"
lacks "v2026.01.01, v2026.01.02 and active stay" grep -E 'docker rmi .*(:v2026.01.01|:v2026.01.02|:active)' "$FAKE_CALLS"
lacks "releases/v2026.01.00 removed" test -d "$D/releases/v2026.01.00"
check "releases/v2026.01.01 and v2026.01.02 stay" test -d "$D/releases/v2026.01.01" -a -d "$D/releases/v2026.01.02"

title "release.sh: going back by hand by putting the tag back"
registry v2026.01.01; healthy v2026.01.01 v2026.01.02
do_release
equal "exits with 0" 0 "$EXIT_CODE"
equal "version" v2026.01.01 "$(cat "$D/state/version")"
equal "previous" v2026.01.02 "$(cat "$D/state/previous")"

# --- release.sh: when the site does not answer ------------------------------------------------------------------------

title "release.sh: the site does not answer after the update, it rolls back"
machine rollback
registry v2026.01.01; healthy v2026.01.01
do_release
registry v2026.01.02; healthy v2026.01.01      # v2026.01.02 never answers
reset
do_release
equal "exits with 1" 1 "$EXIT_CODE"
contains "says what happened" "$OUT" "v2026.01.02 does not answer"
contains "and that it rolled back" "$OUT" "rolled back to v2026.01.01"
equal "state/version stays" v2026.01.01 "$(cat "$D/state/version")"
equal "state/failed" v2026.01.02 "$(cat "$D/state/failed")"
lacks "no previous" test -e "$D/state/previous"
equal "history: only the first" 1 "$(wc -l < "$D/state/history")"
equal "v2026.01.02 activated and then v2026.01.01 again" "v2026.01.02 v2026.01.01 " "$(activated_versions)"
equal "60 tries on v2026.01.02, one on v2026.01.01" "61" "$(count 'exec web' "$FAKE_CALLS")"
check "current/ is back to that of v2026.01.01" grep -qx '# docker-compose.yml v2026.01.01' "$D/current/docker-compose.yml"
check "bin/ has not changed" grep -qx '# version v2026.01.01' "$D/bin/release.sh"
check "the database had been saved" test -f "$D/backup/before-v2026.01.02.dump"
equal "previous services" "web caddy postgres" "$(tr '\n' ' ' < "$D/state/services" | sed 's/ $//')"

title "release.sh: check.sh reports the failed version"
printf 'web\ncaddy\npostgres\n' > "$D/state/services"
for service in web caddy postgres; do echo "running Up 3 hours" > "$F/container/$service"; done
date +%s > "$D/state/registry.ok"
reset
run "$D/bin/check.sh" "$D"
contains "alarm" "$(cat "$FAKE_TELEGRAM")" "release v2026.01.02 did not start: staying on v2026.01.01"

title "release.sh: a version that already failed is not tried again"
reset
do_release
equal "exits with 0" 0 "$EXIT_CODE"
lacks "no pull of the version" grep -q 'pull .*:v2026.01.02' "$FAKE_CALLS"
equal "no activation" "" "$(activated_versions)"
equal "state/failed stays" v2026.01.02 "$(cat "$D/state/failed")"
equal "state/version stays" v2026.01.01 "$(cat "$D/state/version")"
do_release
equal "not the next time either" 0 "$EXIT_CODE"

title "release.sh: the version after the failed one is tried, and if it works it clears the failed one"
registry v2026.01.03; healthy v2026.01.01 v2026.01.03
do_release
equal "exits with 0" 0 "$EXIT_CODE"
equal "version" v2026.01.03 "$(cat "$D/state/version")"
equal "previous is v2026.01.01, not the failed one" v2026.01.01 "$(cat "$D/state/previous")"
lacks "failed removed" test -e "$D/state/failed"

title "release.sh: the previous version does not answer either"
machine double
registry v2026.01.01; healthy v2026.01.01
do_release
registry v2026.01.02; healthy ""
do_release
equal "exits with 1" 1 "$EXIT_CODE"
contains "says so" "$OUT" "v2026.01.01 does not answer either"
equal "state/failed" v2026.01.02 "$(cat "$D/state/failed")"

title "release.sh: the first installation does not answer"
machine firstfailed
registry v2026.01.01; healthy ""
do_release
equal "exits with 1" 1 "$EXIT_CODE"
contains "says so" "$OUT" "v2026.01.01 does not answer"
lacks "without a previous version there is no rolling back" bash -c "echo '$OUT' | grep -E 'rolled back|either'"
equal "a single activation" "v2026.01.01 " "$(activated_versions)"
equal "state/failed" v2026.01.01 "$(cat "$D/state/failed")"
lacks "there is no state/version" test -e "$D/state/version"
lacks "without a version no backup" grep -q pg_dump "$FAKE_CALLS"
reset
do_release
equal "does not retry" 0 "$EXIT_CODE"
equal "no activation" "" "$(activated_versions)"

title "release.sh: an image without the compose files leaves no containers around"
machine nofile
registry v2026.01.01; healthy v2026.01.01
echo /app/tor.sh > "$F/cp_ko"
do_release
equal "exits with 1" 1 "$EXIT_CODE"
lacks "activates nothing" grep -q ' up ' "$FAKE_CALLS"
lacks "does not write the version" test -e "$D/state/version"
equal "the created container is removed" "docker create registry.fake/test/bitmentor:v2026.01.01|docker rm container-fake" \
  "$(grep -E 'docker (create|rm)' "$FAKE_CALLS" | paste -sd'|')"
lacks "and it is not a failed version: it is tried again" test -e "$D/state/failed"

title "release.sh: IPv6 on the Docker network only with IPV6_SUBNET in the .env"
machine ipv6
registry v2026.01.01; healthy v2026.01.01
do_release
check "the IPv6 file comes from the image" test -f "$D/current/compose.ipv6.yml"
lacks "without IPV6_SUBNET it does not enter the compose" grep -qF -- "compose.ipv6.yml" "$FAKE_COMPOSE"
machine ipv6on
echo 'IPV6_SUBNET=fd12:3456:789a:1::/64' >> "$D/.env"
registry v2026.01.01; healthy v2026.01.01
do_release
equal "installs" v2026.01.01 "$(cat "$D/state/version")"
check "with IPV6_SUBNET it enters the compose, after those of the release" grep -qF -- \
  "-f current/docker-compose.yml -f current/compose.release.yml -f current/compose.ipv6.yml up" "$FAKE_COMPOSE"
reset
run "$D/bin/mode.sh" "$D" maintenance
equal "mode.sh changes" maintenance "$(grep -m1 '^SITE_MODE=' "$D/.env" | cut -d= -f2)"
equal "mode.sh uses the same files" 0 \
  "$(grep -- '^-p ' "$FAKE_COMPOSE" | grep -vc -- "-f current/compose.release.yml -f current/compose.ipv6.yml" || true)"
for value in 'IPV6_SUBNET=""' "IPV6_SUBNET=''" 'IPV6_SUBNET= # after' $'IPV6_SUBNET=fd12:3456:789a:1::/64\nIPV6_SUBNET='; do
  machine ipv6empty
  printf '%s\n' "$value" >> "$D/.env"
  registry v2026.01.01; healthy v2026.01.01
  do_release
  equal "$value: installs" v2026.01.01 "$(cat "$D/state/version" 2>/dev/null)"
  lacks "$value: compose.ipv6.yml stays out" grep -qF -- "compose.ipv6.yml" "$FAKE_COMPOSE"
done
machine ipv6between
echo /app/compose.ipv6.yml > "$F/cp_ko"
registry v2026.01.01; healthy v2026.01.01
do_release
lacks "installed by an older release.sh: current/ without the file" test -f "$D/current/compose.ipv6.yml"
rm "$F/cp_ko"
echo 'IPV6_SUBNET="fd12:3456:789a:1::/64"' >> "$D/.env"
reset
do_release
equal "same version: exits with 0" 0 "$EXIT_CODE"
check "with IPV6_SUBNET the file comes from the image of the installed version" test -f "$D/current/compose.ipv6.yml"
check "also in the release folder, to roll back" test -f "$D/releases/v2026.01.01/compose.ipv6.yml"
lacks "and reactivates nothing" grep -q ' up ' "$FAKE_CALLS"
machine ipv6old
echo 'IPV6_SUBNET=fd12:3456:789a:1::/64' >> "$D/.env"
registry v2026.01.01; healthy v2026.01.01
echo /app/compose.ipv6.yml > "$F/cp_ko"
do_release
equal "an older image, without the file, installs all the same" v2026.01.01 "$(cat "$D/state/version")"
lacks "and the compose does not look for it" grep -qF -- "compose.ipv6.yml" "$FAKE_COMPOSE"

title "release.sh and mode.sh: the folder may be given as a relative path too"
machine relative
registry v2026.01.01; healthy v2026.01.01
run bash -c 'cd "$1" && opt/bin/release.sh opt' _ "$M"
equal "release.sh installs" v2026.01.01 "$(cat "$D/state/version")"
run bash -c 'cd "$1" && opt/bin/mode.sh opt maintenance' _ "$M"
equal "mode.sh changes" maintenance "$(grep -m1 '^SITE_MODE=' "$D/.env" | cut -d= -f2)"
equal "docker compose always gets absolute paths" 0 \
  "$(grep -- '^-p ' "$FAKE_COMPOSE" | grep -vc -- "--project-directory $D/current --env-file $D/.env" || true)"

# --- release.sh: registry and lock ------------------------------------------------------------------------------------

title "release.sh: registry unreachable, exits with 0 and check.sh alerts after an hour"
machine registrydown
registry v2026.01.01; healthy v2026.01.01
do_release
old=$(( $(date +%s) - 1800 ))
echo "$old" > "$D/state/registry.ok"
echo registry.fake/test/bitmentor:active > "$F/pull_ko"
reset
do_release
equal "exits with 0" 0 "$EXIT_CODE"
contains "the error is in registry.error" "$(cat "$D/state/registry.error")" "registry unreachable"
equal "registry.ok does not move" "$old" "$(cat "$D/state/registry.ok")"
equal "no other call" "docker pull registry.fake/test/bitmentor:active" "$(cat "$FAKE_CALLS")"
equal "version intact" v2026.01.01 "$(cat "$D/state/version")"
printf 'web\ncaddy\npostgres\n' > "$D/state/services"
for service in web caddy postgres; do echo "running Up 3 hours" > "$F/container/$service"; done
reset
run "$D/bin/check.sh" "$D"
equal "half an hour: no alert" 0 "$(wc -l < "$FAKE_TELEGRAM")"
echo $(( $(date +%s) - 3500 )) > "$D/state/registry.ok"
run "$D/bin/check.sh" "$D"
equal "58 minutes: no alert" 0 "$(wc -l < "$FAKE_TELEGRAM")"
echo $(( $(date +%s) - 3700 )) > "$D/state/registry.ok"
run "$D/bin/check.sh" "$D"
contains "more than an hour: alert" "$(cat "$FAKE_TELEGRAM")" "the image registry has not answered for more than an hour"
rm -f "$F/pull_ko"
reset
do_release
check "registry back, registry.ok is renewed" test "$(( $(date +%s) - $(cat "$D/state/registry.ok") ))" -lt 60
lacks "registry.error is emptied" test -s "$D/state/registry.error"
run "$D/bin/check.sh" "$D"
contains "and check.sh says so" "$(cat "$FAKE_TELEGRAM")" "all fine again"

# --- release.sh: images pinned by digest -------------------------------------------------------------------------------

dg() { printf 'sha256:%064d' "$1"; }  # dg <n>: a well-formed fake digest
WEB_R=registry.fake/test/bitmentor CADDY_R=registry.fake/test/bitmentor-caddy
pin() {  # pin <tag> <web digest> <caddy digest>: release.env with digests, which the fake registry knows as <tag>
  sed -i '/^TAG=/d; /_DIGEST=/d' "$D/release.env"
  printf 'TAG=%s\nWEB_DIGEST=%s\nCADDY_DIGEST=%s\n' "$1" "$2" "$3" >> "$D/release.env"
}
knows() { printf '%s %s\n' "$2" "$1" >> "$F/digests"; }  # knows <version> <digest>: the registry has it

title "release.sh: digests, pulled by digest and run by digest"
machine digest
knows v2026.01.01 "$(dg 11)"; knows v2026.01.01 "$(dg 12)"
pin v2026.01.01 "$(dg 11)" "$(dg 12)"; healthy v2026.01.01 v2026.01.02
do_release
equal "exits with 0" 0 "$EXIT_CODE"
contains "installed" "$OUT" "installed v2026.01.01"
lacks "no warning with digests" grep -q "warning" <<< "$OUT"
check "web pulled by digest" grep -qx "docker pull $WEB_R@$(dg 11)" "$FAKE_CALLS"
check "caddy pulled by digest" grep -qx "docker pull $CADDY_R@$(dg 12)" "$FAKE_CALLS"
lacks "never by tag" grep -q "docker pull .*:v2026" "$FAKE_CALLS"
check "the tag stays for people, locally" grep -qx "docker tag $WEB_R@$(dg 11) $WEB_R:v2026.01.01" "$FAKE_CALLS"
check "compose runs the digests" grep -q "^docker create $WEB_R@$(dg 11)$" "$FAKE_CALLS"
check "images written with the release" grep -qx "CADDY_IMAGE=$CADDY_R@$(dg 12)" "$D/current/images"
equal "state/version is the tag" v2026.01.01 "$(cat "$D/state/version")"
reset; run "$D/bin/mode.sh" "$D" maintenance
equal "mode.sh: exits with 0" 0 "$EXIT_CODE"
check "mode.sh: recreates with the digest" grep -q "^compose v2026.01.01 up" "$FAKE_CALLS"

title "release.sh: digests, the update fails and the rollback runs the old digests"
reset
knows v2026.01.02 "$(dg 21)"; knows v2026.01.02 "$(dg 22)"
pin v2026.01.02 "$(dg 21)" "$(dg 22)"; healthy v2026.01.01
do_release
equal "exits with 1" 1 "$EXIT_CODE"
contains "rolled back" "$OUT" "rolled back to v2026.01.01"
equal "activated: new, then old" "v2026.01.02 v2026.01.01 " "$(activated_versions)"
equal "current/ runs the old web digest" "WEB_IMAGE=$WEB_R@$(dg 11)" "$(grep WEB_IMAGE "$D/current/images")"
lacks "the rollback pulls nothing by tag" grep -q "docker pull .*:v2026" "$FAKE_CALLS"

title "release.sh: digests, the same version with another digest is refused"
reset; rm -f "$D/state/failed"
knows v2026.01.01 "$(dg 13)"
pin v2026.01.01 "$(dg 13)" "$(dg 12)"
do_release
equal "exits with 1" 1 "$EXIT_CODE"
contains "says why" "$OUT" "already installed with another digest"

title "release.sh: digests, a pulled image that is not the digest is refused"
machine mismatch
knows v2026.01.01 "$(dg 11)"; knows v2026.01.01 "$(dg 12)"; healthy v2026.01.01
pin v2026.01.01 "$(dg 11)" "$(dg 12)"
dg 11 > "$F/mismatch"
do_release
equal "web: exits with 1" 1 "$EXIT_CODE"
contains "web: says why" "$OUT" "the web image is not $(dg 11)"
lacks "web: nothing started" grep -q " up " "$FAKE_CALLS"
reset; dg 12 > "$F/mismatch"
do_release
equal "caddy: exits with 1" 1 "$EXIT_CODE"
contains "caddy: says why" "$OUT" "the caddy image is not $(dg 12)"
lacks "caddy: nothing started" grep -q " up " "$FAKE_CALLS"
lacks "caddy: no version" test -e "$D/state/version"

title "release.sh: digests, the digest's version must be TAG"
machine wrongtag
knows v2026.01.01 "$(dg 11)"; knows v2026.01.01 "$(dg 12)"; healthy v2026.01.01
pin v2026.01.02 "$(dg 11)" "$(dg 12)"
do_release
equal "exits with 1" 1 "$EXIT_CODE"
contains "says why" "$OUT" "not TAG=v2026.01.02"

title "release.sh: malformed or half digests are refused, in any mode"
for pair in "sha256:abc $(dg 12)" "$(dg 11) md5:$(printf '%032d' 1)" "$(dg 11) " " $(dg 12)" "SHA256:$(printf '%064d' 1) $(dg 12)"; do
  machine malformed
  read -r web caddy <<< "$pair"
  pin v2026.01.01 "${web:-}" "${caddy:-}"
  do_release
  equal "«$pair»: exits with 1" 1 "$EXIT_CODE"
  contains "«$pair»: refused" "$OUT" "refused:"
  lacks "«$pair»: nothing pulled" grep -q "docker pull" "$FAKE_CALLS"
done

title "release.sh: without digests, refused in production and on mainnet, a warning elsewhere"
for line in SITE_MODE=production NETWORK=mainnet 'NETWORK="mainnet"' 'export SITE_MODE=production' \
  'SITE_MODE = "production"' "  NETWORK='mainnet'  # real money"; do
  machine strict
  sed -i '/^SITE_MODE=/d; /^NETWORK=/d' "$D/.env"; echo "$line" >> "$D/.env"
  registry v2026.01.01; healthy v2026.01.01
  do_release
  equal "$line: exits with 1" 1 "$EXIT_CODE"
  contains "$line: says why" "$OUT" "needs WEB_DIGEST and CADDY_DIGEST"
  lacks "$line: nothing pulled" grep -q "docker pull" "$FAKE_CALLS"
done
machine loose
registry v2026.01.01; healthy v2026.01.01
do_release
equal "development and signet: exits with 0" 0 "$EXIT_CODE"
contains "with a warning" "$OUT" "warning: release.env has no WEB_DIGEST"

title "release.sh and mode.sh: strict, a version installed by tag is neither rolled back to nor recreated"
machine bytag
registry v2026.01.01; healthy v2026.01.01
do_release
equal "installed by tag on signet" v2026.01.01 "$(cat "$D/state/version")"
reset; run "$D/bin/mode.sh" "$D" production
equal "mode.sh production: exits with 1" 1 "$EXIT_CODE"
contains "mode.sh production: says why" "$OUT" "installed by tag, not by digest"
check "mode.sh production: .env untouched" grep -qx SITE_MODE=development "$D/.env"
lacks "mode.sh production: nothing recreated" grep -q " up " "$FAKE_CALLS"
sed -i 's/^NETWORK=.*/NETWORK=mainnet/' "$D/.env"
reset; run "$D/bin/mode.sh" "$D" maintenance
equal "mode.sh on mainnet: exits with 1" 1 "$EXIT_CODE"
reset
knows v2026.01.02 "$(dg 21)"; knows v2026.01.02 "$(dg 22)"
pin v2026.01.02 "$(dg 21)" "$(dg 22)"; healthy v2026.01.01
do_release
equal "rollback: exits with 1" 1 "$EXIT_CODE"
contains "rollback: refused, says why" "$OUT" "no rollback to v2026.01.01"
equal "rollback: only the new version activated" "v2026.01.02 " "$(activated_versions)"

title "release.sh: the version label must look like a CI version"
for label in 'v1' 'v2026.01.01;rm -rf /' '../../etc' 'v2026.1.1' ''; do
  machine label
  registry "$label"; healthy "$label"
  do_release
  equal "«$label»: exits with 1" 1 "$EXIT_CODE"
  contains "«$label»: refused" "$OUT" "version label is not"
  lacks "«$label»: nothing started" grep -q " up " "$FAKE_CALLS"
done

title "release.sh: lock already taken"
machine lock
registry v2026.01.01; healthy v2026.01.01
( exec 8>"$D/state/.lock"; flock 8; exec sleep 300 ) &
PID_LOCK=$!
for _ in $(seq 1 50); do flock -n "$D/state/.lock" true 2>/dev/null || break; sleep 0.1; done
do_release
equal "exits with 0" 0 "$EXIT_CODE"
equal "saying nothing" "" "$OUT"
equal "calling nothing" 0 "$(wc -c < "$FAKE_CALLS")"
lacks "installing nothing" test -e "$D/state/version"
kill "$PID_LOCK"; wait "$PID_LOCK" 2>/dev/null; PID_LOCK=
do_release
equal "lock released, installs" v2026.01.01 "$(cat "$D/state/version")"

# --- check.sh ----------------------------------------------------------------------------------------------------

healthy() {  # a healthy machine: services running, site answering, registry fresh, no alarm
  machine "${1:-check}"
  printf 'web\ncaddy\npostgres\n' > "$D/state/services"
  for service in web caddy postgres; do echo "running Up 3 hours" > "$F/container/$service"; done
  echo v2026.01.01 > "$D/state/version"
  date +%s > "$D/state/registry.ok"
}
broken() { rm -f "$F"/{health_code,health_body,certificate_ko,root_disk,docker_disk,memory,unit_failed} "$D/state/failed" "$D/state/alerts"; }
check_now() { reset; run "$D/bin/check.sh" "$D"; }
messages() { wc -l < "$FAKE_TELEGRAM"; }

title "check.sh: the alarm goes off only when the state changes"
healthy
check_now
equal "all well from the start: no message" 0 "$(messages)"
lacks "and no state/alerts" test -e "$D/state/alerts"
echo "exited Exited (1) 2 minutes ago" > "$F/container/web"
check_now
equal "a fault: one message" 1 "$(messages)"
contains "with the name of the site and of the machine" "$(cat "$FAKE_TELEGRAM")" "⚠️ test.example.org (machine)|- web: exited"
contains "to the chat" "$(cat "$FAKE_TELEGRAM")" "chat=111 token=123456:FAKE"
contains "and in the log" "$OUT" "web: exited"
check "state/alerts remembers the problems" grep -qx 'web: exited' "$D/state/alerts"
check_now
equal "same fault right after: no message" 0 "$(messages)"
echo "restarting Restarting (1) 5 seconds ago" > "$F/container/caddy"
check_now
equal "one more fault: new message" 1 "$(messages)"
contains "with both" "$(cat "$FAKE_TELEGRAM")" "- web: exited|- caddy: restarting"
echo "running Up 3 hours" > "$F/container/caddy"
check_now
equal "one of the two recovers: new message" 1 "$(messages)"
lacks "without caddy" grep -q 'caddy: restarting' "$FAKE_TELEGRAM"

title "check.sh: the reminder comes after 6 hours"
touch -d "5 hours ago" "$D/state/alerts"
check_now
equal "after 5 hours: nothing" 0 "$(messages)"
touch -d "7 hours ago" "$D/state/alerts"
check_now
equal "after 7 hours: reminder" 1 "$(messages)"
contains "with the earlier fault" "$(cat "$FAKE_TELEGRAM")" "- web: exited"
check_now
equal "and then it starts over" 0 "$(messages)"

title "check.sh: all fine again"
echo "running Up 3 hours" > "$F/container/web"
check_now
equal "one message" 1 "$(messages)"
contains "says it is over" "$(cat "$FAKE_TELEGRAM")" "✅ test.example.org (machine): all fine again"
equal "state/alerts empty" "" "$(cat "$D/state/alerts")"
check_now
equal "then silence" 0 "$(messages)"
touch -d "30 hours ago" "$D/state/alerts"
check_now
equal "even after many hours, if all is well it does not remind" 0 "$(messages)"

title "check.sh: if Telegram does not receive, it tries again next time"
echo "exited Exited (1) 2 minutes ago" > "$F/container/web"
echo 500 > "$F/telegram_code"
rm -f "$D/state/alerts"
check_now
contains "says so" "$OUT" "Telegram: sending to 111 failed"
lacks "state/alerts is not written" test -e "$D/state/alerts"
rm "$F/telegram_code"
check_now
equal "arrives the next time" 1 "$(messages)"
check "and this time it is written" test -e "$D/state/alerts"

title "check.sh: without Telegram it writes only to the log"
broken
printf 'TELEGRAM_CHAT_IDS=111\n' > "$D/alerts.env"
check_now
contains "says so" "$OUT" "Telegram not configured"
equal "does not call curl for Telegram" 0 "$(messages)"
printf 'TELEGRAM_BOT_TOKEN=123456:FAKE\n' > "$D/alerts.env"
check_now
contains "without chats either" "$OUT" "Telegram not configured"
rm "$D/alerts.env"
check_now
contains "and without alerts.env" "$OUT" "Telegram not configured"
TELEGRAM_BOT_TOKEN=123456:FAKE TELEGRAM_CHAT_IDS=111 check_now
contains "and does not take them from the environment" "$OUT" "Telegram not configured"
equal "no call to Telegram" 0 "$(messages)"

title "check.sh: the problems it can recognise"
# problem <description> <expected line> <what to do>: starts from a healthy machine, causes the fault, looks for the line in the message
problem() {
  healthy problems
  eval "$3"
  check_now
  contains "$1" "$(cat "$FAKE_TELEGRAM")" "- $2"
}
problem "missing container" "web: the container is not there" 'rm "$F/container/web"'
problem "healthcheck" "web: not well (healthcheck)" 'echo "running Up 2 hours (unhealthy)" > "$F/container/web"'
problem "stopped container" "postgres: exited" 'echo "exited Exited (137) 1 hour ago" > "$F/container/postgres"'
problem "site that does not answer" "the site does not answer" 'echo 000 > "$F/health_code"'
problem "site with another code" "the site answers 502 on /health/" 'echo 502 > "$F/health_code"'
if python3 -c 'import json' 2>/dev/null; then expected="the site reports: database"; else expected="the site reports: ?"; fi
problem "site problems" "$expected" 'echo 503 > "$F/health_code"; echo "{\"problems\": [\"database\"]}" > "$F/health_body"'
problem "503 without a readable body" "the site reports: ?" 'echo 503 > "$F/health_code"; echo "not json" > "$F/health_body"'
contains "the site is called by name on the local entry point" "$(cat "$FAKE_ARGS")" "--resolve test.example.org:443:127.0.0.1 https://test.example.org/health/"
problem "certificate" "the certificate of test.example.org expires within 14 days, or cannot be read" 'touch "$F/certificate_ko"'
problem "full disk" "disk /: only 5% free" 'echo 95 > "$F/root_disk"'
problem "docker disk" "disk /var/lib/docker: only 9% free" 'echo 91 > "$F/docker_disk"'
problem "memory" "memory: only 4% available" 'echo 4 > "$F/memory"'
problem "failed version" "release v2026.01.09 did not start: staying on v2026.01.01" 'echo v2026.01.09 > "$D/state/failed"'
problem "failed without an installed version" "release v2026.01.09 did not start: staying on the previous version" 'echo v2026.01.09 > "$D/state/failed"; rm "$D/state/version"'
problem "release unit" "the automatic release stopped with an error" 'touch "$F/unit_failed"'
problem "morning summary" "the morning summary (what to do for the shops) did not arrive: journalctl -u bitmentor-dashboard@test" \
  'echo bitmentor-dashboard@test.service > "$F/unit_failed"'
lacks "only the dashboard, not the release" grep -q "automatic release" "$FAKE_TELEGRAM"
problem "reading of OpenStreetMap" "the monthly reading of OpenStreetMap failed: journalctl -u bitmentor-osm@test" \
  'echo bitmentor-osm@test.service > "$F/unit_failed"'
lacks "only OpenStreetMap, not the dashboard" grep -q "morning summary" "$FAKE_TELEGRAM"
problem "reading of the meetups" "the reading of the meetups from Nostr failed: journalctl -u bitmentor-meetups@test" \
  'echo bitmentor-meetups@test.service > "$F/unit_failed"'
lacks "only the meetups, not the release" grep -q "automatic release" "$FAKE_TELEGRAM"
problem "registry" "the image registry has not answered for more than an hour" 'echo $(( $(date +%s) - 4000 )) > "$D/state/registry.ok"'
problem "registry never reached" "the image registry has not answered for more than an hour" 'rm "$D/state/registry.ok"'
problem "backup stalled" "the night backup has been failing for more than 26 hours: the last success was 2026-01-01T03:31:00Z" \
  'touch "$D/backup.env"; echo 2026-01-01T03:31:00Z > "$D/state/backup.ok"'
problem "backup stalled for 27 hours" "the night backup has been failing for more than 26 hours" \
  'touch "$D/backup.env"; date -u -d "27 hours ago" +%FT%TZ > "$D/state/backup.ok"'
problem "backup.ok unreadable" "the night backup has been failing for more than 26 hours: the last success was not a date" \
  'touch "$D/backup.env"; echo "not a date" > "$D/state/backup.ok"'
problem "backup never succeeded" "the night backup has never succeeded: backup.env has been there for more than 26 hours" \
  'touch -d "27 hours ago" "$D/backup.env"'
# backup.sh writes a second line (the copy's name, size and hash): only the first one is the time
problem "backup.ok with the copy line: stalled" "the night backup has been failing for more than 26 hours: the last success was 2026-01-01T03:31:00Z" \
  'touch "$D/backup.env"; printf "2026-01-01T03:31:00Z\ntest-20260101T033100Z.tar.age 123 bytes sha256 abc\n" > "$D/state/backup.ok"'
# no_alert <description> <what to do>: starts from a healthy machine and checks that nothing arrives
no_alert() {
  healthy problems
  eval "$2"
  check_now
  equal "$1" 0 "$(messages)"
}
no_alert "backup from 25 hours ago: nothing" 'touch "$D/backup.env"; date -u -d "25 hours ago" +%FT%TZ > "$D/state/backup.ok"'
no_alert "backup from 25 hours ago, with the copy line: nothing" 'touch "$D/backup.env"; printf "%s\ntest-20260101T033100Z.tar.age 123 bytes sha256 abc\n" "$(date -u -d "25 hours ago" +%FT%TZ)" > "$D/state/backup.ok"'
no_alert "backup just configured, never run: nothing" 'touch "$D/backup.env"'
no_alert "without backup.env nothing, even with an old backup.ok" 'echo 2026-01-01T03:31:00Z > "$D/state/backup.ok"'
no_alert "without backup.env nothing, even without backup.ok" 'rm -f "$D/state/backup.ok"'
healthy problems
echo 90 > "$F/root_disk"; echo 5 > "$F/memory"
check_now
equal "10% free and memory at 5%: still nothing" 0 "$(messages)"
check "the site check asks /health/ with a time limit" grep -q -- '-m 20' "$FAKE_ARGS"
lacks "the token does not go on the command line" grep -q 'FAKE' "$FAKE_ARGS"

# recipients <mode> <days since> [OPENING_DAYS]: puts in WHO who receives a message, with BEFORE=999 and CHAT_IDS=111 222
recipients() {
  healthy recipients
  printf 'TELEGRAM_BOT_TOKEN=123456:FAKE\nTELEGRAM_CHAT_IDS="111 222"\nTELEGRAM_CHAT_IDS_BEFORE=999\n' > "$D/alerts.env"
  [ -z "${3:-}" ] || echo "OPENING_DAYS=$3" >> "$D/alerts.env"
  sed -i '/^SITE_MODE/d' "$D/.env"
  [ -z "$1" ] || echo "SITE_MODE=$1" >> "$D/.env"
  [ -z "$2" ] || echo "SITE_MODE_SINCE=$(days_ago "$2")" >> "$D/.env"
  reset
  run "$D/bin/check.sh" "$D" --test "test message"
  WHO=$(sed -n 's/^chat=\([^ ]*\) .*/\1/p' "$FAKE_TELEGRAM" | paste -sd' ')
}
title "check.sh: who it writes to before and after the opening"
recipients development 40; equal "development: only BEFORE" 999 "$WHO"
recipients maintenance 40; equal "maintenance: only BEFORE" 999 "$WHO"
recipients '' ''; equal "without SITE_MODE: only BEFORE" 999 "$WHO"
recipients production 3; equal "production for 3 days: still BEFORE" 999 "$WHO"
recipients production 6; equal "production for 6 days: still BEFORE" 999 "$WHO"
recipients production 8; equal "production for 8 days: all the others" "111 222" "$WHO"
recipients production 40; equal "production for 40 days: all the others" "111 222" "$WHO"
recipients production 3 2; equal "OPENING_DAYS=2, production for 3 days" "111 222" "$WHO"
recipients production 10 30; equal "OPENING_DAYS=30, production for 10 days" 999 "$WHO"
recipients production ''; equal "production without a date: already open" "111 222" "$WHO"
recipients development 40; equal "development for 40 days, the date does not count" 999 "$WHO"
contains "the test message arrives as it is" "$(cat "$FAKE_TELEGRAM")" "text=test message"
healthy nobefore
printf 'TELEGRAM_BOT_TOKEN=123456:FAKE\nTELEGRAM_CHAT_IDS="111 222"\n' > "$D/alerts.env"
run "$D/bin/check.sh" "$D" --test "hello"
equal "without BEFORE, in development: CHAT_IDS" "111 222" "$(sed -n 's/^chat=\([^ ]*\) .*/\1/p' "$FAKE_TELEGRAM" | paste -sd' ')"
printf 'TELEGRAM_BOT_TOKEN=123456:FAKE\nTELEGRAM_CHAT_IDS_BEFORE=999\n' > "$D/alerts.env"
reset
run "$D/bin/check.sh" "$D" --test "hello"
equal "without CHAT_IDS, after the opening: nobody" 999 "$(sed -n 's/^chat=\([^ ]*\) .*/\1/p' "$FAKE_TELEGRAM" | paste -sd' ')"
echo "SITE_MODE=production" > "$D/.env"; echo "SITE_MODE_SINCE=$(days_ago 40)" >> "$D/.env"
reset
run "$D/bin/check.sh" "$D" --test "hello"
equal "without CHAT_IDS and after the opening: does not write to BEFORE" 1 "$EXIT_CODE"
contains "and says so" "$OUT" "Telegram not configured"
run "$D/bin/check.sh" "$D" --test
equal "--test without text exits with an error" 1 "$([ "$EXIT_CODE" -ne 0 ] && echo 1 || echo 0)"

title "check.sh: alerts.env is read line by line, without executing it"
healthy alerts_env
# real shell lines, quoted values, CRLF, names that are not its own, and a last line without a newline
printf '%s\n' \
  '# comment: TELEGRAM_CHAT_IDS=666' \
  "TELEGRAM_BOT_TOKEN=\$(touch \"$M/ran1\")" \
  "touch $M/ran2" \
  "PATH=/does/not/exist" "PROJECT=wrong" "SITE=wrong.example.org" \
  "TELEGRAM_CHAT_IDS=1 2 ; touch $M/ran3" > "$D/alerts.env"
{
  printf 'TELEGRAM_BOT_TOKEN="654321:ALT=RO"\r\n'
  printf 'TELEGRAM_CHAT_IDS_BEFORE=\r\n'
  printf "TELEGRAM_CHAT_IDS='31 32'"
} >> "$D/alerts.env"
run "$D/bin/check.sh" "$D" --test "hello"
lacks "no command executed" bash -c "ls '$M'/ran*"
equal "the last line counts even without a newline, with the quotes removed" "31 32" "$(sed -n 's/^chat=\([^ ]*\) .*/\1/p' "$FAKE_TELEGRAM" | paste -sd' ')"
contains "the last value wins, without quotes or CR, and an = in the value stays" "$(head -1 "$FAKE_TELEGRAM")" "token=654321:ALT=RO text"
echo "exited Exited (1) 2 minutes ago" > "$F/container/web"
check_now
contains "SITE stays that of release.env" "$(cat "$FAKE_TELEGRAM")" "⚠️ test.example.org (machine)"
lacks "names that are not its own do not pass" grep -q wrong "$FAKE_ARGS" "$FAKE_TELEGRAM"
printf 'TELEGRAM_BOT_TOKEN=abc\nTELEGRAM_CHAT_IDS=77' > "$D/alerts.env"
reset
run "$D/bin/check.sh" "$D" --test "hello"
equal "a single line, without a newline" 77 "$(sed -n 's/^chat=\([^ ]*\) .*/\1/p' "$FAKE_TELEGRAM")"

# --- mode.sh ---------------------------------------------------------------------------------------------------------

installed() {  # a machine with version v2026.01.01 already installed, by digest
  machine "$1"
  echo v2026.01.01 > "$D/state/version"
  mkdir -p "$D/current"
  knows v2026.01.01 "$(dg 1)"; knows v2026.01.01 "$(dg 2)"
  printf 'WEB_IMAGE=%s@%s\nCADDY_IMAGE=%s@%s\n' "$WEB_R" "$(dg 1)" "$CADDY_R" "$(dg 2)" > "$D/current/images"
}
mode() { run "$D/bin/mode.sh" "$D" "$@"; }
value() { grep -m1 "^$1=" "$D/.env" | cut -d= -f2; }

title "mode.sh: invalid mode"
installed mode
cp "$D/.env" "$M/env.before"
mode invalid
equal "exits with 2" 2 "$EXIT_CODE"
contains "says which ones there are" "$OUT" "mode: development, maintenance or production"
equal ".env intact" "$(cat "$M/env.before")" "$(cat "$D/.env")"
equal "no call" 0 "$(wc -c < "$FAKE_CALLS")"
mode
lacks "without a mode it exits with an error" test "$EXIT_CODE" -eq 0

title "mode.sh: change of mode"
today=$(date -u +%F)
mode maintenance
tomorrow=$(date -u +%F)
equal "exits with 0" 0 "$EXIT_CODE"
equal "says the mode" "SITE_MODE=maintenance" "$OUT"
equal "SITE_MODE" maintenance "$(value SITE_MODE)"
check "SITE_MODE_SINCE is today" test "$(value SITE_MODE_SINCE)" = "$today" -o "$(value SITE_MODE_SINCE)" = "$tomorrow"
check "the rest of the .env does not change" grep -qx 'SECRET_KEY=fake/with&characters' "$D/.env"
equal "a single line for the mode" 1 "$(count '^SITE_MODE=' "$D/.env")"
check "Caddy tested with the installed version" grep -qE '^compose v2026.01.01 run --rm --no-deps -T caddy caddy validate' "$FAKE_CALLS"
check "Django tested" grep -qE '^compose v2026.01.01 run --rm --no-deps -T web python manage.py check' "$FAKE_CALLS"
equal "then recreates site and Caddy, after the checks" "validate check up" \
  "$(grep -E 'validate|manage.py check|up -d' "$FAKE_CALLS" | sed -E 's/.*(validate).*/validate/; s/.*(check)$/check/; s/.* (up) .*/up/' | paste -sd' ')"
check "up without build" grep -qx 'compose v2026.01.01 up -d --no-build web caddy' "$FAKE_CALLS"
check "state/modes" grep -Eq '^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9:]{8}Z maintenance$' "$D/state/modes"

title "mode.sh: the date changes only when the mode changes"
sed -i 's/^SITE_MODE_SINCE=.*/SITE_MODE_SINCE=2026-02-02/' "$D/.env"
reset
mode maintenance
equal "exits with 0" 0 "$EXIT_CODE"
equal "same mode: same date" 2026-02-02 "$(value SITE_MODE_SINCE)"
check "but the site is recreated all the same" grep -q ' up -d ' "$FAKE_CALLS"
mode development
equal "different mode: new date" 1 "$([ "$(value SITE_MODE_SINCE)" != 2026-02-02 ] && echo 1)"
sed -i 's/^SITE_MODE_SINCE=.*/SITE_MODE_SINCE=2026-02-02/' "$D/.env"
sed -i 's/^NETWORK=.*/NETWORK=mainnet/' "$D/.env"
mode production
equal "production on mainnet starts" 0 "$EXIT_CODE"
equal "SITE_MODE" production "$(value SITE_MODE)"
equal "the date is renewed" 1 "$([ "$(value SITE_MODE_SINCE)" != 2026-02-02 ] && echo 1)"

title "mode.sh: the new mode does not start, the previous values come back"
sed -i 's/^NETWORK=.*/NETWORK=signet/; s/^SITE_MODE=.*/SITE_MODE=maintenance/; s/^SITE_MODE_SINCE=.*/SITE_MODE_SINCE=2026-02-02/' "$D/.env"
cp "$D/.env" "$M/env.before"
lines=$(wc -l < "$D/state/modes")
reset
mode production
equal "exits with 1" 1 "$EXIT_CODE"
contains "says why" "$OUT" "SITE_MODE production needs NETWORK=mainnet"
contains "and that the previous one stays" "$OUT" "production does not start: staying on maintenance"
equal ".env as it was" "$(cat "$M/env.before")" "$(cat "$D/.env")"
lacks "the site is not touched" grep -q ' up ' "$FAKE_CALLS"
equal "state/modes does not grow" "$lines" "$(wc -l < "$D/state/modes")"
contains "the log of the checks stays in state/mode.log" "$(cat "$D/state/mode.log")" "ImproperlyConfigured"

title "mode.sh: the Caddy rules are not valid, the previous values come back"
touch "$F/caddy_ko"
reset
mode development
equal "exits with 1" 1 "$EXIT_CODE"
contains "says so" "$OUT" "development does not start: staying on maintenance"
contains "with the Caddy error" "$OUT" "adapting config"
equal ".env as it was" "$(cat "$M/env.before")" "$(cat "$D/.env")"
lacks "Django is not tested if Caddy is already broken" grep -q 'manage.py check' "$FAKE_CALLS"
lacks "the site is not touched" grep -q ' up ' "$FAKE_CALLS"

title "mode.sh: .env without a mode"
installed nomode
echo 'SECRET_KEY=fake' > "$D/.env"
mode maintenance
equal "exits with 0" 0 "$EXIT_CODE"
equal "SITE_MODE added" maintenance "$(value SITE_MODE)"
check "SITE_MODE_SINCE added" test -n "$(value SITE_MODE_SINCE)"
installed nomode2
echo 'SECRET_KEY=fake' > "$D/.env"
mode production
equal "if it does not start it exits with 1" 1 "$EXIT_CODE"
contains "and says the default mode stays" "$OUT" "staying on development"
equal "SITE_MODE empty, that is the default" "" "$(value SITE_MODE)"
equal "SITE_MODE_SINCE empty" "" "$(value SITE_MODE_SINCE)"

title "mode.sh: waits for the release in progress"
installed modelock
( exec 8>"$D/state/.lock"; flock 8; exec sleep 300 ) &
PID_LOCK=$!
for _ in $(seq 1 50); do flock -n "$D/state/.lock" true 2>/dev/null || break; sleep 0.1; done
( PATH="$HERE/fakes:$PATH" "$D/bin/mode.sh" "$D" maintenance > "$M/mode.out" 2>&1; echo $? > "$M/mode.output" ) &
PID_MODE=$!
sleep 1
lacks "with the lock taken nothing changes" test -e "$M/mode.output"
equal ".env intact" development "$(value SITE_MODE)"
kill "$PID_LOCK"; wait "$PID_LOCK" 2>/dev/null; PID_LOCK=
wait "$PID_MODE"
equal "lock released, it changes" maintenance "$(value SITE_MODE)"

# --- shellcheck ------------------------------------------------------------------------------------------------------

title "shellcheck"
if command -v shellcheck >/dev/null; then
  # SC1091: release.env is not followed; SC2010 and SC2012: folder and dump names are versions and dates, never odd names;
  # SC2016: the actions of problem() are text that eval runs, with $F and $D in their place
  if OUT=$(shellcheck -e SC1091,SC2010,SC2012,SC2016 "$DEPLOY_DIR"/*.sh "$HERE/test_release.sh" "$HERE"/fakes/* 2>&1); then
    PASSED=$((PASSED + 1))
  else
    ko "shellcheck"
  fi
else
  echo "  (shellcheck is not there: skipped)"
fi

echo
echo "passed: $PASSED, failed: $FAILED"
[ "$FAILED" -eq 0 ]
