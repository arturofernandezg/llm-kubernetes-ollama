"""
Tests for the diagnosis module (diagnosis.py).

All tests mock Ollama — no LLM or cluster needed.
"""

import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from diagnosis import (
    build_alert_text,
    format_context_docs,
    generate_diagnosis,
    _clamp,
)


# ── Test data ─────────────────────────────────────────────────────────────────

SAMPLE_LABELS = {
    "alertname": "KubePodOOMKilled",
    "pod": "nginx-7d4f8b-x2k",
    "namespace": "arturo-llm-test",
    "severity": "critical",
}

SAMPLE_ANNOTATIONS = {
    "description": "Container nginx was OOM killed",
    "summary": "Pod nginx OOM",
}

GOOD_DIAGNOSIS_JSON = json.dumps({
    "diagnosis": "Pod killed by OOM. Memory limit 256Mi too low.",
    "commands": [
        "kubectl describe pod nginx-7d4f8b-x2k -n arturo-llm-test",
        "kubectl top pod nginx-7d4f8b-x2k -n arturo-llm-test",
    ],
    "confidence": 0.85,
    "risk": "low",
    "explanation": "Container exceeded memory limit. Increase to 384Mi.",
})

SAMPLE_RAG_CONTEXT = {
    "query": "OOMKilled pod nginx",
    "runbooks": [
        {
            "id": "rb-oom-001",
            "document": "Fix OOM by increasing memory limits",
            "distance": 0.1,
            "metadata": {"error_class": "OOMKilled"},
        }
    ],
    "incidents": [],
}


def mock_llm_client(response_text: str):
    """Mock httpx client that returns a fake LLM response."""
    mock_response = MagicMock()
    mock_response.json.return_value = {"response": response_text}
    mock_response.raise_for_status = MagicMock()
    client = AsyncMock()
    client.post = AsyncMock(return_value=mock_response)
    return client


# ── build_alert_text tests ────────────────────────────────────────────────────

class TestBuildAlertText:
    def test_full_alert(self):
        text = build_alert_text(SAMPLE_LABELS, SAMPLE_ANNOTATIONS, "firing")
        assert "KubePodOOMKilled" in text
        assert "firing" in text
        assert "critical" in text
        assert "nginx-7d4f8b-x2k" in text
        assert "arturo-llm-test" in text
        assert "Pod nginx OOM" in text
        assert "Container nginx was OOM killed" in text

    def test_minimal_alert(self):
        text = build_alert_text({}, {}, "resolved")
        assert "UnknownAlert" in text
        assert "resolved" in text
        assert "No description" in text

    def test_no_summary(self):
        text = build_alert_text(
            {"alertname": "HighCPU"},
            {"description": "CPU above 90%"},
            "firing",
        )
        assert "Summary" not in text
        assert "CPU above 90%" in text


# ── format_context_docs tests ─────────────────────────────────────────────────

class TestFormatContextDocs:
    def test_empty_docs(self):
        assert format_context_docs([]) == "No relevant context found."

    def test_formats_docs_with_metadata(self):
        docs = [
            {
                "document": "Increase memory limits for OOM",
                "distance": 0.15,
                "metadata": {"error_class": "OOMKilled"},
            }
        ]
        result = format_context_docs(docs)
        assert "OOMKilled" in result
        assert "Increase memory limits" in result
        assert "0.85" in result  # 1 - 0.15 = 0.85

    def test_multiple_docs(self):
        docs = [
            {"document": "doc1", "distance": 0.1, "metadata": {"error_class": "OOM"}},
            {"document": "doc2", "distance": 0.2, "metadata": {"error_class": "CrashLoop"}},
        ]
        result = format_context_docs(docs)
        assert "doc1" in result
        assert "doc2" in result


# ── generate_diagnosis tests ──────────────────────────────────────────────────

class TestGenerateDiagnosis:
    @pytest.mark.asyncio
    async def test_successful_diagnosis(self):
        client = mock_llm_client(GOOD_DIAGNOSIS_JSON)
        result = await generate_diagnosis(
            SAMPLE_LABELS, SAMPLE_ANNOTATIONS, "firing",
            SAMPLE_RAG_CONTEXT, client,
        )
        assert result["diagnosis"] == "Pod killed by OOM. Memory limit 256Mi too low."
        assert len(result["commands"]) == 2
        assert result["confidence"] == 0.85
        assert result["risk"] == "low"
        assert result["duration_ms"] >= 0
        assert "rb-oom-001" in result["rag_sources"]

    @pytest.mark.asyncio
    async def test_unparseable_llm_response(self):
        client = mock_llm_client("I don't know what to do, sorry!")
        result = await generate_diagnosis(
            SAMPLE_LABELS, SAMPLE_ANNOTATIONS, "firing",
            SAMPLE_RAG_CONTEXT, client,
        )
        assert result["confidence"] == 0.0
        assert result["risk"] == "high"
        assert result["commands"] == []
        assert "could not be parsed" in result["explanation"]

    @pytest.mark.asyncio
    async def test_invalid_risk_defaults_to_high(self):
        bad_json = json.dumps({
            "diagnosis": "Something wrong",
            "commands": [],
            "confidence": 0.5,
            "risk": "YOLO",
            "explanation": "test",
        })
        client = mock_llm_client(bad_json)
        result = await generate_diagnosis(
            SAMPLE_LABELS, SAMPLE_ANNOTATIONS, "firing",
            SAMPLE_RAG_CONTEXT, client,
        )
        assert result["risk"] == "high"

    @pytest.mark.asyncio
    async def test_confidence_clamped(self):
        bad_json = json.dumps({
            "diagnosis": "test",
            "commands": [],
            "confidence": 5.0,
            "risk": "low",
            "explanation": "test",
        })
        client = mock_llm_client(bad_json)
        result = await generate_diagnosis(
            SAMPLE_LABELS, SAMPLE_ANNOTATIONS, "firing",
            SAMPLE_RAG_CONTEXT, client,
        )
        assert result["confidence"] == 1.0

    @pytest.mark.asyncio
    async def test_rag_sources_includes_all_doc_ids(self):
        context = {
            "runbooks": [{"id": "rb-1", "document": "d", "distance": 0.1, "metadata": {}}],
            "incidents": [{"id": "inc-1", "document": "d", "distance": 0.2, "metadata": {}}],
        }
        client = mock_llm_client(GOOD_DIAGNOSIS_JSON)
        result = await generate_diagnosis(
            SAMPLE_LABELS, SAMPLE_ANNOTATIONS, "firing",
            context, client,
        )
        assert "rb-1" in result["rag_sources"]
        assert "inc-1" in result["rag_sources"]


# ── _clamp tests ──────────────────────────────────────────────────────────────

class TestClamp:
    def test_within_range(self):
        assert _clamp(0.5, 0.0, 1.0) == 0.5

    def test_above_max(self):
        assert _clamp(2.0, 0.0, 1.0) == 1.0

    def test_below_min(self):
        assert _clamp(-1.0, 0.0, 1.0) == 0.0

    def test_non_numeric(self):
        assert _clamp("not a number", 0.0, 1.0) == 0.0

    def test_none(self):
        assert _clamp(None, 0.0, 1.0) == 0.0
