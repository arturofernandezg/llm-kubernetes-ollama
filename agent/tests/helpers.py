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


def mock_chroma_client():
    """Mock de chromadb.HttpClient con resultados preconfigurados."""
    mock_client = MagicMock()
    collection = MagicMock()
    collection.count.return_value = 0  # incidents empty
    collection.query.return_value = {
        "ids": [["runbook-oomkilled-001"]],
        "documents": [["OOMKilled runbook text"]],
        "distances": [[0.1]],
        "metadatas": [[{"error_class": "OOMKilled", "service": "kubernetes"}]],
    }
    mock_client.get_or_create_collection.return_value = collection
    return mock_client


def mock_rag_context() -> dict:
    """Contexto RAG de ejemplo para tests de diagnosis."""
    return {
        "query": "OOMKilled pod engine-pod namespace prod",
        "runbooks": [
            {
                "id": "runbook-oomkilled-001",
                "document": "OOMKilled runbook text",
                "distance": 0.1,
                "metadata": {"error_class": "OOMKilled", "service": "kubernetes"},
            }
        ],
        "incidents": [],
    }


def mock_diagnosis_result() -> dict:
    """Resultado de diagnosis de ejemplo para tests."""
    return {
        "diagnosis": "Container exceeded memory limit",
        "commands": ["kubectl describe pod engine-pod -n prod", "kubectl top pod -n prod"],
        "confidence": 0.85,
        "risk": "high",
        "explanation": "The container was OOMKilled due to exceeding its memory limit.",
        "model_used": "tinyllama",
        "duration_ms": 1500,
        "rag_sources": ["runbook-oomkilled-001"],
        "raw_response": '{"diagnosis": "Container exceeded memory limit"}',
    }


def mock_diagnosis_auto_remediate() -> dict:
    """Diagnóstico que debe resultar en AUTO_REMEDIATE: risk=low, confidence=0.9, comandos seguros."""
    return {
        "diagnosis": "Container exceeded memory limit",
        "commands": [
            "kubectl describe pod engine-pod -n prod",
            "kubectl set resources deployment engine --limits=memory=512Mi -n prod",
        ],
        "confidence": 0.9,
        "risk": "low",
        "explanation": "Memory limit too low, safe to increase.",
        "model_used": "tinyllama",
        "duration_ms": 1200,
        "rag_sources": ["runbook-oomkilled-001"],
        "raw_response": "{}",
    }


def mock_diagnosis_escalate() -> dict:
    """Diagnóstico que debe resultar en ESCALATE: incluye kubectl drain (BLOCKED)."""
    return {
        "diagnosis": "Node is under pressure",
        "commands": [
            "kubectl get nodes",
            "kubectl drain node-1 --ignore-daemonsets",
        ],
        "confidence": 0.9,
        "risk": "high",
        "explanation": "Node needs draining.",
        "model_used": "tinyllama",
        "duration_ms": 1800,
        "rag_sources": [],
        "raw_response": "{}",
    }


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
