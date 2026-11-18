#!/usr/bin/env bash
# Tests deploy/install.sh in temporary folders, without docker: a fake docker answers with the volumes the scenario
# wants. Usage: bash deploy/tests/test_install.sh        exits with 0 if everything passes
set -uo pipefail
HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
INSTALL=$(dirname "$HERE")/install.sh
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT
PASSED=0 FAILED=0
mkdir -p "$TMP/bin"
cat > "$TMP/bin/docker" <<'EOF'
#!/bin/sh
# volume ls -q --filter name=^<project>_: the volumes written in $FAKE_VOLUMES
[ "$1 $2" = "volume ls" ] && { printf '%s\n' ${FAKE_VOLUMES:-}; exit 0; }
# network inspect bitmentor-entry: the subnet in $FAKE_ENTRY, or no such network
[ "$1 $2" = "network inspect" ] && { [ -n "${FAKE_ENTRY:-}" ] || exit 1; echo "$FAKE_ENTRY "; exit 0; }
# info --format {{.Plugins.Log}}: the log drivers in $FAKE_LOG_DRIVERS, or a Docker that does not answer
[ "$1" = "info" ] && { [ -n "${FAKE_LOG_DRIVERS:-}" ] || exit 1; echo "[$FAKE_LOG_DRIVERS]"; exit 0; }
exit 1
EOF
chmod 755 "$TMP/bin/docker"
mkdir -p "$TMP/systemd"
export PATH="$TMP/bin:$PATH" FAKE_VOLUMES= FAKE_ENTRY= FAKE_LOG_DRIVERS= INSTALL_SYSTEMD=$TMP/systemd INSTALL_JOURNAL=$TMP/no-journal

no() { FAILED=$((FAILED + 1)); echo "  NO: $1"; printf '%s\n' "${OUT:-}" | tail -n 8 | sed 's/^/        | /'; }
yes() { PASSED=$((PASSED + 1)); }
holds() { local what=$1; shift; if "$@" >/dev/null 2>&1; then yes; else no "$what"; fi; }
fails() { local what=$1; shift; if "$@" >/dev/null 2>&1; then no "$what"; else yes; fi; }
has() { if [[ $2 == *"$3"* ]]; then yes; else no "$1: «$3» missing"; fi; }
lacks() { if [[ $2 == *"$3"* ]]; then no "$1: «$3» found"; else yes; fi; }
value() { grep -E "^$2=" "$1" | tail -n 1 | cut -d= -f2-; }

run() {  # run <folder> <options...>: OUT and CODE of install.sh, answering nothing
  OUT=$(bash "$INSTALL" "$@" --yes </dev/null 2>&1); CODE=$?
}
common=(--web-image registry.example.org/community/web --caddy-image registry.example.org/community/caddy)

echo "== a new signet installation"
D=$TMP/opt/shops-test
run "$D" --site shops.example.org --network signet "${common[@]}"
holds "it ends well" test "$CODE" = 0
holds ".env readable by its owner only" test "$(stat -c %a "$D/.env")" = 600
for name in SECRET_KEY POSTGRES_PASSWORD WEB_DB_PASSWORD; do
  secret=$(value "$D/.env" "$name")
  holds "$name of 64 hex digits" grep -qxE '[0-9a-f]{64}' <<<"$secret"
  lacks "$name never printed" "$OUT" "$secret"
done
holds "three different secrets" test "$(for n in SECRET_KEY POSTGRES_PASSWORD WEB_DB_PASSWORD; do value "$D/.env" $n; done | sort -u | wc -l)" = 3
holds "NETWORK" test "$(value "$D/.env" NETWORK)" = signet
holds "SITE_HOST" test "$(value "$D/.env" SITE_HOST)" = shops.example.org
holds "development" test "$(value "$D/.env" SITE_MODE)" = development
holds "release.env" test "$(cat "$D/release.env")" = "$(printf 'PROJECT=shops-test\nSITE=shops.example.org\nWEB_IMAGE_REPO=registry.example.org/community/web\nCADDY_IMAGE_REPO=registry.example.org/community/caddy\nTAG=active')"
holds "direct: no override" grep -qx 'services: {}' "$D/compose.locale.yml"
fails "direct: no shared entry point" grep -qE '^[^#]*(entry|caddy:)' "$D/compose.locale.yml"
holds "the scripts" test -x "$D/bin/release.sh" -a -x "$D/bin/mode.sh" -a -x "$D/bin/check.sh" -a -x "$D/bin/compose.sh"
has "what is left: DNS" "$OUT" "shops.example.org points to this machine"
has "what is left: administrator" "$OUT" "createsuperuser"
has "what is left: the check" "$OUT" "manage.py check"
has "what is left: start" "$OUT" "bitmentor-release@shops-test.timer"
has "with the guardian" "$OUT" "bitmentor-sentinel@shops-test.timer"
has "and the daily summary" "$OUT" "bitmentor-dashboard@shops-test.timer"
has "the summary's unit is copied" "$OUT" "deploy/systemd/bitmentor-dashboard@.timer"
has "units copied only when missing" "$OUT" "cp -n "
holds "direct: HTTPS" test "$(value "$D/.env" SITE_SCHEME)" = https
holds "direct: no proxy to trust" test -z "$(value "$D/.env" TRUSTED_PROXIES)"
if command -v python3 >/dev/null && python3 -c 'import yaml' 2>/dev/null; then
  holds "valid YAML" python3 -c 'import sys, yaml; yaml.compose(open(sys.argv[1]))' "$D/compose.locale.yml"
fi

echo "== run again: nothing changes"
before=$(cd "$D" && sha256sum .env release.env compose.locale.yml bin/*)
run "$D" "${common[@]}"
holds "it ends well" test "$CODE" = 0
has "it says so" "$OUT" "Nothing changed"
holds "the same files" test "$before" = "$(cd "$D" && sha256sum .env release.env compose.locale.yml bin/*)"
run "$D" --network mainnet "${common[@]}"
holds "another network is refused" test "$CODE" = 2
has "and named" "$OUT" "NETWORK=signet, not mainnet"
holds "the .env untouched" test "$before" = "$(cd "$D" && sha256sum .env release.env compose.locale.yml bin/*)"
echo "2026.11.04" > "$D/state/version"
run "$D" "${common[@]}"
lacks "started: not asked again" "$OUT" "systemctl enable"

echo "== mainnet next to it, behind the shared entry point"
M=$TMP/opt/shops-main
run "$M" --site shops2.example.org --network mainnet --entry shared "${common[@]}"
holds "without the entry point's network: refused" test "$CODE" = 2
has "named" "$OUT" "there is no Docker network bitmentor-entry"
fails "and nothing written" test -e "$M"
export FAKE_ENTRY=172.30.0.0/16
touch "$TMP/systemd/bitmentor-release@.service"
run "$M" --site shops2.example.org --network mainnet --entry shared "${common[@]}"
holds "shared: HTTP behind the entry point that holds TLS" test "$(value "$M/.env" SITE_SCHEME)" = http
holds "shared: the entry point's network trusted" test "$(value "$M/.env" TRUSTED_PROXIES)" = 172.30.0.0/16
lacks "a unit already there is not copied again" "$OUT" "deploy/systemd/bitmentor-release@.service "
holds "it ends well" test "$CODE" = 0
holds "alias of its own" grep -q 'aliases: \[caddy-shops-main\]' "$M/compose.locale.yml"
holds "Caddy without ports" grep -q 'ports: !reset \[\]' "$M/compose.locale.yml"
fails "no comment markers left" grep -q '#@shared' "$M/compose.locale.yml"
holds "secrets of its own" test "$(value "$M/.env" SECRET_KEY)" != "$(value "$D/.env" SECRET_KEY)"
has "the mainnet steps" "$OUT" "docs/mainnet.md"
run "$M" "${common[@]}"
has "run again, it knows the entry point" "$OUT" "forwards the name to caddy-shops-main"
sed -i 's|^SITE_SCHEME=.*|SITE_SCHEME=https|' "$M/.env"
run "$M" "${common[@]}"
holds "shared with HTTPS written by hand: stops" test "$CODE" = 1
has "named" "$OUT" "needs SITE_SCHEME=http"
sed -i 's|^SITE_SCHEME=.*|SITE_SCHEME=http|' "$M/.env"
if command -v python3 >/dev/null && python3 -c 'import yaml' 2>/dev/null; then
  holds "valid YAML" python3 -c 'import sys, yaml; yaml.compose(open(sys.argv[1]))' "$M/compose.locale.yml"
fi

echo "== where the containers' output goes"
services=$(sed -nE '/^services:$/,$ s/^  ([a-z-]+):$/\1/p' "$(dirname "$(dirname "$INSTALL")")/docker-compose.yml" | sort | paste -sd' ')
journal='logging: !override {driver: journald, options: {tag: "{{.Name}}"}}'
L=$TMP/opt/logs-journal
run "$L" --site j.example.org --network signet "${common[@]}" --logs journal
holds "journal: it ends well" test "$CODE" = 0
holds "journal: every service of docker-compose.yml ($services)" test \
  "$(sed -nE 's/^  ([a-z-]+):$/\1/p' "$L/compose.locale.yml" | sort | paste -sd' ')" = "$services"
holds "journal: one logging line for each" test \
  "$(grep -cF "$journal" "$L/compose.locale.yml")" = "$(wc -w <<<"$services")"
fails "journal, direct: Caddy keeps its ports" grep -q 'ports:' "$L/compose.locale.yml"
fails "journal: no comment markers left" grep -q '#@' "$L/compose.locale.yml"
has "journal: the closing lines say where to read" "$OUT" "journalctl CONTAINER_NAME=logs-journal-web-1"
L=$TMP/opt/logs-both
FAKE_ENTRY=172.30.0.0/16 run "$L" --site b.example.org --network signet "${common[@]}" --logs journal --entry shared
holds "journal and shared: it ends well" test "$CODE" = 0
holds "journal and shared: one caddy key" test "$(grep -c '^  caddy:$' "$L/compose.locale.yml")" = 1
holds "journal and shared: Caddy without ports" grep -q 'ports: !reset \[\]' "$L/compose.locale.yml"
holds "journal and shared: one logging line for each service" test \
  "$(grep -cF "$journal" "$L/compose.locale.yml")" = "$(wc -w <<<"$services")"
fails "journal and shared: no comment markers left" grep -q '#@' "$L/compose.locale.yml"
L=$TMP/opt/logs-files
mkdir -p "$TMP/journal"
INSTALL_JOURNAL=$TMP/journal FAKE_LOG_DRIVERS="journald json-file" run "$L" --site f.example.org --network signet "${common[@]}" --logs files
holds "files, asked for on a machine with the journal: no override" grep -qx 'services: {}' "$L/compose.locale.yml"
lacks "files: nothing about the journal at the end" "$OUT" "journalctl CONTAINER_NAME"
L=$TMP/opt/logs-auto
INSTALL_JOURNAL=$TMP/journal FAKE_LOG_DRIVERS="awslogs journald json-file" run "$L" --site a.example.org --network signet "${common[@]}"
holds "not said, journal and driver here: journal" grep -qF "$journal" "$L/compose.locale.yml"
L=$TMP/opt/logs-nodriver
INSTALL_JOURNAL=$TMP/journal FAKE_LOG_DRIVERS="json-file local" run "$L" --site n.example.org --network signet "${common[@]}"
holds "not said, a Docker without the driver: files" grep -qx 'services: {}' "$L/compose.locale.yml"
L=$TMP/opt/logs-nojournal
FAKE_LOG_DRIVERS="journald json-file" run "$L" --site o.example.org --network signet "${common[@]}"
holds "not said, a machine without the journal: files" grep -qx 'services: {}' "$L/compose.locale.yml"
if python3 -c 'import yaml' 2>/dev/null; then
  for folder in logs-journal logs-both; do
    holds "$folder: valid YAML" python3 -c 'import sys, yaml; yaml.compose(open(sys.argv[1]))' "$TMP/opt/$folder/compose.locale.yml"
  done
fi
run "$TMP/opt/logs-wrong" --site w.example.org --network signet "${common[@]}" --logs syslog
holds "another word is refused" test "$CODE" = 2
fails "and nothing is written" test -e "$TMP/opt/logs-wrong"

echo "== what is refused"
run "$TMP/opt/again" --site shops.example.org --network signet "${common[@]}"
holds "the same site twice" test "$CODE" = 2
has "named" "$OUT" "already serves shops.example.org"
fails "no folder left" test -e "$TMP/opt/again"
FAKE_VOLUMES="old_postgres_datadir old_bitcoin_datadir" run "$TMP/opt/old" --site old.example.org --network mainnet "${common[@]}"
holds "volumes of a project without .env" test "$CODE" = 2
has "named" "$OUT" "already has volumes of a project called old"
fails "and nothing written" test -f "$TMP/opt/old/.env"
for bad in "--tag a;reboot" "--tag \$(id)" "--site Shops.Example.org" "--site shops" "--network testnet" "--entry proxy" "--web-image registry.example.org/x:v1"; do
  # shellcheck disable=SC2086
  run "$TMP/opt/bad" --site bad.example.org --network signet "${common[@]}" $bad
  holds "refused: $bad" test "$CODE" = 2
  rm -rf "$TMP/opt/bad"
done
run "$TMP/opt/Bad_Name" --site b.example.org --network signet "${common[@]}"
holds "a project name Docker would change" test "$CODE" = 2
run "$TMP/opt/missing" --network signet "${common[@]}"
holds "--yes without the site" test "$CODE" = 2
has "says what is missing" "$OUT" "missing: Site name"

echo "== sources, not images"
mkdir -p "$TMP/opt/sources" && echo "NETWORK=signet" > "$TMP/opt/sources/.env"
run "$TMP/opt/sources" --site src.example.org --network signet "${common[@]}"
holds "a .env without release.env is refused" test "$CODE" = 2
fails "and left alone" test -e "$TMP/opt/sources/release.env"

echo "== help"
OUT=$(bash "$INSTALL" --help)
lacks "only the comment" "$OUT" "set -euo"
has "the options" "$OUT" "--entry"

echo "== every timer has its unit, every unit its script, and the start enables every periodic one"
UNITS=$(dirname "$INSTALL")/systemd
D=$TMP/opt/units-check
run "$D" --site units.example.org --network signet "${common[@]}"
for timer in "$UNITS"/bitmentor-*@.timer; do
  name=$(basename "$timer" .timer)
  holds "$name: its service" test -f "$UNITS/$name.service"
  script=$(sed -nE 's,^ExecStart=/opt/%i/(current/deploy|bin)/([a-z-]+\.sh) .*,\2,p' "$UNITS/$name.service")
  holds "$name: its script ($script)" test -n "$script" -a -x "$(dirname "$INSTALL")/$script"
  # The backup waits for backup.env: the others start with the installation
  [ "$name" = bitmentor-backup@ ] || has "$name: enabled at the start" "$OUT" "${name}units-check.timer"
done
holds "the morning summary counts its exit 10 as a success" grep -qx 'SuccessExitStatus=10' "$UNITS/bitmentor-dashboard@.service"

echo
echo "passed $PASSED, failed $FAILED"
[ "$FAILED" = 0 ]
