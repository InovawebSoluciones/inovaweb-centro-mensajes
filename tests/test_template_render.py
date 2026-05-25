"""
test_template_render.py
=======================
Verifica que el renderizador de plantillas NO permite:
  - acceso a atributos via {x.__class__}
  - acceso a subscripts via {x[clave]}
  - format specs via {x:width}

Y SI hace lo que debe:
  - sustitucion plana de {nombre}
  - dejar literal {nombre_no_existe} si la variable no esta
  - validar tipos contra el schema declarado
"""

import pytest

from app.core.template_render import (
    TemplateError,
    render_template,
    validate_schema,
    validate_variables,
)


# ── Render seguro: solo sustitucion plana ────────────────────────────────────

def test_render_sustituye_variable_simple():
    out = render_template("Hola {nombre}, gracias", {"nombre": "Maria"})
    assert out == "Hola Maria, gracias"


def test_render_deja_literal_si_variable_no_existe():
    out = render_template("Hola {nombre}, {ausente}", {"nombre": "Pedro"})
    assert out == "Hola Pedro, {ausente}"


def test_render_template_none_devuelve_none():
    assert render_template(None, {"x": "y"}) is None


def test_render_acceso_atributos_se_ignora():
    # CRITICAL audit fix: format_map permitia {x.__class__}; ahora se ignora.
    out = render_template("X={x.__class__}", {"x": "hola"})
    # El placeholder con punto NO matchea la regex segura, queda literal.
    assert out == "X={x.__class__}"


def test_render_acceso_subscript_se_ignora():
    out = render_template("X={x[clave]}", {"x": {"clave": "valor"}})
    assert out == "X={x[clave]}"


def test_render_format_spec_se_ignora():
    out = render_template("X={n:>10}", {"n": 42})
    assert out == "X={n:>10}"


def test_render_nombre_con_chars_invalidos_se_ignora():
    # Numero al inicio, espacio, simbolo: ninguno matchea.
    for bad in ["{1var}", "{var name}", "{var-name}", "{var!s}", "{}"]:
        out = render_template(f"X={bad}", {"1var": "x", "var name": "x", "var-name": "x"})
        assert bad in out, f"{bad} no deberia haber sido sustituido"


def test_render_value_none_es_string_vacio():
    out = render_template("X={n}", {"n": None})
    assert out == "X="


def test_render_value_se_castea_a_str():
    out = render_template("X={n}", {"n": 42})
    assert out == "X=42"


def test_render_rechaza_template_no_str():
    with pytest.raises(TemplateError):
        render_template(123, {})  # type: ignore[arg-type]


# ── validate_schema ───────────────────────────────────────────────────────────

def test_validate_schema_vacio_es_ok():
    assert validate_schema(None) == {}
    assert validate_schema({}) == {}


def test_validate_schema_tipos_validos():
    schema = {"a": "string", "b": "integer", "c": "boolean"}
    assert validate_schema(schema) == schema


def test_validate_schema_normaliza_mayusculas():
    assert validate_schema({"x": "STRING"}) == {"x": "string"}


def test_validate_schema_rechaza_tipo_invalido():
    with pytest.raises(TemplateError):
        validate_schema({"x": "bigint"})


def test_validate_schema_rechaza_nombre_con_punto():
    with pytest.raises(TemplateError):
        validate_schema({"a.b": "string"})


def test_validate_schema_rechaza_no_dict():
    with pytest.raises(TemplateError):
        validate_schema("not-a-dict")  # type: ignore[arg-type]


# ── validate_variables ────────────────────────────────────────────────────────

def test_validate_variables_happy_path():
    validate_variables({"n": "integer", "s": "string"}, {"n": 1, "s": "x"})


def test_validate_variables_falta_requerida():
    with pytest.raises(TemplateError) as exc:
        validate_variables({"n": "integer"}, {})
    assert "faltante" in str(exc.value).lower()


def test_validate_variables_tipo_incorrecto():
    with pytest.raises(TemplateError) as exc:
        validate_variables({"n": "integer"}, {"n": "no soy int"})
    assert "tipo invalido" in str(exc.value).lower()


def test_validate_variables_boolean_no_es_integer():
    # En Python `True` es subtipo de int; la validacion debe distinguir.
    with pytest.raises(TemplateError):
        validate_variables({"n": "integer"}, {"n": True})


def test_validate_variables_extras_se_ignoran():
    # Variables no declaradas en schema no rompen.
    validate_variables({"n": "integer"}, {"n": 1, "extra": "ok"})
