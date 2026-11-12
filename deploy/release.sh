#!/usr/bin/env bash
# Installs the latest published version. bitmentor-release@<project>.timer runs it every minute.
# Looks at the TAG tag (default: active) of WEB_IMAGE_REPO in the registry: if it points to a new version, it pulls the
# images, copies the compose files from the release, saves the site database, updates, and checks that the site
# answers. If the site does not answer, it goes back to the previous version and writes the version to state/failed,
# which check.sh reports.
# With WEB_DIGEST and CADDY_DIGEST in release.env (sha256:<64 hex>) it pulls the images by digest (repo@sha256:...), checks
# that what it got is that digest and that the image's version label is TAG (unless TAG is active), and runs exactly those
# images, also when it rolls back. The tag stays for people: logs, labels, /health/. Without digests it pulls by tag: it
# refuses that when the .env says SITE_MODE=production or NETWORK=mainnet, and warns elsewhere (docs/releases.md).
# It does not touch the site mode (SITE_MODE in the .env): the person who runs the machine decides it, with mode.sh.
# Usage: release.sh <directory>, which contains .env, release.env and, if needed, compose.locale.yml.
set -euo pipefail
DIR=${1:?missing folder}
cd "$DIR"
DIR=$PWD  # docker compose gets paths that do not depend on where the script is run from
. ./release.env
: "${PROJECT:?}" "${WEB_IMAGE_REPO:?}" "${CADDY_IMAGE_REPO:?}" "${TAG:=active}"
: "${COMMUNITY_COMPOSE:=/community/deploy/compose.yml}" "${BACKUP_KEEP:=10}" "${WEB_DIGEST:=}" "${CADDY_DIGEST:=}"
DIGEST_RE='^sha256:[0-9a-f]{64}$'
VERSION_RE='^v[0-9]{4}\.[0-9]{2}\.[0-9]{2}(-[0-9]+)?$'  # the same as the CI tags
umask 077
mkdir -p state releases backup bin
exec 9>state/.lock
flock -n 9 || exit 0

ipv6_on() {  # IPv6 on (compose.ipv6.yml): the last IPV6_SUBNET of the .env, with a value that starts like an address
  local value
  value=$(grep -E '^IPV6_SUBNET=' "$DIR/.env" | tail -n 1 | cut -d= -f2- || true)
  value=${value#[\"\']}
  [[ $value =~ ^[0-9a-fA-F] ]]
}

refuse() { echo "refused: $*"; exit 1; }

env_says() {  # env_says <name> <value>: some line of the .env gives <name> that value (export, spaces and quotes allowed)
  sed -nE "s/^[[:space:]]*(export[[:space:]]+)?$1[[:space:]]*=[[:space:]]*//p" "$DIR/.env" |
    sed -E "s/[[:space:]]+#.*$//; s/[[:space:]]+$//; s/^[\"']//; s/[\"']$//" | grep -qxF "$2"
}

strict() {  # production or mainnet: only images pinned by digest
  env_says SITE_MODE production || env_says NETWORK mainnet
}

if [ -n "$WEB_DIGEST$CADDY_DIGEST" ]; then
  [[ $WEB_DIGEST =~ $DIGEST_RE ]] || refuse "WEB_DIGEST is not sha256:<64 hex>"
  [[ $CADDY_DIGEST =~ $DIGEST_RE ]] || refuse "CADDY_DIGEST is not sha256:<64 hex>"
  PINNED=1 WEB_REF=$WEB_IMAGE_REPO@$WEB_DIGEST
else
  strict && refuse "SITE_MODE=production or NETWORK=mainnet: release.env needs WEB_DIGEST and CADDY_DIGEST"
  PINNED='' WEB_REF=$WEB_IMAGE_REPO:$TAG
fi

has_digest() {  # has_digest <repo> <digest>: the local image <repo>@<digest> is really that digest
  docker image inspect -f '{{join .RepoDigests "\n"}}' "$1@$2" 2>/dev/null | grep -qxF "$1@$2"
}

image_of() {  # image_of <version> <web|caddy>: the image a version runs with, by digest if it was installed by digest
  local version=$1 which=$2 repo line
  if [ "$which" = web ]; then repo=$WEB_IMAGE_REPO; else repo=$CADDY_IMAGE_REPO; fi
  line=$(grep -m1 "^${which^^}_IMAGE=" "$DIR/releases/$version/images" 2>/dev/null || true)
  if [ -n "$line" ]; then echo "${line#*=}"; else echo "$repo:$version"; fi
}

compose() {  # compose <version> <docker compose arguments>: always the files of current/, with the images of <version>
  local version=$1; shift
  local file=(-f current/docker-compose.yml -f current/compose.release.yml)
  [ -f current/compose.community.yml ] && file+=(-f current/compose.community.yml)
  # IPv6 on the Docker network (compose.ipv6.yml), only when the .env names its subnet
  [ -f current/compose.ipv6.yml ] && ipv6_on && file+=(-f current/compose.ipv6.yml)
  [ -f compose.locale.yml ] && file+=(-f compose.locale.yml)
  WEB_IMAGE=$(image_of "$version" web) CADDY_IMAGE=$(image_of "$version" caddy) \
    docker compose -p "$PROJECT" --project-directory "$DIR/current" --env-file "$DIR/.env" "${file[@]}" "$@"
}

prepare() {  # pulls the images and copies into releases/<version> the files compose needs
  local version=$1 id web caddy
  if [ -n "$PINNED" ]; then
    web=$WEB_REF caddy=$CADDY_IMAGE_REPO@$CADDY_DIGEST
    docker pull -q "$caddy" >/dev/null
    has_digest "$CADDY_IMAGE_REPO" "$CADDY_DIGEST" || refuse "the caddy image is not $CADDY_DIGEST"
    # the version tag, only local, so that people and the cleanup below see it; compose runs the digests
    docker tag "$web" "$WEB_IMAGE_REPO:$version" && docker tag "$caddy" "$CADDY_IMAGE_REPO:$version"
  else
    web=$WEB_IMAGE_REPO:$version caddy=$CADDY_IMAGE_REPO:$version
    docker pull -q "$web" >/dev/null
    docker pull -q "$caddy" >/dev/null
  fi
  rm -rf "releases/$version.tmp" && mkdir -p "releases/$version.tmp"
  # only for images pinned by digest: a version without this file was installed by tag
  [ -z "$PINNED" ] || printf 'WEB_IMAGE=%s\nCADDY_IMAGE=%s\n' "$web" "$caddy" > "releases/$version.tmp/images"
  id=$(docker create "$web")
  for file in docker-compose.yml compose.release.yml postgres-init.sh tor.sh deploy; do
    docker cp "$id:/app/$file" "releases/$version.tmp/$file" >/dev/null || { docker rm "$id" >/dev/null; return 1; }
  done
  docker cp "$id:$COMMUNITY_COMPOSE" "releases/$version.tmp/compose.community.yml" >/dev/null 2>&1 || true
  docker cp "$id:/app/compose.ipv6.yml" "releases/$version.tmp/compose.ipv6.yml" >/dev/null 2>&1 || true  # from this version on
  docker rm "$id" >/dev/null
  rm -rf "releases/$version" && mv "releases/$version.tmp" "releases/$version"
}

active() {  # current/ always has the same path: services that do not change are not recreated
  local version=$1
  rm -rf current.new && cp -a "releases/$version" current.new
  rm -rf current && mv current.new current
  compose "$version" up -d --no-build --remove-orphans
}

answers() {  # the site answers: /health/ gives 200, or 503 if the problem is one the site reports itself and not the release
  local version=$1
  for _ in $(seq 1 60); do
    if compose "$version" exec -T web python -c "
import urllib.request, urllib.error
try:
    urllib.request.urlopen('http://localhost:8000/health/', timeout=5)
except urllib.error.HTTPError as error:
    raise SystemExit(error.code != 503)" 2>/dev/null && [ -n "$(compose "$version" ps -q --status running caddy)" ]; then
      return 0
    fi
    sleep 5
  done
  return 1
}

save() {  # the site database before the new version's migrations
  local installed=$1 new_version=$2
  [ -n "$installed" ] || return 0
  compose "$installed" exec -T postgres pg_dump -U postgres -Fc bitmentor > "backup/before-$new_version.dump.tmp"
  mv "backup/before-$new_version.dump.tmp" "backup/before-$new_version.dump"
  ls -1t backup/*.dump | tail -n +"$((BACKUP_KEEP + 1))" | xargs -r rm -f || true
}

if ! docker pull -q "$WEB_REF" >/dev/null 2>state/registry.error; then
  exit 0  # registry unreachable: check.sh alerts if it lasts more than an hour
fi
date +%s > state/registry.ok
[ -z "$PINNED" ] || has_digest "$WEB_IMAGE_REPO" "$WEB_DIGEST" || refuse "the web image is not $WEB_DIGEST"
new_version=$(docker image inspect -f '{{index .Config.Labels "org.opencontainers.image.version"}}' "$WEB_REF")
# the label comes from the image: it becomes paths and tags only if it looks like a CI version
[[ $new_version =~ $VERSION_RE ]] || refuse "the image's version label is not vYYYY.MM.DD[-N]: $(printf '%q' "${new_version:0:80}")"
[ -z "$PINNED" ] || [ "$TAG" = active ] || [ "$TAG" = "$new_version" ] || refuse "WEB_DIGEST is version $new_version, not TAG=$TAG"
installed=$(cat state/version 2>/dev/null || true)
if [ -n "$PINNED" ] && [ "$new_version" = "$installed" ] && [ -f "releases/$installed/images" ] &&
  [ "$(image_of "$installed" web)" != "$WEB_REF" ]; then
  refuse "$new_version is already installed with another digest ($(image_of "$installed" web))"
fi
# The version installed by a release.sh older than compose.ipv6.yml has the file in its image but not in current/
if [ -n "$installed" ] && [ -d current ] && [ ! -f current/compose.ipv6.yml ] && ipv6_on; then
  id=$(docker create "$WEB_IMAGE_REPO:$installed" 2>/dev/null) && {
    docker cp "$id:/app/compose.ipv6.yml" current/compose.ipv6.yml >/dev/null 2>&1 &&
      cp current/compose.ipv6.yml "releases/$installed/" 2>/dev/null
    docker rm "$id" >/dev/null 2>&1
  }
  true
fi
[ -n "$new_version" ] && [ "$new_version" != "$installed" ] || exit 0
[ "$(cat state/failed 2>/dev/null || true)" = "$new_version" ] && exit 0  # already tried and rolled back: wait for the next one

echo "new version: $new_version (now: ${installed:-none})"
[ -n "$PINNED" ] || echo "warning: release.env has no WEB_DIGEST and CADDY_DIGEST: images pulled by tag, which can change"
prepare "$new_version"
save "$installed" "$new_version"
if active "$new_version" && answers "$new_version"; then
  [ -n "$installed" ] && echo "$installed" > state/previous
  echo "$new_version" > state/version
  rm -f state/failed
  echo "$(date -u +%FT%TZ) $new_version" >> state/history
  compose "$new_version" ps --services --status running > state/services
  # the scripts travel with the release: they are replaced under a new name, never rewriting the one that is running
  for script in release.sh check.sh mode.sh; do
    cp "releases/$new_version/deploy/$script" "bin/$script.new" && chmod 755 "bin/$script.new" && mv "bin/$script.new" "bin/$script"
  done
  # the images of the new version and of the previous one are kept
  for image in "$WEB_IMAGE_REPO" "$CADDY_IMAGE_REPO"; do
    docker image ls "$image" --format '{{.Tag}}' | grep -vxF -e "$new_version" -e "$installed" -e "$TAG" | sed "s|^|$image:|" | xargs -r docker rmi >/dev/null 2>&1 || true
  done
  ls -1d releases/*/ | grep -vxF -e "releases/$new_version/" -e "releases/$installed/" | xargs -r rm -rf || true
  echo "installed $new_version"
else
  echo "$new_version does not answer"
  echo "$new_version" > state/failed
  if [ -n "$installed" ] && strict && [ ! -f "releases/$installed/images" ]; then
    echo "refused: no rollback to $installed: it was installed by tag, not by digest (production or mainnet)"
  elif [ -n "$installed" ]; then
    if active "$installed" && answers "$installed"; then echo "rolled back to $installed"; else echo "$installed does not answer either"; fi
  fi
  exit 1
fi
