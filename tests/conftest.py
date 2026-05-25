"""
conftest.py
===========
Setup global de tests.

Para que los tests corran SIN una BD viva ni un .env real, seteamos las
variables minimas obligatorias en os.environ ANTES de que pydantic-settings
las lea (al importar app.core.config).

Tests que necesitan BD se marcan @pytest.mark.integration y se saltan si
no hay DATABASE_URL apuntando a una BD real.
"""

from __future__ import annotations

import base64
import os
import sys
from pathlib import Path

# Asegurar que el paquete `app` sea importable cuando pytest corre desde la raiz.
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


# ── Defaults para tests unitarios (sin BD viva) ──────────────────────────────
# Solo seteamos lo minimo que pydantic-settings exige obligatorio (ENV,
# DATABASE_URL, AES_KEY). El resto toma defaults.

os.environ.setdefault("ENV", "dev")
os.environ.setdefault(
    "DATABASE_URL",
    "postgresql+psycopg://test:test@localhost:5432/test_unit",
)
os.environ.setdefault(
    "AES_KEY",
    base64.b64encode(b"\x00" * 32).decode("ascii"),
)
os.environ.setdefault("FINANZAS_BASE_URL", "https://finanzas.local.test")
os.environ.setdefault("FINANZAS_API_KEY", "fz_test_dummy_key_for_unit_tests_only")
os.environ.setdefault("PUBLIC_BASE_URL", "http://localhost:8005")
os.environ.setdefault("LOG_LEVEL", "WARNING")
