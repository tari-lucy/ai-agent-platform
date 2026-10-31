#!/usr/bin/env bash
# Install a dedicated SSH account that may only open ONE reverse listener on the server's
# loopback (the game mod's port). No shell, no other forwarding, no packages, Docker or firewall
# changes. Run as root: sudo bash install-game-tunnel-account.sh <public-key-file> [port]
set -euo pipefail
[[ $(id -u) == 0 ]] || { echo 'Run this script as root.'; exit 1; }
[[ $# -ge 1 && -f $1 ]] || { echo 'Expected a public-key file.'; exit 1; }
port=${2:-25350}
[[ $port =~ ^[0-9]{4,5}$ ]] || { echo 'Invalid port.'; exit 1; }
read -r kind material _ < "$1"
[[ $kind == ssh-ed25519 && $material =~ ^[A-Za-z0-9+/=]+$ ]] || { echo 'Expected an ed25519 key.'; exit 1; }
operator=$(stat -c '%U' "$1")
account=aw-game-tunnel
home=/var/lib/aw-game-tunnel
conf=/etc/ssh/sshd_config.d/81-aw-game-tunnel.conf
sshd=/usr/sbin/sshd
[[ -x $sshd && -d /etc/ssh/sshd_config.d ]] || exit 1
scratch=$(mktemp -d /run/aw-game-tunnel.XXXXXX)
trap 'rm -rf -- "$scratch"' EXIT
"$sshd" -T -C user="$operator",host=localhost,addr=127.0.0.1 > "$scratch/before"
cat > "$scratch/config" <<CONFIG
# AI Agent Platform: only this dedicated account is affected.
Match User $account
    AuthenticationMethods publickey
    PasswordAuthentication no
    KbdInteractiveAuthentication no
    AllowTcpForwarding remote
    PermitListen 127.0.0.1:$port
    PermitOpen none
    AllowStreamLocalForwarding no
    AllowAgentForwarding no
    X11Forwarding no
    PermitTTY no
    PermitTunnel no
    GatewayPorts no
    ClientAliveInterval 30
    ClientAliveCountMax 3
    ForceCommand /bin/false
Match all
CONFIG
if [[ -e $conf ]] && ! cmp -s "$conf" "$scratch/config"; then
    echo 'Existing configuration differs; nothing overwritten.'; exit 1
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
printf 'restrict,port-forwarding,permitlisten="127.0.0.1:%s",command="/bin/false" %s %s ai-platform-game-tunnel\n' \
    "$port" "$kind" "$material" > "$scratch/key"
if [[ -e $home/.ssh/authorized_keys ]] && ! cmp -s "$home/.ssh/authorized_keys" "$scratch/key"; then
    echo 'Existing key differs; nothing overwritten.'; exit 1
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
    'allowtcpforwarding remote' "permitlisten 127.0.0.1:$port" 'permitopen none' \
    'passwordauthentication no' 'kbdinteractiveauthentication no' \
    'allowstreamlocalforwarding no' 'clientaliveinterval 30' 'clientalivecountmax 3' \
    'forcecommand /bin/false'; do
    grep -qxF "$expected" "$scratch/effective" || rollback
done
systemctl reload ssh
echo 'GAME_TUNNEL_ACCOUNT_READY'
echo 'Only the dedicated account was configured. No packages, Docker or firewall changes.'
