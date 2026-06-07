"""
config.py
=========
Configuracion global del Centro de Mensajes. Lee variables de entorno via
pydantic-settings y las expone como un objeto `settings` inmutable.

Variables requeridas en .env:
  - DATABASE_URL          PostgreSQL async (postgresql+psycopg://...)
  - AES_KEY               Clave AES-256 (32 bytes en base64) para cifrar
                          credenciales de proveedores en BD.
  - FINANZAS_BASE_URL     URL base del Finanzas-Core (https://finanzas.inovaweb.com.mx).
  - FINANZAS_API_KEY      API key con scope ledger:write para reportar despachos.
  - PUBLIC_BASE_URL       URL publica del centro (para pixel/click tracking).
  - PORT                  Puerto del servicio (default 8001).
  - ENV                   dev | staging | prod.
  - LOG_LEVEL             DEBUG | INFO | WARNING | ERROR.
"""

from functools import lru_cache
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # Base de datos
    database_url: str = Field(..., alias="DATABASE_URL")

    # Cifrado de credenciales de proveedores externos
    aes_key: str = Field(..., alias="AES_KEY")

    # Integracion downstream con el Finanzas-Core (ledger consolidado)
    finanzas_base_url: str = Field(
        "https://finanzas.inovaweb.com.mx", alias="FINANZAS_BASE_URL"
    )
    finanzas_api_key: str = Field("", alias="FINANZAS_API_KEY")

    # Arquitectura Inovaweb (D2): el CAF (Nivel 2) es el UNICO que contabiliza
    # en el Finanzas-Core. Cuando esta en False (default), el Centro de Mensajes
    # NO auto-reporta asientos al ledger; sigue enviando y contando mensajes
    # igual. Ponerlo en True solo si se quiere el comportamiento legacy de
    # auto-reporte directo al finanzas-core.
    report_to_finanzas: bool = Field(False, alias="REPORT_TO_FINANZAS")

    # URL publica del centro (usada para pixel/click tracking en correos).
    # En produccion: https://mensajes.inovaweb.com.mx
    public_base_url: str = Field(
        "https://mensajes.inovaweb.com.mx", alias="PUBLIC_BASE_URL"
    )

    # Servicio
    port: int = Field(8001, alias="PORT")
    # ENV sin default: el operador DEBE setearlo explicito para evitar
    # despliegues que queden con defaults laxos de dev.
    env: Literal["dev", "staging", "prod"] = Field(..., alias="ENV")
    log_level: str = Field("INFO", alias="LOG_LEVEL")

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )


@lru_cache
def get_settings() -> Settings:
    """Singleton de Settings."""
    return Settings()


settings = get_settings()
