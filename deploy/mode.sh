#!/usr/bin/env bash
# Changes who sees the site: mode.sh <directory> <development|maintenance|production>.
# Writes SITE_MODE in the .env, and SITE_MODE_SINCE if the mode changes; checks Caddy and Django with the installed
# version and recreates the site and Caddy. If the new mode does not start (production needs NETWORK=mainnet), it puts
# the previous one back. The CI and release.sh never change it.
set -euo pipefail
DIR=${1:?missing folder}
MODE=${2:?missing mode}
case "$MODE" in development|maintenance|production) ;; *) echo "mode: development, maintenance or production"; exit 2 ;; esac
cd "$DIR"
DIR=$PWD  # docker compose gets paths that do not depend on where the script is run from
. ./release.env
exec 9>state/.lock
flock 9  # if a release is running, wait for it to end
version=$(cat state/version)

ipv6_on() {  # IPv6 on (compose.ipv6.yml): the last IPV6_SUBNET of the .env, with a value that starts like an address
  local value
  value=$(grep -E '^IPV6_SUBNET=' "$DIR/.env" | tail -n 1 | cut -d= -f2- || true)
  value=${value#[\"\']}
  [[ $value =~ ^[0-9a-fA-F] ]]
}

env_says() {  # env_says <name> <value>: some line of the .env gives <name> that value (export, spaces and quotes allowed)
  sed -nE "s/^[[:space:]]*(export[[:space:]]+)?$1[[:space:]]*=[[:space:]]*//p" "$DIR/.env" |
    sed -E "s/[[:space:]]+#.*$//; s/[[:space:]]+$//; s/^[\"']//; s/[\"']$//" | grep -qxF "$2"
}

strict() {  # production or mainnet: only images pinned by digest
  env_says SITE_MODE production || env_says NETWORK mainnet
}

if { [ "$MODE" = production ] || strict; } && [ ! -f current/images ]; then
  echo "refused: $version was installed by tag, not by digest: production and mainnet run only images pinned by digest"
  exit 1
fi

image_of() {  # image_of <WEB|CADDY> <repo>: the image of the installed version, by digest if release.sh pulled it so
  local line
  line=$(grep -m1 "^$1_IMAGE=" current/images 2>/dev/null || true)
  if [ -n "$line" ]; then echo "${line#*=}"; else echo "$2:$version"; fi
}

compose() {  # the same files and images as release.sh
  local file=(-f current/docker-compose.yml -f current/compose.release.yml)
  [ -f current/compose.community.yml ] && file+=(-f current/compose.community.yml)
  [ -f current/compose.ipv6.yml ] && ipv6_on && file+=(-f current/compose.ipv6.yml)
  [ -f compose.locale.yml ] && file+=(-f compose.locale.yml)
  WEB_IMAGE=$(image_of WEB "$WEB_IMAGE_REPO") CADDY_IMAGE=$(image_of CADDY "$CADDY_IMAGE_REPO") \
    docker compose -p "$PROJECT" --project-directory "$DIR/current" --env-file "$DIR/.env" "${file[@]}" "$@"
}

set_env() {  # set_env <name> <value> in the .env
  if grep -q "^$1=" .env; then sed -i "s/^$1=.*/$1=$2/" .env; else echo "$1=$2" >> .env; fi
}
before=$(grep -m1 '^SITE_MODE=' .env | cut -d= -f2 || true)
since=$(grep -m1 '^SITE_MODE_SINCE=' .env | cut -d= -f2 || true)
set_env SITE_MODE "$MODE"
# the date changes only with the mode: /health/ shows it, and the CI uses it for the alerts that start after the opening
[ "$MODE" = "$before" ] || set_env SITE_MODE_SINCE "$(date -u +%F)"
if ! compose run --rm --no-deps -T caddy caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile > state/mode.log 2>&1 ||
   ! compose run --rm --no-deps -T web python manage.py check >> state/mode.log 2>&1; then
  grep -v '^$' state/mode.log | tail -3
  set_env SITE_MODE "$before"
  set_env SITE_MODE_SINCE "$since"
  echo "$MODE does not start: staying on ${before:-development}"
  exit 1
fi
compose up -d --no-build web caddy
echo "$(date -u +%FT%TZ) $MODE" >> state/modes
echo "SITE_MODE=$MODE"
