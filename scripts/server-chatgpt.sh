#!/usr/bin/env bash
# ChatGPT plan trial (OpenCode's own sign-in), from the server's terminal.
#   chatgpt enable            OpenCode offers the OpenAI provider; then sign in: opencode -> /connect
#   chatgpt on <openai/model> everything defaults to that plan model (work, game, life, helpers)
#   chatgpt off               back to the platform's own model; the default plan model is cleared
#   chatgpt forget            delete the sign-in file from the agent's volume (after `off`)
#   chatgpt status
# The token lives in the agent's container while this is on, and STOP/budget do not cover it.
set -euo pipefail
base="${AI_PLATFORM_HOME:-$HOME/ai-platform}"
root="$base/host"
run() { "$base/venv/bin/python" -I -m ai_agent_core.local_host "$@"; }
restart_with() {
  systemctl --user stop ai-platform
  trap 'systemctl --user start ai-platform' EXIT  # whatever happens, leave the service running
  run configure-harness --root "$root" "$@"
}
case "${1:-}" in
  enable) restart_with --native-chatgpt
    echo "Готово. Когда служба запустится и агент работает: opencode -> /connect -> OpenAI -> ChatGPT Pro/Plus (headless)." ;;
  on)
    [[ "${2:-}" == openai/* ]] || { echo "usage: chatgpt on openai/<model> (имя из /models)" >&2; exit 2; }
    restart_with --native-chatgpt --native-chatgpt-model "$2"
    echo "Готово: по умолчанию всё идёт через подписку ($2)." ;;
  off) restart_with --no-native-chatgpt
    echo "Готово: снова собственная модель платформы. Войти заново нельзя, пока не 'chatgpt enable'." ;;
  forget)
    c=$(docker ps --format '{{.Names}}' | grep aw-harness | head -1 || true)
    [ -n "$c" ] || { echo "Агент не запущен: запусти его START и повтори." >&2; exit 1; }
    docker exec "$c" sh -c 'rm -f /persist/.harness/data/opencode/auth.json && echo "файл входа удалён"' ;;
  status)
    python3 - "$root/host.json" <<'PY'
import json, sys
h = json.load(open(sys.argv[1])).get("harness", {})
print("native_chatgpt:", h.get("native_chatgpt"), "| default model:", h.get("native_chatgpt_model"))
PY
    ;;
  *) echo "usage: chatgpt enable | on <openai/model> | off | forget | status" >&2; exit 2 ;;
esac
