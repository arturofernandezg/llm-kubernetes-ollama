"""
Tests del mecanismo de rollback automático (Mini-Fase 4, Sesión #5).

Cubre:
- _schedule_rollback_evaluation(): registro en IN_FLIGHT_ROLLBACKS + counter scheduled
- _evaluate_rollback(): healthy path, revert path, revert_failed, evaluation_error
- Wire en _process_alert_with_diagnosis(): rollback skipped cuando no aplica
"""

import asyncio
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import main
from main import (
    IN_FLIGHT_ROLLBACKS, _ROLLBACK_LOCK,
    _schedule_rollback_evaluation, _evaluate_rollback,
    _process_alert_with_diagnosis,
    ROLLBACK_COUNTER,
)
from remediation import PrePatchSnapshot, PodHealthStatus, ExecuteResult
from schemas import AlertItem
from tests.helpers import (
    mock_chroma_client, mock_rag_context,
    mock_diagnosis_auto_remediate, mock_diagnosis_escalate,
)


# ── Fixtures ──────────────────────────────────────────────────────────────────

def _make_alert(namespace: str = "arturo-llm-test") -> AlertItem:
    return AlertItem(
        status="firing",
        labels={"alertname": "KubePodOOMKilled", "severity": "critical",
                "pod": "engine-pod", "namespace": namespace},
        annotations={"description": "OOMKilled"},
        startsAt="2026-05-19T10:00:00Z",
    )


def _make_snapshot(value: str = "256Mi") -> PrePatchSnapshot:
    return PrePatchSnapshot(
        deployment="engine", namespace="arturo-llm-test", container="engine",
        field="resources.limits.memory", value=value, selector="app=engine",
    )


def _counter_value(outcome: str) -> float:
    """Read current counter value for a given outcome label."""
    for metric in ROLLBACK_COUNTER.collect():
        for sample in metric.samples:
            if sample.labels.get("outcome") == outcome:
                return sample.value
    return 0.0


# ── TestRollbackScheduling ────────────────────────────────────────────────────

class TestRollbackScheduling:

    @pytest.mark.asyncio
    async def test_schedule_populates_registry_and_increments_counter(self):
        IN_FLIGHT_ROLLBACKS.clear()
        before = _counter_value("scheduled")
        incident_id = "test-schedule-001"
        snapshot = _make_snapshot()
        alert = _make_alert()

        with patch("main.asyncio.create_task"):
            await _schedule_rollback_evaluation(
                incident_id, snapshot, alert, mock_diagnosis_auto_remediate()
            )

        assert incident_id in IN_FLIGHT_ROLLBACKS
        assert IN_FLIGHT_ROLLBACKS[incident_id].snapshot is snapshot
        assert _counter_value("scheduled") == before + 1
        IN_FLIGHT_ROLLBACKS.clear()

    @pytest.mark.asyncio
    async def test_schedule_creates_task(self):
        IN_FLIGHT_ROLLBACKS.clear()
        with patch("main.asyncio.create_task") as mock_create_task:
            await _schedule_rollback_evaluation(
                "test-task-001", _make_snapshot(), _make_alert(), {}
            )
        mock_create_task.assert_called_once()
        IN_FLIGHT_ROLLBACKS.clear()


# ── TestEvaluateRollback ──────────────────────────────────────────────────────

class TestEvaluateRollback:

    def _setup_context(self, incident_id: str, snapshot: PrePatchSnapshot | None = None):
        """Insert a rollback context into the registry."""
        IN_FLIGHT_ROLLBACKS[incident_id] = main.RollbackContext(
            incident_id=incident_id,
            snapshot=snapshot or _make_snapshot(),
            alert_item=_make_alert(),
            diagnosis=mock_diagnosis_auto_remediate(),
            scheduled_at=datetime.now(timezone.utc),
        )

    @pytest.mark.asyncio
    async def test_healthy_path_increments_counter_and_sends_mm(self, monkeypatch):
        """Pod healthy after timeout → counter=healthy, MM message, registry cleared."""
        monkeypatch.setattr("main.settings.remediation_rollback_timeout", 0)
        IN_FLIGHT_ROLLBACKS.clear()
        incident_id = "eval-healthy-001"
        self._setup_context(incident_id)
        before = _counter_value("healthy")

        healthy_status = PodHealthStatus(
            healthy=True, reason="all_pods_running_no_restarts",
            observed_phases=["Running"], observed_restarts=[0],
        )
        with patch("main.check_pod_health", new=AsyncMock(return_value=healthy_status)), \
             patch("main.send_mattermost_alert", new=AsyncMock(return_value=True)) as mock_mm:
            await _evaluate_rollback(incident_id)

        assert _counter_value("healthy") == before + 1
        mock_mm.assert_called_once()
        assert incident_id not in IN_FLIGHT_ROLLBACKS

    @pytest.mark.asyncio
    async def test_unhealthy_path_reverts_and_increments_reverted(self, monkeypatch):
        """Pod unhealthy → revert executes → counter=reverted, MM message."""
        monkeypatch.setattr("main.settings.remediation_rollback_timeout", 0)
        IN_FLIGHT_ROLLBACKS.clear()
        incident_id = "eval-revert-001"
        self._setup_context(incident_id)
        before = _counter_value("reverted")

        unhealthy = PodHealthStatus(
            healthy=False, reason="pods_restarting: [2]",
            observed_phases=["Running"], observed_restarts=[2],
        )
        ok_result = ExecuteResult(
            command="kubectl set resources ...", success=True,
            stdout="updated", stderr="", exit_code=0, outcome="ok",
        )
        with patch("main.check_pod_health", new=AsyncMock(return_value=unhealthy)), \
             patch("main.revert_patch", new=AsyncMock(return_value=ok_result)), \
             patch("main.send_mattermost_alert", new=AsyncMock(return_value=True)) as mock_mm:
            await _evaluate_rollback(incident_id)

        assert _counter_value("reverted") == before + 1
        mock_mm.assert_called_once()
        assert "Rollback executed" in mock_mm.call_args[0][0]
        assert incident_id not in IN_FLIGHT_ROLLBACKS

    @pytest.mark.asyncio
    async def test_unhealthy_path_revert_fails_increments_revert_failed(self, monkeypatch):
        monkeypatch.setattr("main.settings.remediation_rollback_timeout", 0)
        IN_FLIGHT_ROLLBACKS.clear()
        incident_id = "eval-revert-fail-001"
        self._setup_context(incident_id)
        before = _counter_value("revert_failed")

        unhealthy = PodHealthStatus(
            healthy=False, reason="pods_not_running: ['Failed']",
            observed_phases=["Failed"], observed_restarts=[0],
        )
        fail_result = ExecuteResult(
            command="kubectl set resources ...", success=False,
            stdout="", stderr="forbidden", exit_code=1, outcome="failed",
        )
        with patch("main.check_pod_health", new=AsyncMock(return_value=unhealthy)), \
             patch("main.revert_patch", new=AsyncMock(return_value=fail_result)), \
             patch("main.send_mattermost_alert", new=AsyncMock(return_value=True)) as mock_mm:
            await _evaluate_rollback(incident_id)

        assert _counter_value("revert_failed") == before + 1
        assert "FAILED" in mock_mm.call_args[0][0]
        assert incident_id not in IN_FLIGHT_ROLLBACKS

    @pytest.mark.asyncio
    async def test_registry_cleared_if_context_missing(self, monkeypatch):
        """Incident already cleaned up by another path → silent exit, no crash."""
        monkeypatch.setattr("main.settings.remediation_rollback_timeout", 0)
        IN_FLIGHT_ROLLBACKS.clear()
        # Don't populate the registry
        await _evaluate_rollback("nonexistent-incident")
        # No exception, no counter change expected

    @pytest.mark.asyncio
    async def test_evaluation_error_increments_counter(self, monkeypatch):
        monkeypatch.setattr("main.settings.remediation_rollback_timeout", 0)
        IN_FLIGHT_ROLLBACKS.clear()
        incident_id = "eval-error-001"
        self._setup_context(incident_id)
        before = _counter_value("evaluation_error")

        with patch("main.check_pod_health", new=AsyncMock(side_effect=RuntimeError("boom"))):
            await _evaluate_rollback(incident_id)

        assert _counter_value("evaluation_error") == before + 1
        assert incident_id not in IN_FLIGHT_ROLLBACKS

    @pytest.mark.asyncio
    async def test_dry_run_pod_always_healthy_no_revert(self, monkeypatch):
        """In dry-run mode, check_pod_health returns healthy=True → no revert called."""
        monkeypatch.setattr("main.settings.remediation_rollback_timeout", 0)
        monkeypatch.setattr("main.settings.remediation_dry_run", True)
        IN_FLIGHT_ROLLBACKS.clear()
        incident_id = "eval-dryrun-001"
        self._setup_context(incident_id)

        # check_pod_health itself reads dry_run — patch the full function
        healthy_status = PodHealthStatus(
            healthy=True, reason="dry_run", observed_phases=[], observed_restarts=[],
        )
        with patch("main.check_pod_health", new=AsyncMock(return_value=healthy_status)), \
             patch("main.revert_patch") as mock_revert, \
             patch("main.send_mattermost_alert", new=AsyncMock(return_value=True)):
            await _evaluate_rollback(incident_id)

        mock_revert.assert_not_called()
        assert incident_id not in IN_FLIGHT_ROLLBACKS


# ── TestProcessAlertRollbackWire ──────────────────────────────────────────────

class TestProcessAlertRollbackWire:
    """_process_alert_with_diagnosis() schedules rollback when AUTO_REMEDIATE fires."""

    def _make_http_client(self):
        client = AsyncMock()
        client.get = AsyncMock(return_value=MagicMock(
            status_code=200,
            json=MagicMock(return_value={"models": [{"name": "tinyllama"}]}),
            raise_for_status=MagicMock(),
        ))
        return client

    @pytest.mark.asyncio
    async def test_rollback_not_scheduled_when_disabled(self, monkeypatch):
        monkeypatch.setattr("main.settings.remediation_rollback_enabled", False)
        monkeypatch.setattr("main.settings.remediation_enabled", True)
        IN_FLIGHT_ROLLBACKS.clear()

        auto_result = {
            "action": main.RemediationAction.AUTO_REMEDIATE,
            "command_validations": [], "safe_commands": [], "blocked_commands": [],
            "execution_attempted": True, "execution_log": "[DRY-RUN] cmd",
            "execute_results": [ExecuteResult("cmd", True, "", "", None, "dry_run")],
            "pre_patch_snapshot": _make_snapshot(),
        }
        with patch("main.process_remediation", new=AsyncMock(return_value=auto_result)), \
             patch("main.generate_diagnosis", new=AsyncMock(return_value=mock_diagnosis_auto_remediate())), \
             patch("main.retrieve_context", new=AsyncMock(return_value=mock_rag_context())), \
             patch("main.ingest_incident", new=AsyncMock()), \
             patch("main.send_mattermost_alert", new=AsyncMock(return_value=True)), \
             patch("main._schedule_rollback_evaluation") as mock_schedule:
            await _process_alert_with_diagnosis(
                _make_alert(), self._make_http_client(), mock_chroma_client()
            )

        mock_schedule.assert_not_called()

    @pytest.mark.asyncio
    async def test_rollback_not_scheduled_when_no_snapshot(self, monkeypatch):
        monkeypatch.setattr("main.settings.remediation_rollback_enabled", True)
        IN_FLIGHT_ROLLBACKS.clear()
        before = _counter_value("skipped_no_snapshot")

        auto_result = {
            "action": main.RemediationAction.AUTO_REMEDIATE,
            "command_validations": [], "safe_commands": [], "blocked_commands": [],
            "execution_attempted": True, "execution_log": "[DRY-RUN] cmd",
            "execute_results": [ExecuteResult("cmd", True, "", "", None, "dry_run")],
            "pre_patch_snapshot": None,
        }
        with patch("main.process_remediation", new=AsyncMock(return_value=auto_result)), \
             patch("main.generate_diagnosis", new=AsyncMock(return_value=mock_diagnosis_auto_remediate())), \
             patch("main.retrieve_context", new=AsyncMock(return_value=mock_rag_context())), \
             patch("main.ingest_incident", new=AsyncMock()), \
             patch("main.send_mattermost_alert", new=AsyncMock(return_value=True)), \
             patch("main._schedule_rollback_evaluation") as mock_schedule:
            await _process_alert_with_diagnosis(
                _make_alert(), self._make_http_client(), mock_chroma_client()
            )

        mock_schedule.assert_not_called()
        assert _counter_value("skipped_no_snapshot") == before + 1

    @pytest.mark.asyncio
    async def test_rollback_not_scheduled_for_escalate(self, monkeypatch):
        monkeypatch.setattr("main.settings.remediation_rollback_enabled", True)
        IN_FLIGHT_ROLLBACKS.clear()

        escalate_result = {
            "action": main.RemediationAction.ESCALATE,
            "command_validations": [], "safe_commands": ["kubectl get pods"], "blocked_commands": [],
            "execution_attempted": False, "execution_log": "",
            "execute_results": [],
            "pre_patch_snapshot": None,
        }
        with patch("main.process_remediation", new=AsyncMock(return_value=escalate_result)), \
             patch("main.generate_diagnosis", new=AsyncMock(return_value=mock_diagnosis_escalate())), \
             patch("main.retrieve_context", new=AsyncMock(return_value=mock_rag_context())), \
             patch("main.ingest_incident", new=AsyncMock()), \
             patch("main.send_escalation_with_buttons", new=AsyncMock()), \
             patch("main._schedule_rollback_evaluation") as mock_schedule:
            await _process_alert_with_diagnosis(
                _make_alert(), self._make_http_client(), mock_chroma_client()
            )

        mock_schedule.assert_not_called()
