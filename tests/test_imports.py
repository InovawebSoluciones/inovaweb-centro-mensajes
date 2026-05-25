"""
test_imports.py
===============
Confirma que todos los modulos del proyecto importan sin error.
Detecta typos, imports rotos y errores de sintaxis en CI sin necesidad de BD.
"""


def test_app_main_imports():
    import app.main as m
    assert hasattr(m, "app"), "app.main debe exponer una variable `app` (FastAPI instance)"


def test_core_modules():
    import app.core.config  # noqa: F401
    import app.core.crypto  # noqa: F401
    import app.core.database  # noqa: F401
    import app.core.observability  # noqa: F401
    import app.core.api_key_auth  # noqa: F401
    import app.core.ledger_client  # noqa: F401


def test_providers():
    from app.providers.factory import build_provider, supported_slugs
    slugs = supported_slugs()
    assert "resend" in slugs
    assert "sendgrid" in slugs
    assert "meta_whatsapp" in slugs
    assert "twilio" in slugs


def test_routers():
    from app.routers.health_router import router as h
    from app.routers.messages_router import router as m
    from app.routers.templates_router import router as t
    from app.routers.credentials_router import router as c
    from app.routers.webhooks_router import router as w
    from app.routers.tracking_router import router as tr
    # Sanity: cada router expone al menos una ruta.
    for r in (h, m, t, c, w, tr):
        assert len(r.routes) >= 1


def test_workers():
    import app.workers.ledger_retry as r
    assert callable(r.retry_loop_forever)
    assert callable(r.run_retry_once)
