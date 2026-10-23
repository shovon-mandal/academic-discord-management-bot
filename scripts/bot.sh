#!/usr/bin/env bash
# Shortcuts on the Tinkerboard:  bash scripts/bot.sh start|stop|restart|status|log|update
SERVICE="nubtk-bot"
APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
case "${1:-status}" in
  start)   sudo systemctl start "$SERVICE" ;;
  stop)    sudo systemctl stop "$SERVICE" ;;
  restart) sudo systemctl restart "$SERVICE" ;;
  status)  systemctl status "$SERVICE" --no-pager ;;
  log)     journalctl -u "$SERVICE" -f -n 100 ;;
  update)
    cd "$APP_DIR" && git pull --ff-only && venv/bin/python -m pip install -q -r requirements.txt \
      && sudo systemctl restart "$SERVICE" && echo "✅ updated + restarted" ;;
  *) echo "usage: $0 start|stop|restart|status|log|update" ;;
esac
