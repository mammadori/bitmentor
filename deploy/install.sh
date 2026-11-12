#!/usr/bin/env bash
# Prepares a new installation from images: the folder that release.sh, mode.sh and check.sh run on, usually
# /opt/<project>. Run it from a clone of the software; guide in docs/installation.md.
#
#   deploy/install.sh /opt/<project> --site <name> --network <mainnet|signet> --web-image <image> --caddy-image <image>
#
# Options (asked on the terminal when missing, required with --yes):
#   --site <name>          the site's name, e.g. shops.example.org
#   --network <name>       mainnet or signet (the public signet)
#   --web-image <image>    the community's site image without the tag, e.g. registry.example.org/community/web
#   --caddy-image <image>  the community's Caddy image without the tag
#   --tag <tag>            the tag the machine follows (default active)
#   --entry <how>          direct: Caddy holds ports 80 and 443 (default); shared: behind the entry point's Caddy on
#                          the Docker network bitmentor-entry, which holds TLS (deploy/examples/entry), as when
#                          two installations share the machine
#   --yes                  ask nothing
#
# It writes only the files that are missing: .env (with its secrets generated here and never printed), release.env,
# compose.locale.yml from deploy/examples, bin/ with the scripts and bin/compose.sh. Run again, it changes nothing that
# exists and says again what is left to do. It starts nothing: the first run of release.sh starts the stack.
set -euo pipefail
umask 077

CLONE=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
die() { echo "install.sh: $*" >&2; exit 2; }
note() { echo "  - $*"; }

DIR='' SITE='' NETWORK='' WEB_IMAGE='' CADDY_IMAGE='' TAG=active ENTRY='' YES=''
while [ $# -gt 0 ]; do
  case "$1" in
    --site) SITE=${2:?}; shift ;;
    --network) NETWORK=${2:?}; shift ;;
    --web-image) WEB_IMAGE=${2:?}; shift ;;
    --caddy-image) CADDY_IMAGE=${2:?}; shift ;;
    --tag) TAG=${2:?}; shift ;;
    --entry) ENTRY=${2:?}; shift ;;
    --yes) YES=1 ;;
    -h|--help) awk 'NR > 1 && !/^#/ { exit } NR > 1 { sub(/^# ?/, ""); print }' "${BASH_SOURCE[0]}"; exit 0 ;;
    -*) die "unknown option $1 (--help)" ;;
    *) [ -z "$DIR" ] || die "one folder only"; DIR=$1 ;;
  esac
  shift
done
[ -n "$DIR" ] || die "which folder? For example /opt/shops (--help)"

ask() {  # ask <variable> <question> [default]: the value given, or asked on the terminal, or the default
  local name=$1 question=$2 default=${3:-} answer
  [ -n "${!name}" ] && return
  if [ -z "$YES" ] && [ -t 0 ]; then
    read -r -p "$question${default:+ [$default]}: " answer
    printf -v "$name" '%s' "${answer:-$default}"
  else
    printf -v "$name" '%s' "$default"
  fi
  [ -n "${!name}" ] || die "missing: $question"
}

value() {  # value <file> <name>: the last value of name=... in a file, without quotes
  local found
  found=$(grep -E "^$2=" "$1" 2>/dev/null | tail -n 1 | cut -d= -f2-) || true
  found=${found#[\"\']}; printf '%s' "${found%[\"\']}"
}

case "$DIR" in /*) ;; *) DIR=$PWD/$DIR ;; esac
DIR=${DIR%/}
PROJECT=$(basename "$DIR")
[[ $PROJECT =~ ^[a-z0-9][a-z0-9_-]*$ ]] || die "the folder's name is the project's name: lowercase letters, digits, - and _ ($PROJECT)"

# An existing installation: what it already says wins, and what is given must agree with it
if [ -f "$DIR/.env" ]; then
  for pair in NETWORK:NETWORK SITE:SITE_HOST; do
    given=${pair%%:*} written=$(value "$DIR/.env" "${pair#*:}")
    [ -z "${!given}" ] || [ "${!given}" = "$written" ] ||
      die "the .env says ${pair#*:}=$written, not ${!given}: this script does not change an existing .env"
    printf -v "$given" '%s' "$written"
  done
fi
if [ -f "$DIR/.env" ] && [ ! -f "$DIR/release.env" ]; then
  die "$DIR has a .env but no release.env: an installation from the sources, or a copied folder. This script prepares
installations from images only, and does not add to that one"
fi
if [ -f "$DIR/release.env" ]; then
  for pair in WEB_IMAGE:WEB_IMAGE_REPO CADDY_IMAGE:CADDY_IMAGE_REPO; do
    given=${pair%%:*} written=$(value "$DIR/release.env" "${pair#*:}")
    [ -z "${!given}" ] || [ "${!given}" = "$written" ] || die "release.env says ${pair#*:}=$written, not ${!given}"
    printf -v "$given" '%s' "$written"
  done
fi

# The entry point of an existing compose.locale.yml: shared when it joins the entry point's network
if [ -f "$DIR/compose.locale.yml" ] && [ -z "$ENTRY" ]; then
  if grep -qE '^ +entry:' "$DIR/compose.locale.yml"; then ENTRY=shared; else ENTRY=direct; fi
fi
ask SITE "Site name (for example shops.example.org)"
ask NETWORK "Network: mainnet or signet (the public signet)" signet
ask WEB_IMAGE "The community's site image, without the tag"
caddy_default=
[[ $WEB_IMAGE == *-web ]] && caddy_default=${WEB_IMAGE%-web}-caddy
ask CADDY_IMAGE "The community's Caddy image, without the tag" "$caddy_default"
ask ENTRY "Entry point: direct (this Caddy holds ports 80 and 443) or shared (network bitmentor-entry)" direct

host_name='^([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,}$'
[[ $SITE =~ $host_name ]] || die "the site's name is a domain name in lowercase, like shops.example.org ($SITE)"
case "$NETWORK" in mainnet|signet) ;; *) die "network: mainnet or signet ($NETWORK)" ;; esac
case "$ENTRY" in direct|shared) ;; *) die "entry point: direct or shared ($ENTRY)" ;; esac
image='^[a-z0-9]([a-z0-9._/:-]*[a-z0-9])?$'
for name in WEB_IMAGE CADDY_IMAGE; do
  last=${!name##*/}
  [[ ${!name} =~ $image ]] && [[ $last != *:* ]] || die "an image without the tag, like registry.example.org/community/web (${!name})"
done
# release.env is sourced by root: the tag is a Docker tag and nothing else
[[ $TAG =~ ^[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}$ ]] || die "the tag is a Docker tag, like active ($TAG)"
[ "$WEB_IMAGE" != "$CADDY_IMAGE" ] || die "the site and Caddy have two different images"
EXAMPLE=$CLONE/deploy/examples/compose.locale.yml
[ -f "$EXAMPLE" ] && [ -f "$CLONE/.env.example" ] || die "run it from a clone of the software: $EXAMPLE is missing"

secret() { od -An -tx1 -N32 /dev/urandom | tr -d ' \n'; }  # 64 hex digits, like openssl rand -hex 32

SS=$(command -v ss || true)
busy_port() {  # a port listening on this machine
  [ -n "$SS" ] && "$SS" -Hltn "sport = :$1" 2>/dev/null | grep -q .
}

# The shared entry point: its Caddy holds TLS and reaches this one over HTTP on its Docker network, so this Caddy
# answers over HTTP and trusts that network's addresses for the visitor's real one (README, "Behind another proxy")
ENTRY_NET=bitmentor-entry
TRUSTED=
if [ "$ENTRY" = shared ] && [ ! -f "$DIR/.env" ]; then
  command -v docker >/dev/null || die "the shared entry point needs Docker, to read the network $ENTRY_NET"
  TRUSTED=$(docker network inspect -f '{{range .IPAM.Config}}{{.Subnet}} {{end}}' "$ENTRY_NET" 2>/dev/null | xargs) ||
    die "there is no Docker network $ENTRY_NET: start the entry point first (docs/installation.md, \"The shared entry point\")"
  [ -n "$TRUSTED" ] || die "the Docker network $ENTRY_NET has no subnet to trust: write it by hand in TRUSTED_PROXIES"
fi

# Ports 80 and 443, checked before anything is written: a refusal leaves the folder as it was
if [ ! -f "$DIR/compose.locale.yml" ]; then
  if [ "$ENTRY" = direct ]; then
    [ -n "$SS" ] || echo "install.sh: ss is missing: ports 80 and 443 are not checked" >&2
    for port in 80 443; do
      ! busy_port "$port" || die "port $port is taken on this machine: another installation holds it? Use --entry shared"
    done
  fi
fi

# Two installations on the same machine: the project's name makes containers, networks and volumes their own
created=()
if [ ! -f "$DIR/.env" ]; then
  if command -v docker >/dev/null && [ -n "$(docker volume ls -q --filter "name=^${PROJECT}_" 2>/dev/null)" ]; then
    die "Docker already has volumes of a project called $PROJECT, without a .env here: they belong to another
installation, or to an old one of another network. Choose another folder name, or remove them first
(docker volume ls --filter name=^${PROJECT}_)"
  fi
  for other in "$(dirname "$DIR")"/*/release.env; do
    [ -f "$other" ] && [ "$other" != "$DIR/release.env" ] || continue
    [ "$(value "$other" SITE)" != "$SITE" ] || die "$(dirname "$other") already serves $SITE"
  done
  mkdir -p "$DIR"
  # Secrets: generated here, written straight into the file, never on the screen nor on a command line
  SECRET_KEY=$(secret) POSTGRES_PASSWORD=$(secret) WEB_DB_PASSWORD=$(secret)
  SCHEME=https
  [ "$ENTRY" = shared ] && SCHEME=http
  export SECRET_KEY POSTGRES_PASSWORD WEB_DB_PASSWORD NETWORK SITE SCHEME TRUSTED
  awk '
    BEGIN { split("NETWORK=NETWORK SITE_HOST=SITE SECRET_KEY=SECRET_KEY POSTGRES_PASSWORD=POSTGRES_PASSWORD WEB_DB_PASSWORD=WEB_DB_PASSWORD SITE_SCHEME=SCHEME TRUSTED_PROXIES=TRUSTED", pairs, " ")
            for (i in pairs) { split(pairs[i], kv, "="); source[kv[1]] = kv[2] } }
    { name = $0; sub(/=.*/, "", name) }
    /^[A-Z_]+=/ && (name in source) { print name "=" ENVIRON[source[name]]; next }
    /^SITE_MODE=/ { print "SITE_MODE=development"; next }
    /^COMMUNITY_DIR=/ { print "# The community is inside the site image, in /community"; next }
    { print }
  ' "$CLONE/.env.example" > "$DIR/.env.new"
  unset SECRET_KEY POSTGRES_PASSWORD WEB_DB_PASSWORD
  chmod 600 "$DIR/.env.new" && mv "$DIR/.env.new" "$DIR/.env"
  created+=(.env)
fi

if [ ! -f "$DIR/release.env" ]; then
  printf 'PROJECT=%s\nSITE=%s\nWEB_IMAGE_REPO=%s\nCADDY_IMAGE_REPO=%s\nTAG=%s\n' \
    "$PROJECT" "$SITE" "$WEB_IMAGE" "$CADDY_IMAGE" "$TAG" > "$DIR/release.env"
  created+=(release.env)
fi

if [ ! -f "$DIR/compose.locale.yml" ]; then
  if [ "$ENTRY" = direct ]; then
    sed -e '/^ *#@shared /d; s/^#@direct //' "$EXAMPLE"
  else
    sed -e '/^#@direct /d; s/#@shared //' "$EXAMPLE"
  fi | sed -e "s/caddy-<project>/caddy-$PROJECT/" \
    > "$DIR/compose.locale.yml.new"
  mv "$DIR/compose.locale.yml.new" "$DIR/compose.locale.yml"
  created+=(compose.locale.yml)
fi

mkdir -p "$DIR/bin" "$DIR/state"
for script in release.sh check.sh mode.sh; do
  # release.sh replaces them with the ones of each release: here only the first time
  [ -f "$DIR/bin/$script" ] || { install -m 755 "$CLONE/deploy/$script" "$DIR/bin/$script"; created+=("bin/$script"); }
done
if [ ! -f "$DIR/bin/compose.sh" ]; then
  cat > "$DIR/bin/compose.sh" <<'EOF'
#!/usr/bin/env bash
# docker compose on this installation, with the files and images of the installed version, as release.sh uses it:
#   bin/compose.sh ps    bin/compose.sh logs -f web    bin/compose.sh exec web python manage.py check
set -euo pipefail
DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
cd "$DIR"
. ./release.env
version=$(cat state/version 2>/dev/null) || { echo "no version installed yet: the first run of bin/release.sh installs one" >&2; exit 1; }
file=(-f current/docker-compose.yml -f current/compose.release.yml)
[ -f current/compose.community.yml ] && file+=(-f current/compose.community.yml)
ipv6=$(grep -E '^IPV6_SUBNET=' .env | tail -n 1 | cut -d= -f2- || true); ipv6=${ipv6#[\"\']}
[ -f current/compose.ipv6.yml ] && [[ $ipv6 =~ ^[0-9a-fA-F] ]] && file+=(-f current/compose.ipv6.yml)
[ -f compose.locale.yml ] && file+=(-f compose.locale.yml)
WEB_IMAGE="$WEB_IMAGE_REPO:$version" CADDY_IMAGE="$CADDY_IMAGE_REPO:$version" \
  exec docker compose -p "$PROJECT" --project-directory "$DIR/current" --env-file "$DIR/.env" "${file[@]}" "$@"
EOF
  chmod 755 "$DIR/bin/compose.sh"
  created+=(bin/compose.sh)
fi

# The network: what the .env says against what was asked
problems=()
env_network=$(value "$DIR/.env" NETWORK)
[ "$env_network" = "$NETWORK" ] || problems+=("the .env says NETWORK=$env_network")
# Behind the shared entry point, which holds TLS: over HTTPS this Caddy would redirect forever, and without
# TRUSTED_PROXIES every visitor would have the entry point's address
if [ "$ENTRY" = shared ]; then
  [ "$(value "$DIR/.env" SITE_SCHEME)" = http ] || problems+=("behind the shared entry point the .env needs SITE_SCHEME=http")
  [ -n "$(value "$DIR/.env" TRUSTED_PROXIES)" ] || problems+=("behind the shared entry point the .env needs TRUSTED_PROXIES: the subnet of the network $ENTRY_NET")
fi

echo "Installation $PROJECT in $DIR: $SITE on $NETWORK"
if [ ${#created[@]} -gt 0 ]; then
  echo "Written now: ${created[*]}. Secrets generated in the .env (readable by root only), never shown."
else
  echo "Nothing changed: every file was already there."
fi
if [ ${#problems[@]} -gt 0 ]; then
  echo
  echo "The configuration does not add up. Fix these first:"
  for problem in "${problems[@]}"; do note "$problem"; done
  exit 1
fi

missing=()
[ -n "$(value "$DIR/.env" DEV_ALLOWED_IPS)" ] || missing+=(dev)
[ -n "$(value "$DIR/.env" EMAIL_HOST)" ] || missing+=(email)
[ -f "$DIR/alerts.env" ] || missing+=(alerts)
[ -f "$DIR/state/version" ] || missing+=(start)

echo
echo "What is left, in order:"
step=0
say() { step=$((step + 1)); echo "$step. $1"; shift; for line in "$@"; do echo "   $line"; done; }
say "DNS: $SITE points to this machine (A record; AAAA only after README, \"IPv6\")." \
  "$( [ "$ENTRY" = shared ] && echo "The shared entry point forwards the name to caddy-$PROJECT." || echo "Ports 80 and 443 open in the firewall: Caddy gets the certificates by itself.")"
if [[ " ${missing[*]} " == *" start "* ]]; then
  [ "$DIR" = "/opt/$PROJECT" ] || echo "   (the systemd units run /opt/<project>: this folder is $DIR, start bin/release.sh $DIR by hand)"
  # The units are templates, shared by every installation on the machine: only the missing ones are copied, and
  # from then on they come from the releases (current/deploy/systemd), as in docs/mainnet.md
  units=()
  for unit in "$CLONE"/deploy/systemd/bitmentor-*; do
    [ -e "${INSTALL_SYSTEMD:-/etc/systemd/system}/${unit##*/}" ] || units+=("$unit")
  done
  if [ ${#units[@]} -gt 0 ]; then
    copy="cp -n ${units[*]} /etc/systemd/system/ && systemctl daemon-reload"
  else
    copy="(the systemd units are already there, shared with the installations next to this one: they stay as they are)"
  fi
  say "Start: the release timer installs the version tagged $TAG within a minute." \
    "$copy" \
    "systemctl enable --now bitmentor-release@$PROJECT.timer bitmentor-check@$PROJECT.timer \\" \
    "  bitmentor-sentinel@$PROJECT.timer bitmentor-dashboard@$PROJECT.timer \\" \
    "  bitmentor-osm@$PROJECT.timer bitmentor-meetups@$PROJECT.timer" \
    "journalctl -fu bitmentor-release@$PROJECT    (it ends with: installed <version>)" \
    "Every hour bitmentor-sentinel tries the shops' Lightning addresses." \
    "Every morning bitmentor-dashboard sends what to do about the shops, when it changed (or weekly), to" \
    "TELEGRAM_MODERATION_CHAT in the .env, or else to the email of community.yml." \
    "Once a month bitmentor-osm reads OpenStreetMap, only if community.yml has an osm section (docs/features/openstreetmap.md)." \
    "Every day bitmentor-meetups reads the meetups that the trusted authors of the \`meetups\` block publish on Nostr (it does nothing without the block)." \
    "The nightly backup, bitmentor-backup@$PROJECT.timer, once backup.env is ready (docs/operations.md, \"Turn on the backup\")." \
    "What each timer does, when it runs and what it needs: docs/operations.md, \"The timers of a production machine\"."
fi
say "The site's first administrator: $DIR/bin/compose.sh exec web python manage.py createsuperuser"
if [[ " ${missing[*]} " == *" dev "* ]]; then
  say "Who sees the site while it is in development: your addresses in DEV_ALLOWED_IPS in the .env, then $DIR/bin/compose.sh up -d web caddy." \
    "Until then everybody, you included, sees the under-construction page."
fi
if [[ " ${missing[*]} " == *" email "* ]]; then
  say "Email: EMAIL_HOST, EMAIL_HOST_USER, EMAIL_HOST_PASSWORD, DEFAULT_FROM_EMAIL in the .env. Without them nobody gets a sign-in link."
fi
if [[ " ${missing[*]} " == *" alerts "* ]]; then
  say "Alerts: alerts.env with TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_IDS, then $DIR/bin/check.sh $DIR --test \"hello\""
fi
say "Check: $DIR/bin/compose.sh exec web python manage.py check" \
  "It says \"System check identified no issues\" when the configuration holds together."
if [ "$NETWORK" = mainnet ]; then
  say "Before real payments: the decisions and the steps of docs/mainnet.md, then manage.py preflight must say \"All ready\"." \
    "Only you open the site: bin/mode.sh $DIR production."
else
  say "Test coins: from the public signet faucets. On signet the site stays in development or maintenance: production is mainnet only."
fi
