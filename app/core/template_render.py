"""
template_render.py
==================
Renderizador minimo y SEGURO para plantillas.

Reemplaza `str.format_map(_SafeDict(...))`, que permitia acceso a atributos
(`{var.__class__}`) y subscript (`{var[__class__]}`) — vector de exfiltracion
hacia el destinatario del correo.

Esta implementacion SOLO acepta lookups planos: `{nombre_variable}`. Cualquier
cosa con punto, corchete, ! o : dentro de las llaves se ignora literalmente
(no se sustituye). Si la variable no existe en `variables`, se preserva el
literal `{nombre}` (para diagnostico).

Tambien se incluye validacion contra `variables_schema` declarado al alta de
la plantilla. El schema es un dict simple {nombre: tipo} donde tipo es uno de:
  string | integer | number | boolean | array | object
"""

from __future__ import annotations

import re
from typing import Any


# Reconoce SOLO {nombre_variable} con caracteres seguros. Rechaza punto, [],
# !, : (format specs y attribute access).
_PLACEHOLDER_RE = re.compile(r"\{([a-zA-Z_][a-zA-Z0-9_]{0,127})\}")

# Tipos permitidos en variables_schema.
_SCHEMA_TYPES = frozenset({"string", "integer", "number", "boolean", "array", "object"})


class TemplateError(Exception):
    """Error de renderizado o validacion de variables."""


def render_template(template: str | None, variables: dict[str, Any] | None) -> str | None:
    """
    Sustituye `{var}` por `str(variables[var])`. Si `var` no existe, deja `{var}`.
    Nunca evalua atributos ni subscripts.

    Devuelve None si template es None (para que la cadena de helpers no truene
    cuando hay plantillas con solo HTML o solo texto).
    """
    if template is None:
        return None
    if not isinstance(template, str):
        raise TemplateError("template debe ser str o None")
    vars_dict = variables or {}

    def _sub(match: re.Match[str]) -> str:
        name = match.group(1)
        if name in vars_dict:
            value = vars_dict[name]
            # Forzar a str con guardia simple: no permitimos None silencioso.
            if value is None:
                return ""
            return str(value)
        return match.group(0)  # literal {nombre}

    return _PLACEHOLDER_RE.sub(_sub, template)


def validate_schema(schema: Any) -> dict[str, str]:
    """
    Valida y normaliza un variables_schema. Debe ser dict {nombre: tipo} donde
    tipo es uno de _SCHEMA_TYPES. Levanta TemplateError si invalido.
    Retorna el schema normalizado (claves str, valores en minusculas).
    """
    if schema is None:
        return {}
    if not isinstance(schema, dict):
        raise TemplateError("variables_schema debe ser objeto (dict).")
    normalized: dict[str, str] = {}
    for name, type_ in schema.items():
        if not isinstance(name, str) or not name:
            raise TemplateError(f"variables_schema: nombre invalido: {name!r}")
        if not _PLACEHOLDER_RE.fullmatch("{" + name + "}"):
            raise TemplateError(
                f"variables_schema: nombre de variable '{name}' contiene caracteres no permitidos"
            )
        if not isinstance(type_, str) or type_.lower() not in _SCHEMA_TYPES:
            raise TemplateError(
                f"variables_schema: tipo invalido para '{name}': {type_!r}. "
                f"Permitidos: {sorted(_SCHEMA_TYPES)}"
            )
        normalized[name] = type_.lower()
    return normalized


def validate_variables(
    schema: dict[str, str] | None,
    variables: dict[str, Any] | None,
) -> None:
    """
    Verifica que `variables` cumple con `schema`. Levanta TemplateError ante:
      - variable faltante (schema la declara pero variables no la trae)
      - tipo incorrecto (string vs int vs etc.)

    Variables EXTRA (no declaradas en schema) se ignoran silenciosamente,
    para permitir extender plantillas sin romper callers viejos.
    """
    if not schema:
        return
    vars_dict = variables or {}
    for name, expected in schema.items():
        if name not in vars_dict:
            raise TemplateError(f"variable requerida faltante: '{name}'")
        value = vars_dict[name]
        if not _matches_type(value, expected):
            raise TemplateError(
                f"variable '{name}' tipo invalido: esperado {expected}, "
                f"recibido {type(value).__name__}"
            )


def _matches_type(value: Any, expected: str) -> bool:
    """Mapea tipos JSON-like a tipos Python."""
    if expected == "string":
        return isinstance(value, str)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "array":
        return isinstance(value, list)
    if expected == "object":
        return isinstance(value, dict)
    return False
