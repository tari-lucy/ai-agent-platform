#!/bin/sh
set -eu
: "${AI_TG_VPS_HOST:?VPS hostname required}"
case "$AI_TG_VPS_HOST" in ''|*[!A-Za-z0-9.-]*) echo INVALID_VPS_HOST >&2; exit 1;; esac
umask 077
# Windows bind permissions do not need to be trusted by OpenSSH. The private copy
# lives only in the non-root container's tmpfs and disappears with the container.
cat /secrets/identity > /run/tunnel/identity
chmod 600 /run/tunnel/identity
cat /secrets/known_hosts > /run/tunnel/known_hosts
child=''
stop() {
    if [ -n "$child" ]; then kill "$child" 2>/dev/null || true; wait "$child" 2>/dev/null || true; fi
    exit 0
}
trap stop TERM INT
while :; do
    ssh -M -S /run/tunnel/control -N -n -T -a -x -D 0.0.0.0:1080 \
        -i /run/tunnel/identity -l aw-telegram-proxy \
        -o UserKnownHostsFile=/run/tunnel/known_hosts \
        -o GlobalKnownHostsFile=/dev/null -o StrictHostKeyChecking=yes \
        -o IdentitiesOnly=yes -o BatchMode=yes -o PasswordAuthentication=no \
        -o KbdInteractiveAuthentication=no -o ConnectTimeout=10 \
        -o ConnectionAttempts=1 -o ExitOnForwardFailure=yes \
        -o ServerAliveInterval=30 -o ServerAliveCountMax=3 -o LogLevel=ERROR \
        "$AI_TG_VPS_HOST" &
    child=$!
    wait "$child" || true
    child=''
    sleep 15 &
    child=$!
    wait "$child" || true
    child=''
done
