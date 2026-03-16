"""
Tests del AIOps Infrastructure Agent.

Estructura:
    TestExtractJson      — tests unitarios de la función extract_json()
    TestValidateParams   — tests unitarios de validate_params()
    TestHealthzEndpoint  — liveness probe (sin dependencias externas)
    TestReadyzEndpoint   — readiness probe (verifica Ollama + modelo)
    TestHealthEndpoint   — health completo (retrocompatibilidad)
    TestExtractEndpoint  — tests de integración del endpoint POST /extract

Ejecutar:
    pip install -r requirements.txt -r requirements-dev.txt
    pytest tests/test_main.py -v
    pytest tests/test_main.py -v -k "extract_json"   # solo una clase
"""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import httpx as _httpx
import pytest
from fastapi.testclient import TestClient

from main import app, extract_json, validate_params

client = TestClient(app)

# ── Fixtures y helpers ─────────────────────────────────────────────────────────

VALID_PARAMS = {
    "project_name": "web-prod",
    "region": "europe-west1",
    "instance_type": "e2-standard-4",
    "purpose": "web server",
}
VALID_JSON_STR = json.dumps(VALID_PARAMS)


def _mock_http_client(response_text: str):
    """
    Crea un mock de httpx.AsyncClient compatible con app.state.http_client.
    Simula tanto POST /api/generate como GET /api/tags.
    """
    mock_response = MagicMock()
    mock_response.json.return_value = {"response": response_text}
    mock_response.raise_for_status = MagicMock()

    tags_response = MagicMock()
    tags_response.json.return_value = {"models": [{"name": "tinyllama:latest"}]}
    tags_response.raise_for_status = MagicMock()

    mock_client = AsyncMock()
    mock_client.post = AsyncMock(return_value=mock_response)
    mock_client.get = AsyncMock(return_value=tags_response)
    return mock_client


def _mock_ollama_unreachable():
    """Mock de cliente que falla al llamar a /api/tags."""
    mock_client = AsyncMock()
    mock_client.get = AsyncMock(side_effect=Exception("Connection refused"))
    return mock_client


def _mock_ollama_model_not_loaded():
    """Mock de cliente donde Ollama responde pero el modelo no está cargado."""
    tags_response = MagicMock()
    tags_response.json.return_value = {"models": [{"name": "llama2:latest"}]}
    tags_response.raise_for_status = MagicMock()

    mock_client = AsyncMock()
    mock_client.get = AsyncMock(return_value=tags_response)
    return mock_client


# ── Inicializar app.state.http_client para tests ─────────────────────────────
# El TestClient no ejecuta el lifespan, así que creamos un mock por defecto.
# Cada test que necesite un comportamiento específico lo sobreescribe con
# patch.object(app.state, "http_client", ...).
app.state.http_client = _mock_http_client("")


# ── extract_json ───────────────────────────────────────────────────────────────

class TestExtractJson:

    def test_direct_json(self):
        result, method = extract_json(VALID_JSON_STR)
        assert method == "direct"
        assert result == VALID_PARAMS

    def test_direct_json_with_surrounding_whitespace(self):
        result, method = extract_json(f"  {VALID_JSON_STR}  \n")
        assert method == "direct"
        assert result is not None

    def test_markdown_block_with_json_tag(self):
        text = f"Aquí tienes el resultado:\n```json\n{VALID_JSON_STR}\n```"
        result, method = extract_json(text)
        assert method == "markdown_block"
        assert result["region"] == "europe-west1"

    def test_markdown_block_without_json_tag(self):
        text = f"Resultado:\n```\n{VALID_JSON_STR}\n```"
        result, method = extract_json(text)
        assert method == "markdown_block"
        assert result is not None

    def test_regex_fallback(self):
        text = f"El objeto JSON sería {VALID_JSON_STR} espero que ayude."
        result, method = extract_json(text)
        assert method == "regex_search"
        assert result["purpose"] == "web server"

    def test_no_json_returns_none(self):
        result, method = extract_json("No hay JSON aquí, solo texto plano.")
        assert result is None
        assert method is None

    def test_malformed_json_returns_none(self):
        result, method = extract_json('{"project_name": "test", "region":}')
        assert result is None
        assert method is None

    def test_empty_string_returns_none(self):
        result, method = extract_json("")
        assert result is None
        assert method is None

    def test_prefers_direct_over_regex(self):
        result, method = extract_json(VALID_JSON_STR)
        assert method == "direct"


# ── validate_params ────────────────────────────────────────────────────────────

class TestValidateParams:

    def test_all_valid_params_no_warnings(self):
        assert validate_params(VALID_PARAMS) == []

    def test_invalid_region_generates_warning(self):
        params = {**VALID_PARAMS, "region": "marte-1"}
        warnings = validate_params(params)
        assert any("region" in w.lower() for w in warnings)

    def test_invalid_instance_type_generates_warning(self):
        params = {**VALID_PARAMS, "instance_type": "grande"}
        warnings = validate_params(params)
        assert any("instance type" in w.lower() for w in warnings)

    def test_null_region_generates_missing_warning(self):
        params = {**VALID_PARAMS, "region": None}
        warnings = validate_params(params)
        assert any("region" in w.lower() for w in warnings)

    def test_all_null_generates_four_warnings(self):
        params = {k: None for k in VALID_PARAMS}
        assert len(validate_params(params)) == 4

    @pytest.mark.parametrize("instance", [
        "e2-standard-4", "n1-standard-2", "n2-standard-8",
        "n2d-standard-4", "c2-standard-4", "t2d-standard-1",
    ])
    def test_valid_instance_prefixes_no_warning(self, instance):
        params = {**VALID_PARAMS, "instance_type": instance}
        warnings = validate_params(params)
        assert not any("instance type" in w.lower() for w in warnings)


# ── GET /healthz ───────────────────────────────────────────────────────────────

class TestHealthzEndpoint:
    """Liveness probe: siempre 200, sin dependencias."""

    def test_healthz_always_200(self):
        r = client.get("/healthz")
        assert r.status_code == 200
        assert r.json() == {"status": "alive"}

    def test_healthz_200_even_if_ollama_down(self):
        with patch.object(app.state, "http_client", _mock_ollama_unreachable()):
            r = client.get("/healthz")
        assert r.status_code == 200


# ── GET /readyz ────────────────────────────────────────────────────────────────

class TestReadyzEndpoint:
    """Readiness probe: 200 si Ollama + modelo OK, 503 si no."""

    def test_readyz_200_when_ollama_and_model_ok(self):
        with patch.object(app.state, "http_client", _mock_http_client("")):
            r = client.get("/readyz")
        assert r.status_code == 200
        data = r.json()
        assert data["status"] == "ready"
        assert data["model_loaded"] is True

    def test_readyz_503_when_ollama_unreachable(self):
        with patch.object(app.state, "http_client", _mock_ollama_unreachable()):
            r = client.get("/readyz")
        assert r.status_code == 503
        assert "Ollama unreachable" in r.json()["detail"]

    def test_readyz_503_when_model_not_loaded(self):
        with patch.object(app.state, "http_client", _mock_ollama_model_not_loaded()):
            r = client.get("/readyz")
        assert r.status_code == 503
        assert "not loaded" in r.json()["detail"]


# ── GET /health ────────────────────────────────────────────────────────────────

class TestHealthEndpoint:

    def test_health_ok_when_ollama_available(self):
        with patch.object(app.state, "http_client", _mock_http_client("")):
            r = client.get("/health")
        assert r.status_code == 200
        data = r.json()
        assert data["status"] == "ok"
        assert data["model_loaded"] is True
        assert "available_models" in data

    def test_health_503_when_ollama_unreachable(self):
        with patch.object(app.state, "http_client", _mock_ollama_unreachable()):
            r = client.get("/health")
        assert r.status_code == 503
        assert "Ollama unreachable" in r.json()["detail"]


# ── POST /extract ──────────────────────────────────────────────────────────────

class TestExtractEndpoint:

    def test_success_direct_json(self):
        with patch.object(app.state, "http_client", _mock_http_client(VALID_JSON_STR)):
            r = client.post("/extract", json={"message": "Servidor web-prod en europe-west1"})
        assert r.status_code == 200
        data = r.json()
        assert data["extracted_parameters"] == VALID_PARAMS
        assert data["extraction_method"] == "direct"
        assert data["validation_warnings"] == []
        assert len(data["request_id"]) == 8
        assert data["duration_ms"] >= 0
        assert data["model_used"] is not None

    def test_success_markdown_response(self):
        markdown = f"Aquí tienes:\n```json\n{VALID_JSON_STR}\n```"
        with patch.object(app.state, "http_client", _mock_http_client(markdown)):
            r = client.post("/extract", json={"message": "Test"})
        assert r.status_code == 200
        assert r.json()["extraction_method"] == "markdown_block"

    def test_success_regex_fallback(self):
        prose = f"El resultado sería {VALID_JSON_STR} según los parámetros indicados."
        with patch.object(app.state, "http_client", _mock_http_client(prose)):
            r = client.post("/extract", json={"message": "Test"})
        assert r.status_code == 200
        assert r.json()["extraction_method"] == "regex_search"

    def test_no_json_in_response(self):
        with patch.object(app.state, "http_client", _mock_http_client("No entiendo la petición.")):
            r = client.post("/extract", json={"message": "Test"})
        assert r.status_code == 200
        data = r.json()
        assert data["extracted_parameters"] is None
        assert len(data["validation_warnings"]) > 0

    def test_raw_response_preserved(self):
        with patch.object(app.state, "http_client", _mock_http_client(VALID_JSON_STR)):
            r = client.post("/extract", json={"message": "Test"})
        assert r.json()["raw_response"] == VALID_JSON_STR

    def test_validation_warning_for_unknown_region(self):
        params = {**VALID_PARAMS, "region": "zona-inventada-1"}
        with patch.object(app.state, "http_client", _mock_http_client(json.dumps(params))):
            r = client.post("/extract", json={"message": "Test"})
        assert r.status_code == 200
        assert any("region" in w for w in r.json()["validation_warnings"])

    # Validación de input
    def test_empty_message_returns_422(self):
        assert client.post("/extract", json={"message": ""}).status_code == 422

    def test_whitespace_only_message_returns_422(self):
        assert client.post("/extract", json={"message": "   "}).status_code == 422

    def test_message_too_long_returns_422(self):
        assert client.post("/extract", json={"message": "x" * 2001}).status_code == 422

    def test_missing_message_field_returns_422(self):
        assert client.post("/extract", json={}).status_code == 422

    # Errores de Ollama
    def test_ollama_timeout_returns_504(self):
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(side_effect=_httpx.TimeoutException("timeout"))
        with patch.object(app.state, "http_client", mock_client):
            r = client.post("/extract", json={"message": "Test"})
        assert r.status_code == 504
        assert "timeout" in r.json()["detail"].lower()

    def test_ollama_connection_error_returns_502(self):
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(side_effect=_httpx.ConnectError("Connection refused"))
        with patch.object(app.state, "http_client", mock_client):
            r = client.post("/extract", json={"message": "Test"})
        assert r.status_code == 502

    def test_ollama_http_status_error_returns_502(self):
        mock_response = MagicMock()
        mock_response.status_code = 500
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(
            side_effect=_httpx.HTTPStatusError(
                "Server Error", request=MagicMock(), response=mock_response
            )
        )
        with patch.object(app.state, "http_client", mock_client):
            r = client.post("/extract", json={"message": "Test"})
        assert r.status_code == 502
