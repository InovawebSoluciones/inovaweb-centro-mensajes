"""
test_messages_validation.py
===========================
Validacion pydantic de los bodies de despacho. No requiere BD.
"""

import pytest
from pydantic import ValidationError

from app.routers.messages_router import EmailIn, SmsIn, WhatsappIn


# ── Email ─────────────────────────────────────────────────────────────────────

def test_email_template_ok():
    body = EmailIn.model_validate({
        "app_id": "webescolar",
        "client_id": "escuela-123",
        "service_id": "boleta-mensual",
        "origin_kind": "template",
        "template_id": "tpl-boleta-mensual",
        "from": {"email": "noreply@inovaweb.com.mx", "name": "Inovaweb"},
        "to": {"email": "padre@ejemplo.com", "name": "Maria"},
        "variables": {"alumno": "Juan"},
    })
    assert body.origin_kind == "template"
    assert body.template_id == "tpl-boleta-mensual"


def test_email_ai_generated_ok():
    body = EmailIn.model_validate({
        "origin_kind": "ai_generated",
        "subject": "Hola",
        "body_html": "<p>x</p>",
        "from": {"email": "envios@inovaweb.com.mx"},
        "to": {"email": "dest@ejemplo.com"},
    })
    assert body.origin_kind == "ai_generated"


def test_email_template_sin_template_id_falla():
    with pytest.raises(ValidationError) as exc:
        EmailIn.model_validate({
            "origin_kind": "template",
            "from": {"email": "x@y.com"},
            "to": {"email": "z@y.com"},
        })
    assert "template_id" in str(exc.value)


def test_email_ai_generated_sin_subject_falla():
    with pytest.raises(ValidationError) as exc:
        EmailIn.model_validate({
            "origin_kind": "ai_generated",
            "body_html": "<p>x</p>",
            "from": {"email": "x@y.com"},
            "to": {"email": "z@y.com"},
        })
    assert "subject" in str(exc.value)


def test_email_ai_generated_sin_body_falla():
    with pytest.raises(ValidationError) as exc:
        EmailIn.model_validate({
            "origin_kind": "ai_generated",
            "subject": "Hi",
            "from": {"email": "x@y.com"},
            "to": {"email": "z@y.com"},
        })
    assert "body_html" in str(exc.value)


def test_email_origin_kind_invalido_falla():
    with pytest.raises(ValidationError):
        EmailIn.model_validate({
            "origin_kind": "other",
            "from": {"email": "x@y.com"},
            "to": {"email": "z@y.com"},
        })


def test_email_destinatario_invalido_falla():
    with pytest.raises(ValidationError):
        EmailIn.model_validate({
            "origin_kind": "ai_generated",
            "subject": "Hi",
            "body_text": "x",
            "from": {"email": "x@y.com"},
            "to": {"email": "no-es-email"},
        })


# ── WhatsApp ──────────────────────────────────────────────────────────────────

def test_whatsapp_e164_ok():
    body = WhatsappIn.model_validate({
        "template_id": "tpl-recordatorio",
        "from_phone_id": "+5215555000001",
        "to_phone": "+5215512345678",
        "variables": {"alumno": "Juan"},
    })
    assert body.to_phone == "+5215512345678"


def test_whatsapp_e164_invalido_falla():
    with pytest.raises(ValidationError):
        WhatsappIn.model_validate({
            "template_id": "tpl-x",
            "from_phone_id": "+5215555000001",
            "to_phone": "5215512345678",  # falta el +
        })


def test_whatsapp_sin_template_ni_mensaje_falla():
    with pytest.raises(ValidationError):
        WhatsappIn.model_validate({
            "from_phone_id": "+5215555000001",
            "to_phone": "+5215512345678",
        })


# ── SMS ────────────────────────────────────────────────────────────────────────

def test_sms_ok():
    body = SmsIn.model_validate({
        "from_phone_id": "+5215555000001",
        "to_phone": "+5215512345678",
        "message": "Su hijo registro entrada a las 07:42",
    })
    assert body.message.startswith("Su hijo")


def test_sms_e164_invalido_falla():
    with pytest.raises(ValidationError):
        SmsIn.model_validate({
            "from_phone_id": "5215555000001",
            "to_phone": "+5215512345678",
            "message": "X",
        })


def test_sms_mensaje_vacio_falla():
    with pytest.raises(ValidationError):
        SmsIn.model_validate({
            "from_phone_id": "+5215555000001",
            "to_phone": "+5215512345678",
            "message": "",
        })
