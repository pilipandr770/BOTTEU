#!/bin/sh
# Wraps nginx startup so a fresh deployment never crash-loops on missing TLS certs.
#
# nginx.conf hardcodes an `ssl_certificate` path under /etc/letsencrypt/live/<domain>/.
# On a brand-new host that path doesn't exist yet (certbot only creates it after a
# successful ACME challenge, which itself requires nginx to already be serving
# /.well-known/acme-challenge/ on port 80) — a classic chicken-and-egg problem.
#
# Fix: if the real cert is missing, generate a throwaway self-signed one so nginx
# can bind :443 and start. Once you run certbot for real (see README "First-time
# SSL"), it overwrites these files; `docker compose exec nginx nginx -s reload`
# picks up the real cert without touching this script again.
set -e

CERT_PATH=$(grep -oE '/etc/letsencrypt/live/[^;]+/fullchain\.pem' /etc/nginx/conf.d/default.conf | head -1)

if [ -n "$CERT_PATH" ] && [ ! -f "$CERT_PATH" ]; then
    CERT_DIR=$(dirname "$CERT_PATH")
    DOMAIN=$(basename "$CERT_DIR")
    echo "[nginx-bootstrap] No TLS certificate for $DOMAIN yet — generating a temporary self-signed one so nginx can start."
    echo "[nginx-bootstrap] Run certbot for a real certificate, then: docker compose exec nginx nginx -s reload"
    mkdir -p "$CERT_DIR"
    openssl req -x509 -nodes -newkey rsa:2048 -days 1 \
        -keyout "$CERT_DIR/privkey.pem" \
        -out "$CERT_DIR/fullchain.pem" \
        -subj "/CN=$DOMAIN"
fi

exec nginx -g "daemon off;"
