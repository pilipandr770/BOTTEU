"""
Tests for app/__init__.py — app factory behavior.

test_health_endpoint_is_never_rate_limited guards against a real incident:
Docker's HEALTHCHECK polls /health every 30s (120/hour) from one source IP.
RATELIMIT_DEFAULT is "50 per hour" and applies globally with no exemption,
so the health check itself got 429'd ~25 minutes after every container
start. Docker then marked the container unhealthy, and Traefik stopped
routing to it entirely — the site went down while the app was fine
internally the whole time.
"""
import os

os.environ.setdefault("SECRET_KEY", "test-secret-key")
os.environ.setdefault("FERNET_KEY", "ZmVybmV0a2V5Zm9ydGVzdGluZ29ubHkxMjM0NTY3OA==")

from app import create_app


def test_health_endpoint_is_never_rate_limited():
    app = create_app("testing")
    client = app.test_client()

    # RATELIMIT_DEFAULT is "50 per hour" — well more than double that here,
    # all from the same client, must all succeed if /health is truly exempt.
    for _ in range(120):
        resp = client.get("/health")
        assert resp.status_code == 200, "docker HEALTHCHECK must never be rate-limited"
