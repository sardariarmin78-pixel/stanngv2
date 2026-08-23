#!/bin/sh
set -e

# The platform assigns the public port at runtime; nginx needs it baked in.
PORT="${PORT:-8000}"
PANEL_PORT="${PANEL_PORT:-10000}"
export PANEL_PORT

sed -i -E "s/listen ([0-9]+|NGINX_PORT);/listen ${PORT};/" /etc/nginx/nginx.conf

nginx -t
nginx

echo "[peyk] nginx on ${PORT}, panel on ${PANEL_PORT}"

# The panel starts and supervises xray itself, so it stays in the foreground:
# if it dies the container dies and the platform restarts it.
exec python3 main.py
