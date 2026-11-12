#!/bin/sh
# Onion service to the site: it goes through Caddy, so the same rules apply and the client's address cannot be forged.
# The image is pinned by digest (docker-compose.yml); tor comes from its Alpine branch, with no pin on the version or
# on the series: an Alpine branch keeps only the latest build of a package, and moves to the next series when upstream
# retires one, so either pin would stop the start at one of the branch's security fixes. What bounds tor is the Alpine
# release: a different one is a change to test, together with the image
set -eu
apk add --no-cache tor >/dev/null || {
  echo "tor.sh: apk could not install tor (see its error above): no onion service until it can" >&2
  exit 1
}
echo "tor.sh: $(tor --version | head -n 1)"
chown -R tor /var/lib/tor
chmod 700 /var/lib/tor
# Caddy's IPv4 address: with compose.ipv6.yml `getent hosts` may give the IPv6 one, which tor wants in brackets
caddy=$(getent ahostsv4 caddy | awk '{print $1; exit}')
[ -n "$caddy" ] || caddy="[$(getent hosts caddy | awk '{print $1; exit}')]"
[ "$caddy" != "[]" ] || { echo "tor.sh: the name caddy has no address: is the caddy service running?" >&2; exit 1; }
cat > /tmp/torrc <<EOF
HiddenServiceDir /var/lib/tor/site
HiddenServicePort 80 $caddy:8081
EOF
exec su -s /bin/sh tor -c "tor -f /tmp/torrc"
