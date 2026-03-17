"""
Datos de test y mock helpers compartidos.

Importar desde aquí en los test files:
    from tests.helpers import VALID_PARAMS, mock_http_client
"""

import json
from unittest.mock import AsyncMock, MagicMock


# ── Datos de test ─────────────────────────────────────────────────────────────

VALID_PARAMS = {
    "project_name": "web-prod",
    "region": "europe-west1",
    "instance_type": "e2-standard-4",
    "purpose": "web server",
}
VALID_JSON_STR = json.dumps(VALID_PARAMS)


# ── Mock helpers ──────────────────────────────────────────────────────────────

def mock_http_client(response_text: str):
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


def mock_ollama_unreachable():
    """Mock de cliente que falla al llamar a /api/tags."""
    mock_client = AsyncMock()
    mock_client.get = AsyncMock(side_effect=Exception("Connection refused"))
    return mock_client


def mock_ollama_model_not_loaded():
    """Mock de cliente donde Ollama responde pero el modelo no está cargado."""
    tags_response = MagicMock()
    tags_response.json.return_value = {"models": [{"name": "llama2:latest"}]}
    tags_response.raise_for_status = MagicMock()

    mock_client = AsyncMock()
    mock_client.get = AsyncMock(return_value=tags_response)
    return mock_client


def mock_http_client_with_retries(fail_times: int, fail_exc, response_text: str):
    """
    Mock que falla fail_times veces con fail_exc, luego devuelve OK.
    Útil para testear retry con exponential backoff.
    """
    mock_response = MagicMock()
    mock_response.json.return_value = {"response": response_text}
    mock_response.raise_for_status = MagicMock()

    side_effects = [fail_exc] * fail_times + [mock_response]

    mock_client = AsyncMock()
    mock_client.post = AsyncMock(side_effect=side_effects)
    return mock_client
