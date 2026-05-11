"""
Tests de los endpoints del agente AIOps.

Cubre: GET /healthz, GET /readyz, GET /health, POST /extract, GET /metrics.
Todos los tests usan mocks de Ollama (no requieren cluster ni LLM).
"""

import json
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import httpx as _httpx
import pytest

import main
from main import (
    app, _format_diagnosis_message, _format_escalation_body, _extract_alert_meta,
    PENDING_ESCALATIONS, PendingEscalation, _cleanup_expired_escalations,
)
from mattermost import make_hmac_token
from schemas import AlertItem
from tests.helpers import (
    VALID_PARAMS, VALID_JSON_STR,
    mock_http_client, mock_ollama_unreachable, mock_ollama_model_not_loaded,
    mock_http_client_with_retries,
    mock_chroma_client, mock_rag_context, mock_diagnosis_result,
    mock_diagnosis_auto_remediate, mock_diagnosis_escalate,
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
    """GET /health — deprecated, redirige a /readyz (307)."""

    def test_health_redirects_to_readyz(self, api_client):
        """Sin follow_redirects, devuelve 307 con Location: /readyz."""
        r = api_client.get("/health", follow_redirects=False)
        assert r.status_code == 307
        assert r.headers["location"] == "/readyz"

    def test_health_follows_redirect_when_ollama_ok(self, api_client):
        """Con follow_redirects (default), acaba en /readyz y devuelve 200."""
        with patch.object(app.state, "http_client", mock_http_client("")):
            r = api_client.get("/health")
        assert r.status_code == 200
        data = r.json()
        assert data["status"] == "ready"
        assert data["model_loaded"] is True


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

    def test_webhook_firing_queues_diagnosis_task(self, api_client):
        """Alerta firing encola _process_alert_with_diagnosis como background task."""
        with patch("main._process_alert_with_diagnosis", new_callable=AsyncMock) as mock_task:
            r = api_client.post("/webhook/alert", json=FIRING_PAYLOAD)
        assert r.status_code == 200
        assert r.json()["alerts_processed"] == 1
        mock_task.assert_called_once()

    def test_webhook_resolved_skips_diagnosis(self, api_client):
        """Alerta resolved NO encola diagnosis — va directo a Mattermost como texto simple."""
        with patch("main._process_alert_with_diagnosis", new_callable=AsyncMock) as mock_diag, \
             patch("main.send_mattermost_alert", new_callable=AsyncMock):
            r = api_client.post("/webhook/alert", json=RESOLVED_PAYLOAD)
        assert r.status_code == 200
        mock_diag.assert_not_called()

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


class TestActionCallbackEndpoint:
    """POST /webhook/action — callbacks de botones interactivos de Mattermost."""

    def setup_method(self):
        PENDING_ESCALATIONS.clear()

    def teardown_method(self):
        PENDING_ESCALATIONS.clear()

    def test_approve_executes_commands_and_returns_update(self, api_client):
        """Approve → execute_commands llamado, respuesta con 'update' que limpia botones."""
        PENDING_ESCALATIONS["abc-123"] = _make_pending_escalation("abc-123")

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
        assert "abc-123" not in PENDING_ESCALATIONS

    def test_reject_does_not_execute_commands(self, api_client):
        """Reject → execute_commands NO llamado, mensaje de rechazo en update."""
        PENDING_ESCALATIONS["abc-456"] = _make_pending_escalation("abc-456")

        with patch("main.execute_commands", new_callable=AsyncMock) as mock_exec, \
             patch("main.ingest_incident", new_callable=AsyncMock):
            r = api_client.post("/webhook/action", json={
                "user_name": "arturo",
                "context": {"action": "reject", "incident_id": "abc-456"},
            })

        assert r.status_code == 200
        body = r.json()
        assert "update" in body
        # El mensaje ahora incluye el contexto completo + decisión en mayúsculas al final
        assert "RECHAZADA" in body["update"]["message"]
        assert "arturo" in body["update"]["message"]
        mock_exec.assert_not_called()
        assert "abc-456" not in PENDING_ESCALATIONS

    def test_unknown_incident_id_returns_ephemeral_text(self, api_client):
        """incident_id no encontrado → ephemeral_text + update que limpia los botones."""
        r = api_client.post("/webhook/action", json={
            "context": {"action": "approve", "incident_id": "no-existe"},
        })
        assert r.status_code == 200
        body = r.json()
        assert "ephemeral_text" in body
        # Ahora también devuelve update para limpiar los botones del mensaje original
        assert "update" in body
        assert body["update"]["props"]["attachments"] == []

    def test_expired_escalation_returns_ephemeral_text(self, api_client):
        """Escalación con TTL expirado → ephemeral_text, execute_commands no llamado."""
        PENDING_ESCALATIONS["old-xyz"] = _make_pending_escalation("old-xyz", ttl_minutes=-1)

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
        PENDING_ESCALATIONS["cb-789"] = _make_pending_escalation("cb-789")

        with patch("main.execute_commands", new_callable=AsyncMock, return_value="[DRY-RUN] done"), \
             patch("main.ingest_incident", new_callable=AsyncMock) as mock_ingest:
            api_client.post("/webhook/action", json={
                "user_name": "arturo",
                "context": {"action": "approve", "incident_id": "cb-789"},
            })

        mock_ingest.assert_called_once()
        _, kwargs = mock_ingest.call_args
        assert kwargs["metadata"]["outcome"] == "auto_remediate"

    def test_expired_escalation_is_removed_from_pending(self, api_client):
        """Expired TTL callback removes entry from PENDING_ESCALATIONS (no zombie entries)."""
        PENDING_ESCALATIONS["expired-abc"] = _make_pending_escalation("expired-abc", ttl_minutes=-1)

        with patch("main.execute_commands", new_callable=AsyncMock):
            api_client.post("/webhook/action", json={
                "context": {"action": "approve", "incident_id": "expired-abc"},
            })

        assert "expired-abc" not in PENDING_ESCALATIONS

    def test_duplicate_callback_same_incident_is_idempotent(self, api_client):
        """Second callback for same incident_id → not-found response (entry already popped)."""
        PENDING_ESCALATIONS["dup-123"] = _make_pending_escalation("dup-123")

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
        """When webhook_secret is set, missing hmac_token → 401."""
        PENDING_ESCALATIONS["hmac-001"] = _make_pending_escalation("hmac-001")
        with patch.object(main.settings, "webhook_secret", "test-secret"):
            r = api_client.post("/webhook/action", json={
                "context": {"action": "approve", "incident_id": "hmac-001"},
            })
        assert r.status_code == 401

    def test_invalid_hmac_returns_401_when_secret_set(self, api_client):
        """When webhook_secret is set, wrong hmac_token → 401."""
        PENDING_ESCALATIONS["hmac-002"] = _make_pending_escalation("hmac-002")
        with patch.object(main.settings, "webhook_secret", "test-secret"):
            r = api_client.post("/webhook/action", json={
                "context": {"action": "approve", "incident_id": "hmac-002", "hmac_token": "bad-token"},
            })
        assert r.status_code == 401

    def test_valid_hmac_passes_when_secret_set(self, api_client):
        """When webhook_secret is set, correct hmac_token → request processed normally."""
        PENDING_ESCALATIONS["hmac-003"] = _make_pending_escalation("hmac-003")
        token = make_hmac_token("hmac-003", "reject", "test-secret")
        with patch.object(main.settings, "webhook_secret", "test-secret"), \
             patch("main.ingest_incident", new_callable=AsyncMock):
            r = api_client.post("/webhook/action", json={
                "context": {"action": "reject", "incident_id": "hmac-003", "hmac_token": token},
            })
        assert r.status_code == 200


class TestCleanupExpiredEscalations:
    """Tests directos de _cleanup_expired_escalations() (ahora async)."""

    def setup_method(self):
        PENDING_ESCALATIONS.clear()

    def teardown_method(self):
        PENDING_ESCALATIONS.clear()

    @pytest.mark.asyncio
    async def test_removes_expired_keeps_valid(self):
        """Cleanup elimina expiradas y deja válidas intactas."""
        PENDING_ESCALATIONS["expired"] = _make_pending_escalation("expired", ttl_minutes=-1)
        PENDING_ESCALATIONS["valid"] = _make_pending_escalation("valid", ttl_minutes=60)

        await _cleanup_expired_escalations()

        assert "expired" not in PENDING_ESCALATIONS
        assert "valid" in PENDING_ESCALATIONS

    @pytest.mark.asyncio
    async def test_empty_dict_is_noop(self):
        """Cleanup sobre dict vacío no lanza excepciones."""
        await _cleanup_expired_escalations()
        assert len(PENDING_ESCALATIONS) == 0

    @pytest.mark.asyncio
    async def test_all_expired_clears_dict(self):
        """Todos expirados → dict vacío tras cleanup."""
        PENDING_ESCALATIONS["e1"] = _make_pending_escalation("e1", ttl_minutes=-5)
        PENDING_ESCALATIONS["e2"] = _make_pending_escalation("e2", ttl_minutes=-1)

        await _cleanup_expired_escalations()

        assert len(PENDING_ESCALATIONS) == 0


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
        PENDING_ESCALATIONS.clear()

    def teardown_method(self):
        PENDING_ESCALATIONS.clear()

    def test_unknown_action_returns_400(self, api_client):
        PENDING_ESCALATIONS["m6-test"] = _make_pending_escalation("m6-test")
        r = api_client.post("/webhook/action", json={
            "user_name": "arturo",
            "context": {"action": "unknown_typo", "incident_id": "m6-test"},
        })
        assert r.status_code == 400
        assert "unknown_typo" in r.json()["detail"]

    def test_empty_action_returns_400(self, api_client):
        PENDING_ESCALATIONS["m6-empty"] = _make_pending_escalation("m6-empty")
        r = api_client.post("/webhook/action", json={
            "context": {"action": "", "incident_id": "m6-empty"},
        })
        assert r.status_code == 400


# ── M7: execute_commands failure counter ─────────────────────────────────────

class TestActionCallbackApproveFailure:
    """execute_commands failure: endpoint returns 200 and logs ERROR in message (M7)."""

    def setup_method(self):
        PENDING_ESCALATIONS.clear()

    def teardown_method(self):
        PENDING_ESCALATIONS.clear()

    def test_execute_failure_returns_200_with_error_in_log(self, api_client):
        PENDING_ESCALATIONS["m7-test"] = _make_pending_escalation("m7-test")

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
