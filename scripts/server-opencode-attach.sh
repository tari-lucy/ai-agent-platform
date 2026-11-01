#!/usr/bin/env bash
# Terminal client for the agent's OpenCode, run on the server itself (no tunnel needed).
#   ssh <user>@<server>, then:  ~/ai-platform/opencode-attach.sh
# The password is read from the host's file into the client's environment only; it is never
# put on the command line, printed or written anywhere. Ctrl+C leaves the client; STOP is in
# the panel. Nothing here starts or changes the agent.
set -euo pipefail
base="${AI_PLATFORM_HOME:-$HOME/ai-platform}"
cli="$base/opencode-cli"
password_file="$base/host/data/harness/password"
port="${OPENCODE_PORT:-4096}"

if ! (exec 3<>"/dev/tcp/127.0.0.1/$port") 2>/dev/null; then
  echo "OpenCode агента не запущен. Открой панель и нажми START, потом повтори." >&2
  exit 1
fi
if [ ! -r "$password_file" ] || [ ! -x "$cli/opencode" ]; then
  echo "Не хватает файла пароля или клиента OpenCode ($cli)." >&2
  exit 1
fi

# The client keeps its own state under opencode-cli/, not in the login home.
export HOME="$cli" XDG_DATA_HOME="$cli/data" XDG_CONFIG_HOME="$cli/config"
export XDG_STATE_HOME="$cli/state" XDG_CACHE_HOME="$cli/cache"
export OPENCODE_DISABLE_AUTOUPDATE=true OPENCODE_SERVER_USERNAME=opencode
OPENCODE_SERVER_PASSWORD="$(tr -d '\r\n' <"$password_file")"
export OPENCODE_SERVER_PASSWORD
exec "$cli/opencode" attach "http://127.0.0.1:$port" --dir /workspace --pure
