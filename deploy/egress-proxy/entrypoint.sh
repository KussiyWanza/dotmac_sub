#!/bin/sh
# Egress proxy for external connectors (ADR 0005 Phase 4b).
#
# The connector runs on an --internal Podman network with no route out. This
# proxy is dual-homed onto that network and an external one, and is the only
# path to the internet. It enforces a default-deny allowlist of the exact hosts
# the connector's manifest declares.
set -eu

# Podman puts the default route on the internal network regardless of attach
# order, and that network has no NAT — so without this the proxy itself cannot
# reach the internet. Resolve the interface facing the external gateway rather
# than assuming an interface name, which is not stable.
if [ -n "${EXTERNAL_GATEWAY:-}" ]; then
    iface=$(ip -o -4 route get "$EXTERNAL_GATEWAY" 2>/dev/null |
        sed -n 's/.* dev \([^ ]*\).*/\1/p' | head -1)
    if [ -z "$iface" ]; then
        echo "egress-proxy: no interface reaches $EXTERNAL_GATEWAY" >&2
        exit 1
    fi
    ip route replace default via "$EXTERNAL_GATEWAY" dev "$iface"
fi

# Default-deny: the filter file is the whole allowlist. Each host becomes an
# anchored regex so "evil-api.paystack.co.attacker.test" cannot match
# "api.paystack.co". An empty allowlist therefore denies everything.
: >/tmp/egress-allowlist
if [ -n "${ALLOWED_HOSTS:-}" ]; then
    echo "$ALLOWED_HOSTS" | tr ',' '\n' | while read -r host; do
        [ -n "$host" ] || continue
        escaped=$(printf '%s' "$host" | sed 's/\./\\./g')
        printf '^%s$\n' "$escaped" >>/tmp/egress-allowlist
    done
fi

exec tinyproxy -d -c /etc/tinyproxy/tinyproxy.conf
