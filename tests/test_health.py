"""
test_health.py
==============
GET /health debe responder 200 sin BD.
GET /health/db requiere BD viva (skipped si no esta disponible).
"""

from fastapi.testclient import TestClient


def test_health_liveness():
    from app.main import app
    with TestClient(app) as client:
        resp = client.get("/health")
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["status"] == "ok"
    assert data["service"] == "centro-mensajes"


def test_openapi_json_publico():
    from app.main import app
    with TestClient(app) as client:
        resp = client.get("/openapi.json")
    assert resp.status_code == 200
    spec = resp.json()
    assert spec["info"]["title"] == "Inovaweb Centro de Mensajes"
    # Confirmar que los paths principales estan registrados.
    paths = set(spec["paths"].keys())
    for expected in [
        "/health",
        "/health/db",
        "/v1/messages/email",
        "/v1/messages/whatsapp",
        "/v1/messages/sms",
        "/v1/messages",
        "/v1/messages/{message_id}",
        "/v1/reports/usage",
        "/admin/v1/templates",
        "/webhooks/{provider_slug}",
        "/v1/track/email/open/{message_id}",
    ]:
        assert expected in paths, f"missing path in openapi: {expected}"
