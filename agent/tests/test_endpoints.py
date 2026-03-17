"""
Tests de los endpoints del agente AIOps.

Cubre: GET /healthz, GET /readyz, GET /health, POST /extract.
Todos los tests usan mocks de Ollama (no requieren cluster ni LLM).
"""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import httpx as _httpx

from main import app
from tests.helpers import (
    VALID_PARAMS, VALID_JSON_STR,
    mock_http_client, mock_ollama_unreachable, mock_ollama_model_not_loaded,
    mock_http_client_with_retries,
)


# ── GET /healthz ──────────────────────────────────────────────────────────────

class TestHealthzEndpoint:
    """Liveness probe: siempre 200, sin dependencias."""

    def test_healthz_always_200(self, api_client):
        r = api_client.get("/healthz")
        assert r.status_code == 200
        assert r.json() == {"status": "alive"}

    def test_healthz_200_even_if_ollama_down(self, api_client):
        with patch.object(app.state, "http_client", mock_ollama_unreachable()):
            r = api_client.get("/healthz")
        assert r.status_code == 200


# ── GET /readyz ───────────────────────────────────────────────────────────────

class TestReadyzEndpoint:
    """Readiness probe: 200 si Ollama + modelo OK, 503 si no."""

    def test_readyz_200_when_ollama_and_model_ok(self, api_client):
        with patch.object(app.state, "http_client", mock_http_client("")):
            r = api_client.get("/readyz")
        assert r.status_code == 200
        data = r.json()
        assert data["status"] == "ready"
        assert data["model_loaded"] is True

    def test_readyz_503_when_ollama_unreachable(self, api_client):
        with patch.object(app.state, "http_client", mock_ollama_unreachable()):
            r = api_client.get("/readyz")
        assert r.status_code == 503
        assert "Ollama unreachable" in r.json()["detail"]

    def test_readyz_503_when_model_not_loaded(self, api_client):
        with patch.object(app.state, "http_client", mock_ollama_model_not_loaded()):
            r = api_client.get("/readyz")
        assert r.status_code == 503
        assert "not loaded" in r.json()["detail"]


# ── GET /health ───────────────────────────────────────────────────────────────

class TestHealthEndpoint:

    def test_health_ok_when_ollama_available(self, api_client):
        with patch.object(app.state, "http_client", mock_http_client("")):
            r = api_client.get("/health")
        assert r.status_code == 200
        data = r.json()
        assert data["status"] == "ok"
        assert data["model_loaded"] is True
        assert "available_models" in data

    def test_health_503_when_ollama_unreachable(self, api_client):
        with patch.object(app.state, "http_client", mock_ollama_unreachable()):
            r = api_client.get("/health")
        assert r.status_code == 503
        assert "Ollama unreachable" in r.json()["detail"]


# ── POST /extract ─────────────────────────────────────────────────────────────

class TestExtractEndpoint:

    def test_success_direct_json(self, api_client):
        with patch.object(app.state, "http_client", mock_http_client(VALID_JSON_STR)):
            r = api_client.post("/extract", json={"message": "Servidor web-prod en europe-west1"})
        assert r.status_code == 200
        data = r.json()
        assert data["extracted_parameters"] == VALID_PARAMS
        assert data["extraction_method"] == "direct"
        assert data["validation_warnings"] == []
        assert len(data["request_id"]) == 8
        assert data["duration_ms"] >= 0
        assert data["model_used"] is not None

    def test_success_markdown_response(self, api_client):
        markdown = f"Aquí tienes:\n```json\n{VALID_JSON_STR}\n```"
        with patch.object(app.state, "http_client", mock_http_client(markdown)):
            r = api_client.post("/extract", json={"message": "Test"})
        assert r.status_code == 200
        assert r.json()["extraction_method"] == "markdown_block"

    def test_success_regex_fallback(self, api_client):
        prose = f"El resultado sería {VALID_JSON_STR} según los parámetros indicados."
        with patch.object(app.state, "http_client", mock_http_client(prose)):
            r = api_client.post("/extract", json={"message": "Test"})
        assert r.status_code == 200
        assert r.json()["extraction_method"] == "regex_search"

    def test_no_json_in_response(self, api_client):
        with patch.object(app.state, "http_client", mock_http_client("No entiendo la petición.")):
            r = api_client.post("/extract", json={"message": "Test"})
        assert r.status_code == 200
        data = r.json()
        assert data["extracted_parameters"] is None
        assert len(data["validation_warnings"]) > 0

    def test_raw_response_preserved(self, api_client):
        with patch.object(app.state, "http_client", mock_http_client(VALID_JSON_STR)):
            r = api_client.post("/extract", json={"message": "Test"})
        assert r.json()["raw_response"] == VALID_JSON_STR

    def test_validation_warning_for_unknown_region(self, api_client):
        params = {**VALID_PARAMS, "region": "zona-inventada-1"}
        with patch.object(app.state, "http_client", mock_http_client(json.dumps(params))):
            r = api_client.post("/extract", json={"message": "Test"})
        assert r.status_code == 200
        assert any("region" in w for w in r.json()["validation_warnings"])

    # Validación de input
    def test_empty_message_returns_422(self, api_client):
        assert api_client.post("/extract", json={"message": ""}).status_code == 422

    def test_whitespace_only_message_returns_422(self, api_client):
        assert api_client.post("/extract", json={"message": "   "}).status_code == 422

    def test_message_too_long_returns_422(self, api_client):
        assert api_client.post("/extract", json={"message": "x" * 2001}).status_code == 422

    def test_missing_message_field_returns_422(self, api_client):
        assert api_client.post("/extract", json={}).status_code == 422

    # Errores de Ollama
    def test_ollama_timeout_returns_504(self, api_client):
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(side_effect=_httpx.TimeoutException("timeout"))
        with patch.object(app.state, "http_client", mock_client):
            r = api_client.post("/extract", json={"message": "Test"})
        assert r.status_code == 504
        assert "timeout" in r.json()["detail"].lower()

    def test_ollama_connection_error_returns_502(self, api_client):
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(side_effect=_httpx.ConnectError("Connection refused"))
        with patch.object(app.state, "http_client", mock_client):
            r = api_client.post("/extract", json={"message": "Test"})
        assert r.status_code == 502

    def test_ollama_http_status_error_returns_502(self, api_client):
        mock_response = MagicMock()
        mock_response.status_code = 500
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(
            side_effect=_httpx.HTTPStatusError(
                "Server Error", request=MagicMock(), response=mock_response
            )
        )
        with patch.object(app.state, "http_client", mock_client):
            r = api_client.post("/extract", json={"message": "Test"})
        assert r.status_code == 502


# ── Retry con exponential backoff ─────────────────────────────────────────────

class TestRetryBehavior:
    """Tests del retry con exponential backoff hacia Ollama."""

    def test_retry_succeeds_after_connect_error(self, api_client):
        """Falla 1 vez con ConnectError, luego OK → 200."""
        mock_client = mock_http_client_with_retries(
            fail_times=1,
            fail_exc=_httpx.ConnectError("Connection refused"),
            response_text=VALID_JSON_STR,
        )
        with patch.object(app.state, "http_client", mock_client):
            r = api_client.post("/extract", json={"message": "Test retry"})
        assert r.status_code == 200
        assert r.json()["extracted_parameters"] == VALID_PARAMS
        assert mock_client.post.call_count == 2

    def test_retry_succeeds_after_timeout(self, api_client):
        """Falla 1 vez con TimeoutException, luego OK → 200."""
        mock_client = mock_http_client_with_retries(
            fail_times=1,
            fail_exc=_httpx.TimeoutException("timeout"),
            response_text=VALID_JSON_STR,
        )
        with patch.object(app.state, "http_client", mock_client):
            r = api_client.post("/extract", json={"message": "Test retry"})
        assert r.status_code == 200
        assert mock_client.post.call_count == 2

    def test_retry_exhausted_timeout_returns_504(self, api_client):
        """Todos los intentos fallan con TimeoutException → 504."""
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(side_effect=_httpx.TimeoutException("timeout"))
        with patch.object(app.state, "http_client", mock_client):
            r = api_client.post("/extract", json={"message": "Test"})
        assert r.status_code == 504
        assert mock_client.post.call_count == 3  # retry_max_attempts default

    def test_retry_exhausted_connect_error_returns_502(self, api_client):
        """Todos los intentos fallan con ConnectError → 502."""
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(side_effect=_httpx.ConnectError("refused"))
        with patch.object(app.state, "http_client", mock_client):
            r = api_client.post("/extract", json={"message": "Test"})
        assert r.status_code == 502
        assert "3 attempts" in r.json()["detail"]

    def test_no_retry_on_http_status_error(self, api_client):
        """HTTPStatusError (500) → fallo inmediato sin retry."""
        mock_response = MagicMock()
        mock_response.status_code = 500
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(
            side_effect=_httpx.HTTPStatusError(
                "Server Error", request=MagicMock(), response=mock_response
            )
        )
        with patch.object(app.state, "http_client", mock_client):
            r = api_client.post("/extract", json={"message": "Test"})
        assert r.status_code == 502
        assert mock_client.post.call_count == 1  # sin retry
