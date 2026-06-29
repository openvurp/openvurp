#!/usr/bin/env sh
# Entrypoint del container openvurp.
#   headless  → server (dashboard chat + gateway + telegram + heartbeat), default
#   tui       → TUI interattiva (usa: docker compose exec openvurp openvurp)
#   shell     → shell di servizio
#   <altro>   → eseguito così com'è
set -e
cd /app

mode="${1:-headless}"
case "$mode" in
  headless)
    exec python main.py --headless
    ;;
  tui)
    exec python TUI.py
    ;;
  shell|sh|bash)
    exec /bin/sh
    ;;
  *)
    exec "$@"
    ;;
esac
