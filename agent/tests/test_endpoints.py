"""
Tests de los endpoints del agente AIOps.

Cubre: GET /healthz, GET /readyz, GET /health, POST /extract, GET /metrics.
Todos los tests usan mocks de Ollama (no requieren cluster ni LLM).
"""

import asyncio
import json
import re
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import httpx as _httpx
import pytest

import main
from main import (
    app, _format_diagnosis_message, _format_escalation_body, _extract_alert_meta,
    PendingEscalation, _escalation_to_dict, _dict_to_escalation,
    _process_alert_with_diagnosis,
    CHAOS_EXPERIMENT_COUNTER, CHAOS_MTTD_HISTOGRAM, CHAOS_MTTR_HISTOGRAM,
)
from prometheus_client import REGISTRY
from mattermost import make_hmac_token
from schemas import AlertItem
from tests.helpers import (
    VALID_PARAMS, VALID_JSON_STR,
    mock_http_client, mock_ollama_unreachable,
    mock_http_client_with_retries,
    mock_chroma_client, mock_rag_context, mock_diagnosis_result,
    mock_diagnosis_auto_remediate, mock_diagnosis_escalate,
    FakeRedis,
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

class TestReadyzQueueMode:
    """/readyz gated por Redis (cola = único camino de ingesta; Ollama no cuenta)."""

    def teardown_method(self):
        app.state.redis = None

    def test_readyz_200_when_redis_ok_and_ignores_ollama(self, api_client):
        """Redis up → 200 mode=queue; Ollama no se consulta aunque esté caído."""
        app.state.redis = AsyncMock()
        with patch.object(app.state, "http_client", mock_ollama_unreachable()):
            r = api_client.get("/readyz")
        assert r.status_code == 200
        data = r.json()
        assert data["status"] == "ready"
        assert data["mode"] == "queue"
        app.state.redis.ping.assert_awaited_once()

    def test_readyz_503_when_redis_none(self, api_client):
        app.state.redis = None
        r = api_client.get("/readyz")
        assert r.status_code == 503
        assert "Redis unavailable" in r.json()["detail"]

    def test_readyz_503_when_redis_ping_raises(self, api_client):
        app.state.redis = AsyncMock()
        app.state.redis.ping.side_effect = Exception("connection refused")
        r = api_client.get("/readyz")
        assert r.status_code == 503
        assert "Redis unreachable" in r.json()["detail"]


# ── GET /health ───────────────────────────────────────────────────────────────

class TestHealthEndpoint:
    """GET /health — deprecated, redirige a /readyz (307)."""

    def test_health_redirects_to_readyz(self, api_client):
        """Sin follow_redirects, devuelve 307 con Location: /readyz."""
        r = api_client.get("/health", follow_redirects=False)
        assert r.status_code == 307
        assert r.headers["location"] == "/readyz"

    def test_health_follows_redirect_when_redis_ok(self, api_client):
        """Con follow_redirects (default), acaba en /readyz y devuelve 200."""
        app.state.redis = AsyncMock()
        try:
            r = api_client.get("/health")
        finally:
            app.state.redis = None
        assert r.status_code == 200
        data = r.json()
        assert data["status"] == "ready"
        assert data["mode"] == "queue"


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


class TestAlertmanagerWebhook:
    """Tests para la ingesta del payload JSON de Alertmanager garantizando el Data Contract."""

    def setup_method(self):
        # La cola es el camino único: las alertas firing requieren Redis up para encolar.
        app.state.redis = AsyncMock()

    def teardown_method(self):
        app.state.redis = None

    def test_webhook_success_single_alert(self, api_client):
        payload = {
            "receiver": "webhook",
            "status": "firing",
            "alerts": [
                {
                    "status": "firing",
                    "labels": {"alertname": "PodOOMKilled", "pod": "engine-pod", "namespace": "prod"},
                    "annotations": {"description": "OOM Killed"},
                    "startsAt": "2026-03-19T14:00:00Z"
                }
            ],
            "groupLabels": {},
            "commonLabels": {},
            "commonAnnotations": {}
        }
        r = api_client.post("/webhook/alert", json=payload)
        assert r.status_code == 200
        assert r.json()["status"] == "success"
        assert r.json()["alerts_processed"] == 1

    def test_webhook_success_multiple_alerts(self, api_client):
        payload = {
            "receiver": "webhook",
            "status": "firing",
            "alerts": [
                {
                    "status": "firing",
                    "labels": {"alertname": "PodOOMKilled", "pod": "engine-pod"},
                    "annotations": {},
                    "startsAt": "2026-03-19T14:00:00Z"
                },
                {
                    "status": "resolved",
                    "labels": {"alertname": "CPUThrottlingHigh", "pod": "web-pod"},
                    "annotations": {},
                    "startsAt": "2026-03-19T14:00:00Z",
                    "endsAt": "2026-03-19T14:05:00Z"
                }
            ]
        }
        r = api_client.post("/webhook/alert", json=payload)
        assert r.status_code == 200
        assert r.json()["alerts_processed"] == 2

    def test_webhook_fails_validation_missing_status(self, api_client):
        payload = {
            "receiver": "webhook",
            # "status": "firing",  # Falta campo obligatorio
            "alerts": []
        }
        r = api_client.post("/webhook/alert", json=payload)
        assert r.status_code == 422
        
    def test_webhook_fails_validation_alert_missing_start_time(self, api_client):
        payload = {
            "receiver": "webhook",
            "status": "firing",
            "alerts": [
                {
                    "status": "firing",
                    "labels": {"alertname": "PodOOMKilled"}
                    # "startsAt" falta
                }
            ]
        }
        r = api_client.post("/webhook/alert", json=payload)
        assert r.status_code == 422


# ── Webhook con RAG + Diagnosis ───────────────────────────────────────────────

FIRING_PAYLOAD = {
    "receiver": "webhook",
    "status": "firing",
    "alerts": [
        {
            "status": "firing",
            "labels": {"alertname": "OOMKilled", "pod": "engine-pod", "namespace": "prod", "severity": "critical"},
            "annotations": {"description": "Container exceeded memory limit"},
            "startsAt": "2026-03-30T10:00:00Z",
        }
    ],
    "groupLabels": {},
    "commonLabels": {},
    "commonAnnotations": {},
}

RESOLVED_PAYLOAD = {
    "receiver": "webhook",
    "status": "resolved",
    "alerts": [
        {
            "status": "resolved",
            "labels": {"alertname": "OOMKilled", "pod": "engine-pod", "namespace": "prod", "severity": "critical"},
            "annotations": {"description": "Container exceeded memory limit"},
            "startsAt": "2026-03-30T10:00:00Z",
            "endsAt": "2026-03-30T10:05:00Z",
        }
    ],
    "groupLabels": {},
    "commonLabels": {},
    "commonAnnotations": {},
}


class TestWebhookWithDiagnosis:
    """Tests del webhook con pipeline RAG + diagnosis integrado."""

    def teardown_method(self):
        app.state.redis = None

    def test_webhook_firing_enqueues_alert(self, api_client):
        """Alerta firing → encola en Redis (el consumer corre el pipeline, no el webhook)."""
        app.state.redis = AsyncMock()
        with patch("main.enqueue_alert", new_callable=AsyncMock, return_value="1-0") as mock_enq, \
             patch("main._process_alert_with_diagnosis", new_callable=AsyncMock) as mock_task:
            r = api_client.post("/webhook/alert", json=FIRING_PAYLOAD)
        assert r.status_code == 200
        assert r.json()["alerts_processed"] == 1
        mock_enq.assert_awaited_once()
        mock_task.assert_not_called()

    def test_webhook_resolved_skips_diagnosis(self, api_client):
        """Alerta resolved NO encola diagnosis — va directo a Mattermost como texto simple."""
        with patch("main.enqueue_alert", new_callable=AsyncMock) as mock_enq, \
             patch("main._process_alert_with_diagnosis", new_callable=AsyncMock) as mock_diag, \
             patch("main.send_mattermost_alert", new_callable=AsyncMock):
            r = api_client.post("/webhook/alert", json=RESOLVED_PAYLOAD)
        assert r.status_code == 200
        mock_diag.assert_not_called()
        mock_enq.assert_not_called()

    def test_process_alert_full_pipeline_success(self, api_client):
        """Pipeline completo: RAG OK + Diagnosis OK → mensaje formateado a Mattermost."""
        alert_data = FIRING_PAYLOAD["alerts"][0]
        alert = AlertItem(**alert_data)

        with patch("main.retrieve_context", new_callable=AsyncMock, return_value=mock_rag_context()), \
             patch("main.generate_diagnosis", new_callable=AsyncMock, return_value=mock_diagnosis_result()), \
             patch("main.ingest_incident", new_callable=AsyncMock), \
             patch("main.send_mattermost_alert", new_callable=AsyncMock) as mock_mm:
            import asyncio
            from main import _process_alert_with_diagnosis
            asyncio.get_event_loop().run_until_complete(
                _process_alert_with_diagnosis(alert, AsyncMock(), mock_chroma_client())
            )

        mock_mm.assert_called_once()
        sent_msg = mock_mm.call_args[0][0]
        assert "OOMKilled" in sent_msg
        assert "Diagnosis" in sent_msg
        assert "85%" in sent_msg  # confidence

    def test_process_alert_rag_failure_still_diagnoses(self, api_client):
        """ChromaDB down → contexto vacío → diagnosis sigue funcionando."""
        alert_data = FIRING_PAYLOAD["alerts"][0]
        alert = AlertItem(**alert_data)

        with patch("main.retrieve_context", new_callable=AsyncMock, side_effect=Exception("ChromaDB down")), \
             patch("main.generate_diagnosis", new_callable=AsyncMock, return_value=mock_diagnosis_result()) as mock_diag, \
             patch("main.send_mattermost_alert", new_callable=AsyncMock):
            import asyncio
            from main import _process_alert_with_diagnosis
            asyncio.get_event_loop().run_until_complete(
                _process_alert_with_diagnosis(alert, AsyncMock(), None)
            )

        # generate_diagnosis fue llamado con contexto vacío
        call_kwargs = mock_diag.call_args
        rag_ctx = call_kwargs[0][3]  # 4th positional arg
        assert rag_ctx["runbooks"] == []
        assert rag_ctx["incidents"] == []

    def test_process_alert_ollama_failure_sends_fallback(self, api_client):
        """Ollama down → diagnosis=None → mensaje con 'Diagnosis unavailable'."""
        alert_data = FIRING_PAYLOAD["alerts"][0]
        alert = AlertItem(**alert_data)

        with patch("main.retrieve_context", new_callable=AsyncMock, return_value=mock_rag_context()), \
             patch("main.generate_diagnosis", new_callable=AsyncMock, side_effect=Exception("Ollama timeout")), \
             patch("main.send_mattermost_alert", new_callable=AsyncMock) as mock_mm:
            import asyncio
            from main import _process_alert_with_diagnosis
            asyncio.get_event_loop().run_until_complete(
                _process_alert_with_diagnosis(alert, AsyncMock(), mock_chroma_client())
            )

        sent_msg = mock_mm.call_args[0][0]
        assert "Diagnosis unavailable" in sent_msg
        assert "OOMKilled" in sent_msg

    def test_process_alert_calls_remediation_after_diagnosis(self, api_client):
        """Pipeline llama a process_remediation cuando diagnosis tiene éxito."""
        alert_data = FIRING_PAYLOAD["alerts"][0]
        alert = AlertItem(**alert_data)

        with patch("main.retrieve_context", new_callable=AsyncMock, return_value=mock_rag_context()), \
             patch("main.generate_diagnosis", new_callable=AsyncMock, return_value=mock_diagnosis_result()), \
             patch("main.process_remediation", new_callable=AsyncMock, return_value={
                 "action": "suggest_only", "execution_log": "", "blocked_commands": [], "safe_commands": [],
             }) as mock_rem, \
             patch("main.send_mattermost_alert", new_callable=AsyncMock):
            import asyncio
            from main import _process_alert_with_diagnosis
            asyncio.get_event_loop().run_until_complete(
                _process_alert_with_diagnosis(alert, AsyncMock(), mock_chroma_client())
            )

        mock_rem.assert_called_once()

    def test_process_alert_remediation_failure_is_fail_open(self, api_client):
        """Si process_remediation lanza excepción, el mensaje se envía igual (fail-open)."""
        alert_data = FIRING_PAYLOAD["alerts"][0]
        alert = AlertItem(**alert_data)

        with patch("main.retrieve_context", new_callable=AsyncMock, return_value=mock_rag_context()), \
             patch("main.generate_diagnosis", new_callable=AsyncMock, return_value=mock_diagnosis_result()), \
             patch("main.process_remediation", new_callable=AsyncMock, side_effect=Exception("remediation crash")), \
             patch("main.ingest_incident", new_callable=AsyncMock), \
             patch("main.send_mattermost_alert", new_callable=AsyncMock) as mock_mm:
            import asyncio
            from main import _process_alert_with_diagnosis
            asyncio.get_event_loop().run_until_complete(
                _process_alert_with_diagnosis(alert, AsyncMock(), mock_chroma_client())
            )

        # Mattermost fue llamado a pesar del crash de remediation
        mock_mm.assert_called_once()
        sent_msg = mock_mm.call_args[0][0]
        assert "OOMKilled" in sent_msg

    def test_pipeline_persists_incident_after_remediation(self, api_client):
        """Después de remediation, ingest_incident es llamado con los datos del incidente."""
        from remediation import RemediationAction
        alert_data = FIRING_PAYLOAD["alerts"][0]
        alert = AlertItem(**alert_data)
        remediation_result = {
            "action": RemediationAction.AUTO_REMEDIATE,
            "execution_log": "[DRY-RUN] kubectl patch ...",
            "blocked_commands": [],
            "safe_commands": ["kubectl patch deployment ..."],
        }

        with patch("main.retrieve_context", new_callable=AsyncMock, return_value=mock_rag_context()), \
             patch("main.generate_diagnosis", new_callable=AsyncMock, return_value=mock_diagnosis_result()), \
             patch("main.process_remediation", new_callable=AsyncMock, return_value=remediation_result), \
             patch("main.ingest_incident", new_callable=AsyncMock) as mock_ingest, \
             patch("main.send_mattermost_alert", new_callable=AsyncMock):
            import asyncio
            from main import _process_alert_with_diagnosis
            asyncio.get_event_loop().run_until_complete(
                _process_alert_with_diagnosis(alert, AsyncMock(), mock_chroma_client())
            )

        mock_ingest.assert_called_once()
        call_kwargs = mock_ingest.call_args[1]
        assert call_kwargs["doc_id"].startswith("incident-OOMKilled-")
        assert "OOMKilled" in call_kwargs["text"]
        assert call_kwargs["metadata"]["outcome"] == "auto_remediate"

    def test_pipeline_feedback_failure_is_fail_open(self, api_client):
        """Si ingest_incident falla, Mattermost recibe el mensaje igual (fail-open)."""
        alert_data = FIRING_PAYLOAD["alerts"][0]
        alert = AlertItem(**alert_data)

        with patch("main.retrieve_context", new_callable=AsyncMock, return_value=mock_rag_context()), \
             patch("main.generate_diagnosis", new_callable=AsyncMock, return_value=mock_diagnosis_result()), \
             patch("main.process_remediation", new_callable=AsyncMock, return_value={
                 "action": "suggest_only", "execution_log": "", "blocked_commands": [], "safe_commands": [],
             }), \
             patch("main.ingest_incident", new_callable=AsyncMock, side_effect=Exception("ChromaDB write failed")), \
             patch("main.send_mattermost_alert", new_callable=AsyncMock) as mock_mm:
            import asyncio
            from main import _process_alert_with_diagnosis
            asyncio.get_event_loop().run_until_complete(
                _process_alert_with_diagnosis(alert, AsyncMock(), mock_chroma_client())
            )

        mock_mm.assert_called_once()
        sent_msg = mock_mm.call_args[0][0]
        assert "OOMKilled" in sent_msg

    def test_process_alert_full_failure_sends_raw(self, api_client):
        """Fallo total del pipeline → envía mensaje raw de la alerta."""
        alert_data = FIRING_PAYLOAD["alerts"][0]
        alert = AlertItem(**alert_data)

        with patch("main.retrieve_context", new_callable=AsyncMock, side_effect=Exception("ChromaDB down")), \
             patch("main.generate_diagnosis", new_callable=AsyncMock, side_effect=Exception("Ollama down")), \
             patch("main.send_mattermost_alert", new_callable=AsyncMock) as mock_mm:
            import asyncio
            from main import _process_alert_with_diagnosis
            asyncio.get_event_loop().run_until_complete(
                _process_alert_with_diagnosis(alert, AsyncMock(), None)
            )

        # Mattermost fue llamado (fallback)
        mock_mm.assert_called_once()
        sent_msg = mock_mm.call_args[0][0]
        assert "OOMKilled" in sent_msg


class TestFormatDiagnosisMessage:
    """Tests de _format_diagnosis_message()."""

    def _make_alert(self, status="firing") -> AlertItem:
        return AlertItem(
            status=status,
            labels={"alertname": "OOMKilled", "pod": "engine-pod", "namespace": "prod", "severity": "critical"},
            annotations={"description": "Container exceeded memory limit"},
            startsAt="2026-03-30T10:00:00Z",
        )

    def test_format_with_full_diagnosis(self):
        alert = self._make_alert()
        msg = _format_diagnosis_message(alert, mock_diagnosis_result())
        assert "🔴" in msg
        assert "OOMKilled" in msg
        assert "Container exceeded memory limit" in msg
        assert "85%" in msg
        assert "HIGH" in msg
        assert "kubectl describe pod" in msg
        assert "runbook-oomkilled-001" in msg

    def test_format_without_diagnosis(self):
        alert = self._make_alert()
        msg = _format_diagnosis_message(alert, None)
        assert "Diagnosis unavailable" in msg
        assert "OOMKilled" in msg
        assert "Container exceeded memory limit" in msg

    def test_format_with_empty_commands(self):
        diag = {**mock_diagnosis_result(), "commands": []}
        alert = self._make_alert()
        msg = _format_diagnosis_message(alert, diag)
        assert "Suggested commands" not in msg
        assert "Diagnosis" in msg

    def test_format_resolved_alert(self):
        alert = self._make_alert(status="resolved")
        msg = _format_diagnosis_message(alert, None)
        assert "🟢" in msg

    def test_format_with_auto_remediation(self):
        from remediation import RemediationAction
        alert = self._make_alert()
        diag = mock_diagnosis_auto_remediate()
        remediation = {
            "action": RemediationAction.AUTO_REMEDIATE,
            "execution_log": "[DRY-RUN] kubectl set resources deployment engine --limits=memory=512Mi -n prod",
            "blocked_commands": [],
            "safe_commands": ["kubectl describe pod engine-pod -n prod"],
        }
        msg = _format_diagnosis_message(alert, diag, remediation)
        assert "Auto-remediated" in msg
        assert "DRY-RUN" in msg
        assert "Requires human approval" not in msg

    def test_format_with_escalation(self):
        from remediation import RemediationAction
        alert = self._make_alert()
        diag = mock_diagnosis_escalate()
        remediation = {
            "action": RemediationAction.ESCALATE,
            "execution_log": "",
            "blocked_commands": ["kubectl drain node-1 --ignore-daemonsets"],
            "safe_commands": [],
        }
        msg = _format_diagnosis_message(alert, diag, remediation)
        assert "Requires human approval" in msg
        assert "kubectl drain" in msg
        assert "⛔" in msg
        assert "Auto-remediated" not in msg

    def test_format_suggest_only_no_remediation_block(self):
        from remediation import RemediationAction
        alert = self._make_alert()
        diag = mock_diagnosis_result()
        remediation = {
            "action": RemediationAction.SUGGEST_ONLY,
            "execution_log": "",
            "blocked_commands": [],
            "safe_commands": [],
        }
        msg = _format_diagnosis_message(alert, diag, remediation)
        assert "Auto-remediated" not in msg
        assert "Requires human approval" not in msg
        assert "Diagnosis" in msg


# ── GET /metrics ─────────────────────────────────────────────────────────────

class TestMetricsEndpoint:
    """Prometheus metrics endpoint auto-instrumentado."""

    def test_metrics_returns_200(self, api_client):
        r = api_client.get("/metrics")
        assert r.status_code == 200
        assert "http_request" in r.text


# ── POST /webhook/action ──────────────────────────────────────────────────────

def _make_pending_escalation(incident_id: str, ttl_minutes: int = 60) -> PendingEscalation:
    alert = AlertItem(
        status="firing",
        labels={"alertname": "KubePodOOMKilled", "pod": "engine-0", "namespace": "prod", "severity": "critical"},
        annotations={"description": "Pod OOMKilled"},
        startsAt="2026-05-06T10:00:00Z",
    )
    return PendingEscalation(
        incident_id=incident_id,
        alert_item=alert,
        diagnosis=mock_diagnosis_result(),
        safe_commands=["kubectl describe pod engine-0 -n prod"],
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=ttl_minutes),
    )


def _seed_redis(fake_redis: FakeRedis, incident_id: str, ttl_minutes: int = 60) -> None:
    """Seed a PendingEscalation into FakeRedis as JSON (mirrors the store path in main.py)."""
    esc = _make_pending_escalation(incident_id, ttl_minutes=ttl_minutes)
    fake_redis.set_raw(f"escalation:{incident_id}", json.dumps(_escalation_to_dict(esc)))


class TestActionCallbackEndpoint:
    """POST /webhook/action — callbacks de botones interactivos de Mattermost."""

    def setup_method(self):
        self.fake_redis = FakeRedis()
        app.state.redis = self.fake_redis

    def teardown_method(self):
        app.state.redis = None

    def test_approve_executes_commands_and_returns_update(self, api_client):
        """Approve → execute_commands llamado, respuesta con 'update' que limpia botones."""
        _seed_redis(self.fake_redis, "abc-123")

        with patch("main.execute_commands", new_callable=AsyncMock, return_value="[DRY-RUN] kubectl describe") as mock_exec, \
             patch("main.ingest_incident", new_callable=AsyncMock):
            r = api_client.post("/webhook/action", json={
                "user_name": "arturo",
                "context": {"action": "approve", "incident_id": "abc-123"},
            })

        assert r.status_code == 200
        body = r.json()
        assert "update" in body
        assert body["update"]["props"]["attachments"] == []
        assert "arturo" in body["update"]["message"]
        mock_exec.assert_called_once_with(["kubectl describe pod engine-0 -n prod"])
        assert self.fake_redis._store.get("escalation:abc-123") is None

    def test_reject_does_not_execute_commands(self, api_client):
        """Reject → execute_commands NO llamado, mensaje de rechazo en update."""
        _seed_redis(self.fake_redis, "abc-456")

        with patch("main.execute_commands", new_callable=AsyncMock) as mock_exec, \
             patch("main.ingest_incident", new_callable=AsyncMock):
            r = api_client.post("/webhook/action", json={
                "user_name": "arturo",
                "context": {"action": "reject", "incident_id": "abc-456"},
            })

        assert r.status_code == 200
        body = r.json()
        assert "update" in body
        assert "RECHAZADA" in body["update"]["message"]
        assert "arturo" in body["update"]["message"]
        mock_exec.assert_not_called()
        assert self.fake_redis._store.get("escalation:abc-456") is None

    def test_unknown_incident_id_returns_ephemeral_text(self, api_client):
        """incident_id no encontrado → ephemeral_text + update que limpia los botones."""
        r = api_client.post("/webhook/action", json={
            "context": {"action": "approve", "incident_id": "no-existe"},
        })
        assert r.status_code == 200
        body = r.json()
        assert "ephemeral_text" in body
        assert "update" in body
        assert body["update"]["props"]["attachments"] == []

    def test_expired_escalation_returns_ephemeral_text(self, api_client):
        """Escalación con TTL expirado (expires_at en el pasado) → ephemeral_text."""
        _seed_redis(self.fake_redis, "old-xyz", ttl_minutes=-1)

        with patch("main.execute_commands", new_callable=AsyncMock) as mock_exec:
            r = api_client.post("/webhook/action", json={
                "context": {"action": "approve", "incident_id": "old-xyz"},
            })

        assert r.status_code == 200
        body = r.json()
        assert "ephemeral_text" in body
        mock_exec.assert_not_called()

    def test_approve_persists_outcome_to_chromadb(self, api_client):
        """Approve → ingest_incident llamado con outcome correspondiente."""
        _seed_redis(self.fake_redis, "cb-789")

        with patch("main.execute_commands", new_callable=AsyncMock, return_value="[DRY-RUN] done"), \
             patch("main.ingest_incident", new_callable=AsyncMock) as mock_ingest:
            api_client.post("/webhook/action", json={
                "user_name": "arturo",
                "context": {"action": "approve", "incident_id": "cb-789"},
            })

        mock_ingest.assert_called_once()
        _, kwargs = mock_ingest.call_args
        assert kwargs["metadata"]["outcome"] == "auto_remediate"

    def test_expired_escalation_is_removed_from_redis(self, api_client):
        """Expired TTL callback deletes entry from Redis (no zombie entries)."""
        _seed_redis(self.fake_redis, "expired-abc", ttl_minutes=-1)

        with patch("main.execute_commands", new_callable=AsyncMock):
            api_client.post("/webhook/action", json={
                "context": {"action": "approve", "incident_id": "expired-abc"},
            })

        assert self.fake_redis._store.get("escalation:expired-abc") is None

    def test_duplicate_callback_same_incident_is_idempotent(self, api_client):
        """Second callback for same incident_id → not-found response (entry already deleted)."""
        _seed_redis(self.fake_redis, "dup-123")

        with patch("main.execute_commands", new_callable=AsyncMock, return_value="[DRY-RUN] done"), \
             patch("main.ingest_incident", new_callable=AsyncMock):
            r1 = api_client.post("/webhook/action", json={
                "user_name": "arturo",
                "context": {"action": "approve", "incident_id": "dup-123"},
            })
            r2 = api_client.post("/webhook/action", json={
                "user_name": "arturo",
                "context": {"action": "approve", "incident_id": "dup-123"},
            })

        assert r1.status_code == 200
        assert "update" in r1.json()
        assert r2.status_code == 200
        assert "ephemeral_text" in r2.json()

    def test_missing_hmac_returns_401_when_secret_set(self, api_client):
        """When webhook_secret is set, missing hmac_token → 401 (before Redis lookup)."""
        with patch.object(main.settings, "webhook_secret", "test-secret"):
            r = api_client.post("/webhook/action", json={
                "context": {"action": "approve", "incident_id": "hmac-001"},
            })
        assert r.status_code == 401

    def test_invalid_hmac_returns_401_when_secret_set(self, api_client):
        """When webhook_secret is set, wrong hmac_token → 401."""
        with patch.object(main.settings, "webhook_secret", "test-secret"):
            r = api_client.post("/webhook/action", json={
                "context": {"action": "approve", "incident_id": "hmac-002", "hmac_token": "bad-token"},
            })
        assert r.status_code == 401

    def test_valid_hmac_passes_when_secret_set(self, api_client):
        """When webhook_secret is set, correct hmac_token → request processed normally."""
        _seed_redis(self.fake_redis, "hmac-003")
        token = make_hmac_token("hmac-003", "reject", "test-secret")
        with patch.object(main.settings, "webhook_secret", "test-secret"), \
             patch("main.ingest_incident", new_callable=AsyncMock):
            r = api_client.post("/webhook/action", json={
                "context": {"action": "reject", "incident_id": "hmac-003", "hmac_token": token},
            })
        assert r.status_code == 200


class TestCleanupExpiredEscalations:
    """Escalation cleanup is now delegated to Redis TTL.

    These tests verify the serialization round-trip (_escalation_to_dict / _dict_to_escalation)
    that replaced the old in-memory _cleanup_expired_escalations() function.
    """

    def test_escalation_roundtrip_serialization(self):
        """_escalation_to_dict → _dict_to_escalation produces identical object."""
        original = _make_pending_escalation("roundtrip-001")
        d = _escalation_to_dict(original)
        restored = _dict_to_escalation(d)

        assert restored.incident_id == original.incident_id
        assert restored.safe_commands == original.safe_commands
        assert restored.expires_at == original.expires_at
        assert restored.alert_item.labels == original.alert_item.labels
        assert restored.diagnosis["confidence"] == original.diagnosis["confidence"]

    def test_escalation_to_dict_is_json_serializable(self):
        """_escalation_to_dict output must be JSON-serializable for Redis storage."""
        esc = _make_pending_escalation("json-check")
        d = _escalation_to_dict(esc)
        raw = json.dumps(d)
        assert isinstance(raw, str)
        assert "json-check" in raw

    def test_dict_to_escalation_handles_expired_expires_at(self):
        """Expired expires_at deserializes correctly (endpoint checks it explicitly)."""
        esc = _make_pending_escalation("exp-check", ttl_minutes=-5)
        d = _escalation_to_dict(esc)
        restored = _dict_to_escalation(d)
        from datetime import datetime, timezone
        assert restored.expires_at < datetime.now(timezone.utc)


# ── _extract_alert_meta ───────────────────────────────────────────────────────

class TestExtractAlertMeta:
    """Unit tests for _extract_alert_meta helper (M5)."""

    def _make_alert(self, labels: dict | None = None) -> AlertItem:
        return AlertItem(
            status="firing",
            labels=labels or {},
            annotations={},
            startsAt="2026-05-11T10:00:00Z",
        )

    def test_defaults_when_labels_missing(self):
        alert = self._make_alert()
        severity, alert_name, pod, namespace = _extract_alert_meta(alert)
        assert severity == "CRITICAL"
        assert alert_name == "UnknownAlert"
        assert pod == "unknown-pod"
        assert namespace == "unknown-ns"

    def test_returns_uppercased_severity(self):
        alert = self._make_alert({"severity": "warning", "alertname": "HighCPU", "pod": "my-pod", "namespace": "prod"})
        severity, alert_name, pod, namespace = _extract_alert_meta(alert)
        assert severity == "WARNING"
        assert alert_name == "HighCPU"
        assert pod == "my-pod"
        assert namespace == "prod"

    def test_severity_already_upper_stays_upper(self):
        alert = self._make_alert({"severity": "CRITICAL"})
        severity, *_ = _extract_alert_meta(alert)
        assert severity == "CRITICAL"


# ── Defensive .get() in formatters ───────────────────────────────────────────

class TestFormatDiagnosisMessageDefensiveGet:
    """_format_diagnosis_message does not KeyError on incomplete diagnosis (M3)."""

    def _make_alert(self) -> AlertItem:
        return AlertItem(
            status="firing",
            labels={"alertname": "OOMKilled", "pod": "p", "namespace": "ns", "severity": "critical"},
            annotations={},
            startsAt="2026-05-11T10:00:00Z",
        )

    def test_missing_confidence_uses_default(self):
        diag = {"risk": "high", "diagnosis": "Memory pressure detected"}
        msg = _format_diagnosis_message(self._make_alert(), diag)
        assert "0%" in msg
        assert "HIGH" in msg

    def test_missing_risk_uses_default(self):
        diag = {"confidence": 0.85, "diagnosis": "Memory pressure detected"}
        msg = _format_diagnosis_message(self._make_alert(), diag)
        assert "85%" in msg
        assert "HIGH" in msg

    def test_missing_diagnosis_key_uses_default(self):
        diag = {"confidence": 0.7, "risk": "medium"}
        msg = _format_diagnosis_message(self._make_alert(), diag)
        assert "Sin diagnóstico" in msg


class TestFormatEscalationBodyDefensiveGet:
    """_format_escalation_body does not KeyError on incomplete diagnosis (M4)."""

    def _make_remediation(self) -> dict:
        return {"safe_commands": ["kubectl get pods"]}

    def test_missing_confidence_uses_default(self):
        diag = {"risk": "high", "diagnosis": "OOM"}
        body = _format_escalation_body(diag, self._make_remediation())
        assert "0%" in body

    def test_missing_risk_uses_default(self):
        diag = {"confidence": 0.9, "diagnosis": "OOM"}
        body = _format_escalation_body(diag, self._make_remediation())
        assert "HIGH" in body

    def test_missing_diagnosis_key_uses_default(self):
        diag = {"confidence": 0.5, "risk": "low"}
        body = _format_escalation_body(diag, self._make_remediation())
        assert "Sin diagnóstico" in body


# ── M6: unknown action → 400 ──────────────────────────────────────────────────

class TestActionCallbackUnknownAction:
    """Unknown action in callback returns 400 (M6)."""

    def setup_method(self):
        self.fake_redis = FakeRedis()
        app.state.redis = self.fake_redis

    def teardown_method(self):
        app.state.redis = None

    def test_unknown_action_returns_400(self, api_client):
        _seed_redis(self.fake_redis, "m6-test")
        r = api_client.post("/webhook/action", json={
            "user_name": "arturo",
            "context": {"action": "unknown_typo", "incident_id": "m6-test"},
        })
        assert r.status_code == 400
        assert "unknown_typo" in r.json()["detail"]

    def test_empty_action_returns_400(self, api_client):
        _seed_redis(self.fake_redis, "m6-empty")
        r = api_client.post("/webhook/action", json={
            "context": {"action": "", "incident_id": "m6-empty"},
        })
        assert r.status_code == 400


# ── M7: execute_commands failure counter ─────────────────────────────────────

class TestActionCallbackApproveFailure:
    """execute_commands failure: endpoint returns 200 and logs ERROR in message (M7)."""

    def setup_method(self):
        self.fake_redis = FakeRedis()
        app.state.redis = self.fake_redis

    def teardown_method(self):
        app.state.redis = None

    def test_execute_failure_returns_200_with_error_in_log(self, api_client):
        _seed_redis(self.fake_redis, "m7-test")

        with patch("main.execute_commands", new_callable=AsyncMock, side_effect=RuntimeError("cmd failed")), \
             patch("main.ingest_incident", new_callable=AsyncMock):
            r = api_client.post("/webhook/action", json={
                "user_name": "arturo",
                "context": {"action": "approve", "incident_id": "m7-test"},
            })

        assert r.status_code == 200
        body = r.json()
        assert "update" in body
        assert "ERROR" in body["update"]["message"]
        assert "cmd failed" in body["update"]["message"]


# ── M10: fallback send_mattermost_alert exception does not propagate ──────────

class TestProcessAlertFallbackException:
    """M10: if the fallback Mattermost send in the except block also fails,
    the exception is logged, not propagated — background task completes cleanly."""

    @pytest.mark.asyncio
    async def test_fallback_failure_does_not_raise(self):
        alert = AlertItem(
            status="firing",
            labels={"alertname": "TestAlert", "severity": "critical",
                    "pod": "test-pod", "namespace": "test-ns"},
            annotations={"description": "test"},
            startsAt="2026-01-01T00:00:00Z",
        )
        http_client = AsyncMock()
        chroma_client = None

        with patch("main.build_rag_query", side_effect=RuntimeError("pipeline boom")), \
             patch("main.send_mattermost_alert", new_callable=AsyncMock,
                   side_effect=RuntimeError("mattermost also down")):
            # Should complete without raising — M10 guard logs the fallback failure
            await _process_alert_with_diagnosis(alert, http_client, chroma_client, redis_client=None)

    @pytest.mark.asyncio
    async def test_fallback_called_when_pipeline_fails(self):
        alert = AlertItem(
            status="firing",
            labels={"alertname": "TestAlert", "severity": "warning",
                    "pod": "test-pod", "namespace": "test-ns"},
            annotations={"description": "test"},
            startsAt="2026-01-01T00:00:00Z",
        )
        http_client = AsyncMock()

        with patch("main.build_rag_query", side_effect=RuntimeError("pipeline boom")), \
             patch("main.send_mattermost_alert", new_callable=AsyncMock, return_value=True) as mock_send:
            await _process_alert_with_diagnosis(alert, http_client, None, redis_client=None)

        mock_send.assert_called_once()
        fallback_msg = mock_send.call_args[0][0]
        assert "TestAlert" in fallback_msg


# ── Redis Streams queue path (F2 — camino único de ingesta) ───────────────────

class TestWebhookQueuePath:
    """El webhook encola en Redis (fail-closed); si Redis cae → 503."""

    def teardown_method(self):
        app.state.redis = None

    def _payload(self, alertname: str = "QueueAlert") -> dict:
        return {
            "status": "firing",
            "receiver": "agent",
            "alerts": [{
                "status": "firing",
                "labels": {"alertname": alertname, "pod": "p", "namespace": "ns", "severity": "critical"},
                "annotations": {"description": "test"},
                "startsAt": "2026-01-01T00:00:00Z",
            }],
        }

    def test_firing_enqueued(self, api_client):
        app.state.redis = AsyncMock()
        with patch("main.enqueue_alert", new_callable=AsyncMock, return_value="1-0") as mock_enq, \
             patch("main._process_alert_with_diagnosis", new_callable=AsyncMock) as mock_pipe:
            r = api_client.post("/webhook/alert", json=self._payload())
        assert r.status_code == 200
        assert r.json()["alerts_processed"] == 1
        mock_enq.assert_awaited_once()
        mock_pipe.assert_not_called()  # camino de cola, no BackgroundTask

    def test_dedup_returns_none_increments_counter(self, api_client):
        app.state.redis = AsyncMock()
        before = REGISTRY.get_sample_value("aiops_dedup_skipped_total", {"alertname": "QueueAlert"}) or 0.0
        with patch("main.enqueue_alert", new_callable=AsyncMock, return_value=None):
            r = api_client.post("/webhook/alert", json=self._payload())
        assert r.status_code == 200
        after = REGISTRY.get_sample_value("aiops_dedup_skipped_total", {"alertname": "QueueAlert"}) or 0.0
        assert after == before + 1

    def test_enqueue_failure_returns_503_fail_closed(self, api_client):
        app.state.redis = AsyncMock()
        with patch("main.enqueue_alert", new_callable=AsyncMock, side_effect=Exception("Redis down")):
            r = api_client.post("/webhook/alert", json=self._payload())
        assert r.status_code == 503

    def test_redis_none_returns_503_fail_closed(self, api_client):
        """Sin Redis no hay fallback legacy: el webhook firing devuelve 503."""
        app.state.redis = None
        with patch("main.enqueue_alert", new_callable=AsyncMock) as mock_enq:
            r = api_client.post("/webhook/alert", json=self._payload())
        assert r.status_code == 503
        mock_enq.assert_not_called()


class TestHandleStreamEntry:
    """El handler decodifica la entrada y delega en el pipeline existente."""

    def _fields(self, alertname: str = "StreamAlert") -> dict:
        alert = AlertItem(
            status="firing",
            labels={"alertname": alertname, "pod": "p", "namespace": "ns", "severity": "critical"},
            annotations={"description": "test"},
            startsAt="2026-01-01T00:00:00Z",
        )
        return {"payload": alert.model_dump_json()}

    @pytest.mark.asyncio
    async def test_delegates_to_pipeline_and_counts_success(self):
        before = REGISTRY.get_sample_value("aiops_queue_processed_total", {"outcome": "success"}) or 0.0
        with patch("main._process_alert_with_diagnosis", new_callable=AsyncMock) as mock_pipe:
            await main._handle_stream_entry("1-0", self._fields())
        mock_pipe.assert_awaited_once()
        assert mock_pipe.call_args.args[0].labels["alertname"] == "StreamAlert"
        after = REGISTRY.get_sample_value("aiops_queue_processed_total", {"outcome": "success"}) or 0.0
        assert after == before + 1

    @pytest.mark.asyncio
    async def test_pipeline_error_propagates_and_counts_error(self):
        before = REGISTRY.get_sample_value("aiops_queue_processed_total", {"outcome": "error"}) or 0.0
        with patch("main._process_alert_with_diagnosis", new_callable=AsyncMock, side_effect=Exception("boom")):
            with pytest.raises(Exception, match="boom"):
                await main._handle_stream_entry("1-0", self._fields())
        after = REGISTRY.get_sample_value("aiops_queue_processed_total", {"outcome": "error"}) or 0.0
        assert after == before + 1


class TestPeriodicReclaim:
    """La tarea periódica delega en reclaim_pending con el handler del stream."""

    @pytest.mark.asyncio
    async def test_delegates_to_reclaim_pending(self):
        main.app.state.redis = AsyncMock()
        # reclaim_pending real haría una iteración y dormiría; cortamos en el sleep.
        with patch("main.reclaim_pending", new_callable=AsyncMock) as mock_reclaim, \
             patch("main.asyncio.sleep", new_callable=AsyncMock, side_effect=asyncio.CancelledError()):
            with pytest.raises(asyncio.CancelledError):
                await main._periodic_reclaim()
        mock_reclaim.assert_awaited_once_with(main.app.state.redis, main._handle_stream_entry)

    @pytest.mark.asyncio
    async def test_reclaim_error_does_not_kill_task(self):
        main.app.state.redis = AsyncMock()
        with patch("main.reclaim_pending", new_callable=AsyncMock, side_effect=Exception("redis down")), \
             patch("main.asyncio.sleep", new_callable=AsyncMock, side_effect=asyncio.CancelledError()):
            # El error de reclaim se traga; la tarea llega al sleep (que aquí corta).
            with pytest.raises(asyncio.CancelledError):
                await main._periodic_reclaim()


# ── LLM timeout messaging ─────────────────────────────────────────────────────

class TestDiagnosisTimeout:
    """When LLM times out, Mattermost receives a specific timeout message (not generic)."""

    def _make_alert(self, alertname: str = "HighCPU") -> AlertItem:
        return AlertItem(
            status="firing",
            labels={"alertname": alertname, "pod": "cpu-pod", "namespace": "prod", "severity": "warning"},
            annotations={"description": "CPU usage is high"},
            startsAt="2026-01-01T00:00:00Z",
        )

    def test_format_diagnosis_message_timeout_flag(self):
        """llm_timeout=True produces message with 'LLM timeout' and HTTP_TIMEOUT value."""
        alert = self._make_alert()
        msg = _format_diagnosis_message(alert, diagnosis=None, llm_timeout=True)
        assert "LLM timeout" in msg or "timeout" in msg.lower()
        assert "CPU usage is high" in msg

    def test_format_diagnosis_message_no_flag_uses_generic(self):
        """llm_timeout=False (default) uses the generic 'Diagnosis unavailable' fallback."""
        alert = self._make_alert()
        msg = _format_diagnosis_message(alert, diagnosis=None, llm_timeout=False)
        assert "unavailable" in msg or "sin descripcion" in msg.lower() or "CPU usage is high" in msg
        assert "LLM timeout" not in msg

    @pytest.mark.asyncio
    async def test_timeout_exception_sets_llm_timeout_flag(self):
        """httpx.TimeoutException during diagnosis → Mattermost receives timeout-specific message."""
        import httpx
        alert = self._make_alert()
        http_client = AsyncMock()
        before = _get_counter("aiops_diagnosis", {"outcome": "llm_timeout"})

        with patch("main.build_rag_query", return_value="query"), \
             patch("main.retrieve_context", new_callable=AsyncMock, return_value={"runbooks": [], "incidents": [], "query": "q"}), \
             patch("main.generate_diagnosis", new_callable=AsyncMock, side_effect=httpx.TimeoutException("timed out")), \
             patch("main.send_mattermost_alert", new_callable=AsyncMock) as mock_send:
            await _process_alert_with_diagnosis(alert, http_client, None, redis_client=None)

        mock_send.assert_called_once()
        sent_msg = mock_send.call_args[0][0]
        assert "timeout" in sent_msg.lower() or "LLM" in sent_msg
        assert _get_counter("aiops_diagnosis", {"outcome": "llm_timeout"}) == before + 1

    @pytest.mark.asyncio
    async def test_non_timeout_exception_uses_generic_fallback(self):
        """Non-timeout exception during diagnosis → generic unavailable message (no 'LLM timeout')."""
        alert = self._make_alert()
        http_client = AsyncMock()
        before = _get_counter("aiops_diagnosis", {"outcome": "llm_error"})

        with patch("main.build_rag_query", return_value="query"), \
             patch("main.retrieve_context", new_callable=AsyncMock, return_value={"runbooks": [], "incidents": [], "query": "q"}), \
             patch("main.generate_diagnosis", new_callable=AsyncMock, side_effect=ValueError("bad response")), \
             patch("main.send_mattermost_alert", new_callable=AsyncMock) as mock_send:
            await _process_alert_with_diagnosis(alert, http_client, None, redis_client=None)

        mock_send.assert_called_once()
        sent_msg = mock_send.call_args[0][0]
        assert "LLM timeout" not in sent_msg
        assert _get_counter("aiops_diagnosis", {"outcome": "llm_error"}) == before + 1


# ── Escalation store observability ────────────────────────────────────────────

class TestEscalationStoreMetric:
    """aiops_escalation_store_total distingue escalación persistida vs Redis caído (PR-06)."""

    def _make_alert(self) -> AlertItem:
        return AlertItem(
            status="firing",
            labels={"alertname": "HighMem", "pod": "mem-pod", "namespace": "prod", "severity": "warning"},
            annotations={"description": "Memory usage is high"},
            startsAt="2026-01-01T00:00:00Z",
        )

    def _escalate_result(self) -> dict:
        from remediation import RemediationAction
        return {
            "action": RemediationAction.ESCALATE,
            "execution_log": "",
            "blocked_commands": [],
            "safe_commands": ["kubectl patch deployment mem-app -n prod ..."],
        }

    @pytest.mark.asyncio
    async def test_escalation_stored_increments_stored(self):
        """Redis disponible y store OK → outcome='stored' + envío con botones."""
        alert = self._make_alert()
        before = _get_counter("aiops_escalation_store", {"outcome": "stored"})

        with patch("main.retrieve_context", new_callable=AsyncMock, return_value=mock_rag_context()), \
             patch("main.generate_diagnosis", new_callable=AsyncMock, return_value=mock_diagnosis_result()), \
             patch("main.process_remediation", new_callable=AsyncMock, return_value=self._escalate_result()), \
             patch("main.store_escalation", new_callable=AsyncMock, return_value=True), \
             patch("main.ingest_incident", new_callable=AsyncMock), \
             patch("main.send_escalation_with_buttons", new_callable=AsyncMock) as mock_buttons, \
             patch("main.send_mattermost_alert", new_callable=AsyncMock):
            await _process_alert_with_diagnosis(alert, AsyncMock(), mock_chroma_client(), redis_client=AsyncMock())

        mock_buttons.assert_called_once()
        assert _get_counter("aiops_escalation_store", {"outcome": "stored"}) == before + 1

    @pytest.mark.asyncio
    async def test_escalation_redis_down_increments_redis_down(self):
        """Redis caído (redis_client=None) → outcome='redis_down' + mensaje sin botones."""
        alert = self._make_alert()
        before = _get_counter("aiops_escalation_store", {"outcome": "redis_down"})

        with patch("main.retrieve_context", new_callable=AsyncMock, return_value=mock_rag_context()), \
             patch("main.generate_diagnosis", new_callable=AsyncMock, return_value=mock_diagnosis_result()), \
             patch("main.process_remediation", new_callable=AsyncMock, return_value=self._escalate_result()), \
             patch("main.ingest_incident", new_callable=AsyncMock), \
             patch("main.send_escalation_with_buttons", new_callable=AsyncMock) as mock_buttons, \
             patch("main.send_mattermost_alert", new_callable=AsyncMock) as mock_send:
            await _process_alert_with_diagnosis(alert, AsyncMock(), mock_chroma_client(), redis_client=None)

        mock_buttons.assert_not_called()
        mock_send.assert_called_once()
        assert "Redis caído" in mock_send.call_args[0][0]
        assert _get_counter("aiops_escalation_store", {"outcome": "redis_down"}) == before + 1


# ── ChromaDB lazy reconnect (PR-05) ───────────────────────────────────────────

class TestRagReconnect:
    """Un cliente ChromaDB stale se reconecta en caliente antes de degradar (PR-05)."""

    def _make_alert(self) -> AlertItem:
        return AlertItem(
            status="firing",
            labels={"alertname": "HighMem", "pod": "mem-pod", "namespace": "prod", "severity": "warning"},
            annotations={"description": "Memory usage is high"},
            startsAt="2026-01-01T00:00:00Z",
        )

    @pytest.mark.asyncio
    async def test_stale_client_reconnects_and_persists(self):
        """retrieve_context falla con el cliente cacheado → un get_chroma_client() nuevo lo cura;
        el cliente sano se persiste en app.state y el diagnóstico procede con contexto (no degradado)."""
        import main
        alert = self._make_alert()
        before = _get_counter("aiops_diagnosis", {"outcome": "rag_reconnect"})
        saved = getattr(main.app.state, "chroma_client", None)
        sentinel = object()
        try:
            with patch("main.retrieve_context", new_callable=AsyncMock,
                       side_effect=[Exception("stale connection"), mock_rag_context()]), \
                 patch("main.get_chroma_client", return_value=sentinel) as mock_new, \
                 patch("main.generate_diagnosis", new_callable=AsyncMock, return_value=mock_diagnosis_result()), \
                 patch("main.process_remediation", new_callable=AsyncMock, return_value={
                     "action": "suggest_only", "execution_log": "", "blocked_commands": [], "safe_commands": [],
                 }) as mock_rem, \
                 patch("main.ingest_incident", new_callable=AsyncMock), \
                 patch("main.send_mattermost_alert", new_callable=AsyncMock):
                await _process_alert_with_diagnosis(alert, AsyncMock(), mock_chroma_client())

            mock_new.assert_called_once()
            assert main.app.state.chroma_client is sentinel
            # rag_degraded=False propagado a remediación (retrieval se recuperó)
            assert mock_rem.call_args.kwargs["rag_degraded"] is False
            assert _get_counter("aiops_diagnosis", {"outcome": "rag_reconnect"}) == before + 1
        finally:
            main.app.state.chroma_client = saved

    @pytest.mark.asyncio
    async def test_persistent_failure_degrades(self):
        """Si la reconexión también falla, se cae al comportamiento actual: rag_degraded=True
        propagado a remediación + counter rag_failed (PR-04 mantiene la red de seguridad)."""
        import main
        alert = self._make_alert()
        before = _get_counter("aiops_diagnosis", {"outcome": "rag_failed"})
        saved = getattr(main.app.state, "chroma_client", None)
        try:
            with patch("main.retrieve_context", new_callable=AsyncMock,
                       side_effect=Exception("chromadb down")), \
                 patch("main.get_chroma_client", return_value=object()), \
                 patch("main.generate_diagnosis", new_callable=AsyncMock, return_value=mock_diagnosis_result()), \
                 patch("main.process_remediation", new_callable=AsyncMock, return_value={
                     "action": "suggest_only", "execution_log": "", "blocked_commands": [], "safe_commands": [],
                 }) as mock_rem, \
                 patch("main.ingest_incident", new_callable=AsyncMock), \
                 patch("main.send_mattermost_alert", new_callable=AsyncMock):
                await _process_alert_with_diagnosis(alert, AsyncMock(), mock_chroma_client())

            assert mock_rem.call_args.kwargs["rag_degraded"] is True
            assert _get_counter("aiops_diagnosis", {"outcome": "rag_failed"}) == before + 1
        finally:
            main.app.state.chroma_client = saved


# ── Chaos Engineering Metrics ─────────────────────────────────────────────────

def _get_counter(name: str, labels: dict) -> float:
    return REGISTRY.get_sample_value(name + "_total", labels) or 0.0


def _get_histogram_count(name: str, labels: dict) -> float:
    return REGISTRY.get_sample_value(name + "_count", labels) or 0.0


class TestChaosMetrics:
    """Métricas chaos se registran cuando el namespace es arturo-chaos."""

    def _chaos_alert(self, alertname: str = "KubePodOOMKilled") -> AlertItem:
        return AlertItem(
            status="firing",
            labels={"alertname": alertname, "severity": "critical", "pod": "chaos-pod", "namespace": "arturo-chaos"},
            annotations={"description": "Chaos experiment"},
            startsAt="2026-05-18T10:00:00Z",
        )

    @pytest.mark.asyncio
    async def test_chaos_counter_incremented_on_chaos_namespace(self):
        alert = self._chaos_alert()
        before = _get_counter("aiops_chaos_experiment", {"experiment": "KubePodOOMKilled", "outcome": "no_diagnosis"})

        with patch("main.build_rag_query", return_value="query"), \
             patch("main.retrieve_context", new_callable=AsyncMock, return_value=mock_rag_context()), \
             patch("main.generate_diagnosis", new_callable=AsyncMock, side_effect=Exception("llm down")), \
             patch("main.send_mattermost_alert", new_callable=AsyncMock):
            await _process_alert_with_diagnosis(alert, AsyncMock(), None)

        after = _get_counter("aiops_chaos_experiment", {"experiment": "KubePodOOMKilled", "outcome": "no_diagnosis"})
        assert after == before + 1

    @pytest.mark.asyncio
    async def test_chaos_mttd_histogram_observed(self):
        alert = self._chaos_alert()
        before = _get_histogram_count("aiops_chaos_mttd_seconds", {"experiment": "KubePodOOMKilled"})

        with patch("main.build_rag_query", return_value="query"), \
             patch("main.retrieve_context", new_callable=AsyncMock, return_value=mock_rag_context()), \
             patch("main.generate_diagnosis", new_callable=AsyncMock, side_effect=Exception("llm down")), \
             patch("main.send_mattermost_alert", new_callable=AsyncMock):
            await _process_alert_with_diagnosis(alert, AsyncMock(), None)

        after = _get_histogram_count("aiops_chaos_mttd_seconds", {"experiment": "KubePodOOMKilled"})
        assert after == before + 1

    @pytest.mark.asyncio
    async def test_chaos_mttr_histogram_observed(self):
        alert = self._chaos_alert()
        before = _get_histogram_count("aiops_chaos_mttr_seconds", {"experiment": "KubePodOOMKilled"})

        with patch("main.build_rag_query", return_value="query"), \
             patch("main.retrieve_context", new_callable=AsyncMock, return_value=mock_rag_context()), \
             patch("main.generate_diagnosis", new_callable=AsyncMock, side_effect=Exception("llm down")), \
             patch("main.send_mattermost_alert", new_callable=AsyncMock):
            await _process_alert_with_diagnosis(alert, AsyncMock(), None)

        after = _get_histogram_count("aiops_chaos_mttr_seconds", {"experiment": "KubePodOOMKilled"})
        assert after == before + 1

    @pytest.mark.asyncio
    async def test_non_chaos_namespace_does_not_increment_chaos_counter(self):
        alert = AlertItem(
            status="firing",
            labels={"alertname": "KubePodOOMKilled", "severity": "critical", "pod": "prod-pod", "namespace": "arturo-llm-test"},
            annotations={"description": "Prod alert"},
            startsAt="2026-05-18T10:00:00Z",
        )
        before = _get_counter("aiops_chaos_experiment", {"experiment": "KubePodOOMKilled", "outcome": "no_diagnosis"})

        with patch("main.build_rag_query", return_value="query"), \
             patch("main.retrieve_context", new_callable=AsyncMock, return_value=mock_rag_context()), \
             patch("main.generate_diagnosis", new_callable=AsyncMock, side_effect=Exception("llm down")), \
             patch("main.send_mattermost_alert", new_callable=AsyncMock):
            await _process_alert_with_diagnosis(alert, AsyncMock(), None)

        after = _get_counter("aiops_chaos_experiment", {"experiment": "KubePodOOMKilled", "outcome": "no_diagnosis"})
        assert after == before  # no increment para namespace no-chaos

    @pytest.mark.asyncio
    async def test_chaos_outcome_reflects_remediation_action(self):
        alert = self._chaos_alert()

        with patch("main.build_rag_query", return_value="query"), \
             patch("main.retrieve_context", new_callable=AsyncMock, return_value=mock_rag_context()), \
             patch("main.generate_diagnosis", new_callable=AsyncMock, return_value=mock_diagnosis_escalate()), \
             patch("main.process_remediation", new_callable=AsyncMock, return_value={"action": main.RemediationAction.ESCALATE, "safe_commands": [], "reason": "r", "reason_code": "rc"}), \
             patch("main.ingest_incident", new_callable=AsyncMock), \
             patch("main.send_mattermost_alert", new_callable=AsyncMock):
            before = _get_counter("aiops_chaos_experiment", {"experiment": "KubePodOOMKilled", "outcome": "escalate"})
            await _process_alert_with_diagnosis(alert, AsyncMock(), None)
            after = _get_counter("aiops_chaos_experiment", {"experiment": "KubePodOOMKilled", "outcome": "escalate"})

        assert after == before + 1

    @pytest.mark.asyncio
    async def test_chaos_metrics_bad_image_alertname(self):
        alert = self._chaos_alert(alertname="KubePodImagePullBackOff")
        before = _get_counter("aiops_chaos_experiment", {"experiment": "KubePodImagePullBackOff", "outcome": "no_diagnosis"})

        with patch("main.build_rag_query", return_value="query"), \
             patch("main.retrieve_context", new_callable=AsyncMock, return_value=mock_rag_context()), \
             patch("main.generate_diagnosis", new_callable=AsyncMock, side_effect=Exception("llm down")), \
             patch("main.send_mattermost_alert", new_callable=AsyncMock):
            await _process_alert_with_diagnosis(alert, AsyncMock(), None)

        after = _get_counter("aiops_chaos_experiment", {"experiment": "KubePodImagePullBackOff", "outcome": "no_diagnosis"})
        assert after == before + 1

    @pytest.mark.asyncio
    async def test_chaos_metrics_high_cpu_alertname(self):
        alert = self._chaos_alert(alertname="HighCPU")
        before = _get_counter("aiops_chaos_experiment", {"experiment": "HighCPU", "outcome": "no_diagnosis"})

        with patch("main.build_rag_query", return_value="query"), \
             patch("main.retrieve_context", new_callable=AsyncMock, return_value=mock_rag_context()), \
             patch("main.generate_diagnosis", new_callable=AsyncMock, side_effect=Exception("llm down")), \
             patch("main.send_mattermost_alert", new_callable=AsyncMock):
            await _process_alert_with_diagnosis(alert, AsyncMock(), None)

        after = _get_counter("aiops_chaos_experiment", {"experiment": "HighCPU", "outcome": "no_diagnosis"})
        assert after == before + 1


# ── POST /webhook/command ─────────────────────────────────────────────────────

def _make_incidents_chroma(n: int = 10) -> MagicMock:
    """ChromaDB mock con n incidents ordenados de forma no-cronológica para verificar el sort."""
    mock_client = MagicMock()
    coll = MagicMock()
    coll.count.return_value = n
    ids = [f"incident-OOMKilled-{1000 + i}" for i in range(n)]
    docs = [f"Alert: OOMKilled incident {i}" for i in range(n)]
    # timestamps scrambled — test must verify sorted output
    metadatas = [
        {
            "timestamp": str(1000 + (n - i)),  # descending order reversed: oldest first in list
            "outcome": "escalate",
            "error_class": "OOMKilled",
            "confidence": "0.9",
        }
        for i in range(n)
    ]
    coll.get.return_value = {"ids": ids, "documents": docs, "metadatas": metadatas}
    mock_client.get_or_create_collection.return_value = coll
    return mock_client


class TestSlashCommandEndpoint:
    """POST /webhook/command — slash commands /aiops de Mattermost."""

    def test_status_returns_ephemeral_ollama_up(self, api_client):
        """Happy path: Ollama responde 200, ChromaDB not None → status UP."""
        mock_http = mock_http_client("")
        with patch.object(app.state, "http_client", mock_http), \
             patch.object(app.state, "chroma_client", MagicMock()):
            r = api_client.post("/webhook/command", data={
                "command": "/aiops", "text": "status",
            })
        assert r.status_code == 200
        body = r.json()
        assert body["response_type"] == "ephemeral"
        assert "UP" in body["text"]
        assert "AIOps Agent" in body["text"]

    def test_status_marks_ollama_down_when_unreachable(self, api_client):
        """Ollama down → status shows DOWN, still 200 ephemeral."""
        with patch.object(app.state, "http_client", mock_ollama_unreachable()), \
             patch.object(app.state, "chroma_client", MagicMock()):
            r = api_client.post("/webhook/command", data={
                "command": "/aiops", "text": "status",
            })
        assert r.status_code == 200
        body = r.json()
        assert body["response_type"] == "ephemeral"
        assert "DOWN" in body["text"]

    def test_incidents_default_5_sorted_by_timestamp_desc(self, api_client):
        """incidents without N → returns 5 entries sorted by timestamp desc."""
        mock_chroma = _make_incidents_chroma(n=10)
        with patch.object(app.state, "chroma_client", mock_chroma):
            r = api_client.post("/webhook/command", data={
                "command": "/aiops", "text": "incidents",
            })
        assert r.status_code == 200
        body = r.json()
        assert body["response_type"] == "ephemeral"
        lines = [l for l in body["text"].split("\n") if l.startswith("-")]
        assert len(lines) == 5
        # verify descending order: first timestamp > second timestamp
        timestamps = [int(re.search(r"`(\d+)`", l).group(1)) for l in lines]
        assert timestamps == sorted(timestamps, reverse=True)

    def test_incidents_n_argument_clamps_to_20(self, api_client):
        """incidents 100 → clamped to 20, no error."""
        mock_chroma = _make_incidents_chroma(n=25)
        with patch.object(app.state, "chroma_client", mock_chroma):
            r = api_client.post("/webhook/command", data={
                "command": "/aiops", "text": "incidents 100",
            })
        assert r.status_code == 200
        body = r.json()
        lines = [l for l in body["text"].split("\n") if l.startswith("-")]
        assert len(lines) == 20

    def test_incidents_n_1_returns_single_entry(self, api_client):
        """incidents 1 → exactly 1 row."""
        mock_chroma = _make_incidents_chroma(n=5)
        with patch.object(app.state, "chroma_client", mock_chroma):
            r = api_client.post("/webhook/command", data={
                "command": "/aiops", "text": "incidents 1",
            })
        assert r.status_code == 200
        lines = [l for l in r.json()["text"].split("\n") if l.startswith("-")]
        assert len(lines) == 1

    def test_incidents_invalid_n_returns_error_ephemeral(self, api_client):
        """incidents abc → ephemeral error, not 422."""
        r = api_client.post("/webhook/command", data={
            "command": "/aiops", "text": "incidents abc",
        })
        assert r.status_code == 200
        body = r.json()
        assert body["response_type"] == "ephemeral"
        assert "Invalid N" in body["text"]
        assert "abc" in body["text"]

    def test_help_returns_help_text(self, api_client):
        """text=help → table with all subcommands."""
        r = api_client.post("/webhook/command", data={
            "command": "/aiops", "text": "help",
        })
        assert r.status_code == 200
        body = r.json()
        assert body["response_type"] == "ephemeral"
        assert "status" in body["text"]
        assert "incidents" in body["text"]

    def test_empty_text_defaults_to_help(self, api_client):
        """No text → treated as help."""
        r = api_client.post("/webhook/command", data={
            "command": "/aiops", "text": "",
        })
        assert r.status_code == 200
        assert "incidents" in r.json()["text"]

    def test_unknown_subcommand_returns_help_fallback(self, api_client):
        """Unknown subcommand → help text included in response."""
        r = api_client.post("/webhook/command", data={
            "command": "/aiops", "text": "foobar",
        })
        assert r.status_code == 200
        body = r.json()
        assert body["response_type"] == "ephemeral"
        assert "foobar" in body["text"] or "Unknown" in body["text"]
        assert "status" in body["text"]

    def test_missing_token_returns_401_when_secret_set(self, api_client):
        """MM_COMMAND_TOKEN set, no token field → 401."""
        with patch.object(main.settings, "mm_command_token", "real-token"):
            r = api_client.post("/webhook/command", data={
                "command": "/aiops", "text": "help",
            })
        assert r.status_code == 401

    def test_wrong_token_returns_401_when_secret_set(self, api_client):
        """MM_COMMAND_TOKEN set, wrong token → 401."""
        with patch.object(main.settings, "mm_command_token", "real-token"):
            r = api_client.post("/webhook/command", data={
                "token": "wrong-token", "command": "/aiops", "text": "help",
            })
        assert r.status_code == 401

    def test_correct_token_passes_when_secret_set(self, api_client):
        """MM_COMMAND_TOKEN set, correct token → 200."""
        with patch.object(main.settings, "mm_command_token", "real-token"):
            r = api_client.post("/webhook/command", data={
                "token": "real-token", "command": "/aiops", "text": "help",
            })
        assert r.status_code == 200

    def test_no_token_passes_when_secret_unset(self, api_client):
        """MM_COMMAND_TOKEN empty → fail-open, request processed without token."""
        with patch.object(main.settings, "mm_command_token", ""):
            r = api_client.post("/webhook/command", data={
                "command": "/aiops", "text": "help",
            })
        assert r.status_code == 200

    def test_incidents_empty_chromadb_returns_no_incidents_message(self, api_client):
        """ChromaDB collection empty → friendly 'no incidents' message."""
        mock_chroma = MagicMock()
        coll = MagicMock()
        coll.count.return_value = 0
        mock_chroma.get_or_create_collection.return_value = coll
        with patch.object(app.state, "chroma_client", mock_chroma):
            r = api_client.post("/webhook/command", data={
                "command": "/aiops", "text": "incidents",
            })
        assert r.status_code == 200
        assert "No incidents" in r.json()["text"]

    def test_incidents_chromadb_none_returns_no_incidents_message(self, api_client):
        """chroma_client is None (down at startup) → fail-open, no crash."""
        with patch.object(app.state, "chroma_client", None):
            r = api_client.post("/webhook/command", data={
                "command": "/aiops", "text": "incidents",
            })
        assert r.status_code == 200
        assert "No incidents" in r.json()["text"]
