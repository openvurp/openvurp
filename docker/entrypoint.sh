#!/usr/bin/env sh
# openvurp container entrypoint.
#   headless  -> the wallet + gateway + inbound channels + heartbeat (default)
#   tui       -> the interactive terminal (docker compose exec openvurp openvurp)
#   shell     -> a service shell
#   <other>   -> run as given
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
