#!/usr/bin/env bash
# Lightweight check of the site's machine, every 10 minutes (bitmentor-check@<project>.timer).
# Alerts on Telegram only about serious problems, when they change, and repeats them every 6 hours while they last.
# Usage: check.sh <directory>                 the check
#        check.sh <directory> --test <text>  sends only <text>, to test the bot
# alerts.env in the folder: TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_IDS (numbers separated by spaces, quotes optional).
# Without it, it only writes to the log. With TELEGRAM_CHAT_IDS_BEFORE, until the site has been in production for
# OPENING_DAYS days (7; SITE_MODE and SITE_MODE_SINCE in the .env), it writes only to those chats.
set -uo pipefail
DIR=${1:?missing folder}
cd "$DIR" || exit 1
. ./release.env

# alerts.env is read line by line, without executing it: someone who is not root may write it. A last line without a newline counts too.
# Token and recipients come only from there, not from the environment (in CI, for example, there is the group's token).
unset TELEGRAM_BOT_TOKEN TELEGRAM_CHAT_IDS TELEGRAM_CHAT_IDS_BEFORE OPENING_DAYS
if [ -f alerts.env ]; then
  while IFS='=' read -r name value || [ -n "$name" ]; do
    case "$name" in TELEGRAM_BOT_TOKEN|TELEGRAM_CHAT_IDS|TELEGRAM_CHAT_IDS_BEFORE|OPENING_DAYS) ;; *) continue ;; esac
    value=${value%$'\r'}; value=${value#[\"\']}; value=${value%[\"\']}
    printf -v "$name" '%s' "$value"
  done < alerts.env
fi
: "${SITE:?}" "${PROJECT:?}"
mkdir -p state
REMIND=$((6 * 3600))

recipients() {  # before the opening, plus the trial days, only TELEGRAM_CHAT_IDS_BEFORE
  local mode since
  mode=$(grep -m1 '^SITE_MODE=' .env | cut -d= -f2)
  since=$(grep -m1 '^SITE_MODE_SINCE=' .env | cut -d= -f2)
  if [ -n "${TELEGRAM_CHAT_IDS_BEFORE:-}" ] && { [ "$mode" != production ] ||
       { [ -n "$since" ] && [ "$(date -u -d "$since + ${OPENING_DAYS:-7} days" +%s)" -gt "$(date -u +%s)" ]; }; }; then
    echo "$TELEGRAM_CHAT_IDS_BEFORE"
  else
    echo "${TELEGRAM_CHAT_IDS:-}"
  fi
}

send() {  # send <text> to the recipients; the token goes to curl on stdin, not on the command line
  local text=$1 chat result=0 chat_ids
  chat_ids=$(recipients)
  if [ -z "${TELEGRAM_BOT_TOKEN:-}" ] || [ -z "$chat_ids" ]; then
    echo "Telegram not configured: $text"
    return 1
  fi
  for chat in $chat_ids; do
    printf 'url = "https://api.telegram.org/bot%s/sendMessage"\n' "$TELEGRAM_BOT_TOKEN" |
      curl -sS -m 15 -K - -o /dev/null -w '%{http_code}\n' --data-urlencode "chat_id=$chat" --data-urlencode "text=$text" |
      grep -qx 200 || { echo "Telegram: sending to $chat failed"; result=1; }
  done
  return $result
}

if [ "${2:-}" = "--test" ]; then
  send "${3:?missing text}"
  exit $?
fi

problems=()

# The services that ran after the last release must still run, and not sick
while read -r service; do
  [ -n "$service" ] || continue
  state=$(docker ps -a --filter "label=com.docker.compose.project=$PROJECT" --filter "label=com.docker.compose.service=$service" \
    --format '{{.State}} {{.Status}}' | head -1)
  case "$state" in
    "") problems+=("$service: the container is not there") ;;
    running*unhealthy*) problems+=("$service: not well (healthcheck)") ;;
    running*) ;;
    *) problems+=("$service: ${state%% *}") ;;
  esac
done < <(cat state/services 2>/dev/null)

# The site, from the entry point of this machine
code=$(curl -s -m 20 -o state/health.json -w '%{http_code}' --resolve "$SITE:443:127.0.0.1" "https://$SITE/health/")
case "$code" in
  200) ;;
  503) problems+=("the site reports: $(python3 -c 'import json,sys; print(", ".join(json.load(sys.stdin)["problems"]))' < state/health.json 2>/dev/null || echo '?')") ;;
  000) problems+=("the site does not answer") ;;
  *) problems+=("the site answers $code on /health/") ;;
esac

if ! echo | openssl s_client -connect 127.0.0.1:443 -servername "$SITE" 2>/dev/null | openssl x509 -noout -checkend $((14 * 86400)) >/dev/null 2>&1; then
  problems+=("the certificate of $SITE expires within 14 days, or cannot be read")
fi

for mount in / /var/lib/docker; do
  free=$(df -P "$mount" 2>/dev/null | awk 'NR==2 {print 100 - $5}')
  [ -n "$free" ] && [ "$free" -lt 10 ] && problems+=("disk $mount: only $free% free")
done
memory=$(awk '/MemTotal/ {t=$2} /MemAvailable/ {a=$2} END {print int(a * 100 / t)}' /proc/meminfo)
[ "$memory" -lt 5 ] && problems+=("memory: only $memory% available")

# The automatic release
[ -f state/failed ] && problems+=("release $(cat state/failed) did not start: staying on $(cat state/version 2>/dev/null || echo 'the previous version')")
systemctl is-failed --quiet "bitmentor-release@$PROJECT.service" 2>/dev/null && problems+=("the automatic release stopped with an error")
# The morning summary: failed even after its tries, neither Telegram nor the email took it
systemctl is-failed --quiet "bitmentor-dashboard@$PROJECT.service" 2>/dev/null && problems+=("the morning summary (what to do for the shops) did not arrive: journalctl -u bitmentor-dashboard@$PROJECT")
# The monthly reading of OpenStreetMap: failed even after its tries (Overpass down, or an answer that cannot be right)
systemctl is-failed --quiet "bitmentor-osm@$PROJECT.service" 2>/dev/null && problems+=("the monthly reading of OpenStreetMap failed: journalctl -u bitmentor-osm@$PROJECT")
# The daily reading of the meetups from Nostr: no relay answered, even after its retries
systemctl is-failed --quiet "bitmentor-meetups@$PROJECT.service" 2>/dev/null && problems+=("the reading of the meetups from Nostr failed: journalctl -u bitmentor-meetups@$PROJECT")
ok=$(cat state/registry.ok 2>/dev/null || echo 0)
[ $(( $(date +%s) - ok )) -gt 3600 ] && problems+=("the image registry has not answered for more than an hour")

# The night backup, if configured (backup.env): backup.sh writes the time of the last success to state/backup.ok.
# It runs once a day, up to half an hour late: 26 hours mean a skipped night.
# Until it has ever succeeded, it counts from when backup.env appeared, so whoever sets it up today gets no alert at once.
if [ -f backup.env ]; then
  last=$(head -n 1 state/backup.ok 2>/dev/null | cut -c 1-40 || true)
  if [ -n "$last" ]; then
    moment=$(date -u -d "$last" +%s 2>/dev/null || echo 0)
  else
    moment=$(stat -c %Y backup.env)
  fi
  if [ $(( $(date +%s) - moment )) -gt $((26 * 3600)) ]; then
    if [ -n "$last" ]; then
      problems+=("the night backup has been failing for more than 26 hours: the last success was $last")
    else
      problems+=("the night backup has never succeeded: backup.env has been there for more than 26 hours")
    fi
  fi
fi

# It writes only when something changes, or to remind every 6 hours
now=$(printf '%s\n' "${problems[@]}" | sort)
before=$(cat state/alerts 2>/dev/null || true)
age=$(( $(date +%s) - $(stat -c %Y state/alerts 2>/dev/null || echo 0) ))
name="$SITE ($(hostname -s))"
if [ "$now" != "$before" ] || { [ -n "$now" ] && [ "$age" -gt "$REMIND" ]; }; then
  if [ -n "$now" ]; then
    text="⚠️ $name"$'\n'"$(printf -- '- %s\n' "${problems[@]}")"
  else
    text="✅ $name: all fine again"
  fi
  echo "$text"
  send "$text" && printf '%s' "$now" > state/alerts
fi
exit 0
