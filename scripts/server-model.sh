#!/usr/bin/env bash
# Which model the persona runs on, from the server's terminal.
#   model list          what the provider offers (* = the current one)
#   model test <id>     one tiny real request: does it answer, and how fast
#   model set <id>      test it, then switch (restarts the service; she resumes by herself)
# Keys and endpoints are never printed: the host's own binding is used.
set -euo pipefail
base="${AI_PLATFORM_HOME:-$HOME/ai-platform}"
run() {
  "$base/venv/bin/python" -I -m ai_agent_core.local_host "$@"
}
env_file="$base/.env"
root="$base/host"
case "${1:-}" in
  list) run list-models --root "$root" --env-file "$env_file" ;;
  test)
    [ -n "${2:-}" ] || { echo "usage: model test <id>" >&2; exit 2; }
    run test-model --root "$root" --env-file "$env_file" "$2"
    ;;
  set)
    [ -n "${2:-}" ] || { echo "usage: model set <id>" >&2; exit 2; }
    if ! run test-model --root "$root" --env-file "$env_file" "$2"; then
      echo "Модель не отвечает: не переключаю. (MODEL_FORCE=1 model set <id> переключит всё равно.)" >&2
      [ "${MODEL_FORCE:-}" = "1" ] || exit 1
    fi
    systemctl --user stop ai-platform
    run set-model --root "$root" "$2"
    systemctl --user start ai-platform
    echo "Служба запущена с моделью $2. Если агент работал сам, он поднимется сам через 1-2 минуты;"
    echo "иначе нажми START в панели."
    ;;
  *) echo "usage: model list | model test <id> | model set <id>" >&2; exit 2 ;;
esac
