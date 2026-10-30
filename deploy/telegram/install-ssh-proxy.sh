#!/usr/bin/env bash
# Install a dedicated SSH forwarding account. No packages, Docker or firewall changes.
set -euo pipefail
[[ $(id -u) == 0 ]] || { echo 'Run this script as root.'; exit 1; }
[[ $# == 1 && -f $1 ]] || { echo 'Expected a public-key file.'; exit 1; }
read -r kind material _ < "$1"
[[ $kind == ssh-ed25519 && $material =~ ^[A-Za-z0-9+/=]+$ ]] || exit 1
operator=$(stat -c '%U' "$1")
account=aw-telegram-proxy
home=/var/lib/aw-telegram-proxy
conf=/etc/ssh/sshd_config.d/80-aw-telegram-proxy.conf
sshd=/usr/sbin/sshd
[[ -x $sshd && -d /etc/ssh/sshd_config.d ]] || exit 1
scratch=$(mktemp -d /run/aw-telegram-proxy.XXXXXX)
trap 'rm -rf -- "$scratch"' EXIT
"$sshd" -T -C user="$operator",host=localhost,addr=127.0.0.1 > "$scratch/before"
cat > "$scratch/config" <<'CONFIG'
# AI Agent Platform: only this dedicated account is affected.
Match User aw-telegram-proxy
    AuthenticationMethods publickey
    PasswordAuthentication no
    KbdInteractiveAuthentication no
    AllowTcpForwarding local
    PermitOpen api.telegram.org:443
    PermitListen none
    AllowStreamLocalForwarding no
    AllowAgentForwarding no
    X11Forwarding no
    PermitTTY no
    PermitTunnel no
    GatewayPorts no
    ForceCommand /bin/false
Match all
CONFIG
if [[ -e $conf ]] && ! cmp -s "$conf" "$scratch/config"; then
    echo 'Existing proxy configuration differs; nothing overwritten.'; exit 1
fi
if getent passwd "$account" > "$scratch/account"; then
    [[ $(cut -d: -f6 "$scratch/account") == "$home" ]] || exit 1
    [[ $(cut -d: -f7 "$scratch/account") == /usr/sbin/nologin ]] || exit 1
else
    useradd --system --user-group --home-dir "$home" --shell /usr/sbin/nologin "$account"
    # Impossible password, not a locked account; SSH accepts only the supplied public key.
    usermod --password '*' "$account"
fi
[[ ! -L $home && ! -L $home/.ssh && ! -L $home/.ssh/authorized_keys ]] || exit 1
printf 'restrict,port-forwarding,permitopen="api.telegram.org:443",command="/bin/false" %s %s ai-platform-telegram-proxy\n' \
    "$kind" "$material" > "$scratch/key"
if [[ -e $home/.ssh/authorized_keys ]] && ! cmp -s "$home/.ssh/authorized_keys" "$scratch/key"; then
    echo 'Existing proxy key differs; nothing overwritten.'; exit 1
fi
install -d -o root -g root -m 755 "$home" "$home/.ssh"
install -o root -g root -m 644 "$scratch/key" "$home/.ssh/authorized_keys"
new_config=0
[[ -e $conf ]] || new_config=1
install -o root -g root -m 644 "$scratch/config" "$conf"
rollback() {
    if [[ $new_config == 1 ]]; then rm -- "$conf"; fi
    echo 'SSH configuration check failed; SSH was not reloaded.'
    exit 1
}
"$sshd" -t || rollback
"$sshd" -T -C user="$operator",host=localhost,addr=127.0.0.1 > "$scratch/after"
cmp -s "$scratch/before" "$scratch/after" || rollback
"$sshd" -T -C user="$account",host=localhost,addr=127.0.0.1 > "$scratch/effective"
for expected in \
    'allowtcpforwarding local' 'permitopen api.telegram.org:443' 'permitlisten none' \
    'passwordauthentication no' 'kbdinteractiveauthentication no' \
    'allowstreamlocalforwarding no' 'forcecommand /bin/false'; do
    grep -qxF "$expected" "$scratch/effective" || rollback
done
systemctl reload ssh
echo 'TELEGRAM_PROXY_ACCOUNT_READY'
echo 'Only the dedicated account was configured. No packages, Docker or firewall changes.'
