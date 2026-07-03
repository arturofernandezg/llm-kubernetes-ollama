"""
Tests del mecanismo de rollback automático (Mini-Fase 4, Sesión #5).

Cubre:
- _schedule_rollback_evaluation(): registro en IN_FLIGHT_ROLLBACKS + counter scheduled
- _evaluate_rollback(): healthy path, revert path, revert_failed, evaluation_error
- Wire en _process_alert_with_diagnosis(): rollback skipped cuando no aplica
"""

import asyncio
import json
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import main
from main import (
    IN_FLIGHT_ROLLBACKS, _ROLLBACK_LOCK,
    _schedule_rollback_evaluation, _evaluate_rollback,
    _recover_rollbacks, _rollback_to_dict, _dict_to_rollback,
    _process_alert_with_diagnosis,
    ROLLBACK_COUNTER,
)
from rollback_store import store_rollback, delete_rollback, list_rollbacks
from remediation import PrePatchSnapshot, PodHealthStatus, ExecuteResult
from schemas import AlertItem
from tests.helpers import (
    FakeRedis,
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


def _verdict_counter_value(outcome: str) -> float:
    """Current FEEDBACK_VERDICT_COUNTER value for an outcome label (R2)."""
    for metric in main.FEEDBACK_VERDICT_COUNTER.collect():
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

        with patch("main.asyncio.create_task", side_effect=lambda c, *a, **k: c.close()):
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
        with patch("main.asyncio.create_task", side_effect=lambda c, *a, **k: c.close()) as mock_create_task:
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


# ── TestRollbackSerialization ─────────────────────────────────────────────────

class TestRollbackSerialization:
    """Round-trip of RollbackContext ↔ dict for Redis persistence (P0·3)."""

    def _make_ctx(self, incident_id: str = "rt-001") -> "main.RollbackContext":
        return main.RollbackContext(
            incident_id=incident_id,
            snapshot=_make_snapshot("512Mi"),
            alert_item=_make_alert(),
            diagnosis=mock_diagnosis_auto_remediate(),
            scheduled_at=datetime.now(timezone.utc),
            doc_id="incident-KubePodOOMKilled-1700000000",
            remediation={"execution_log": "patched", "safe_commands": ["kubectl patch ..."]},
        )

    def test_roundtrip_preserves_fields(self):
        ctx = self._make_ctx()
        restored = _dict_to_rollback(_rollback_to_dict(ctx))
        assert restored.incident_id == ctx.incident_id
        assert restored.snapshot == ctx.snapshot          # dataclass __eq__
        assert restored.alert_item.labels == ctx.alert_item.labels
        assert restored.scheduled_at == ctx.scheduled_at

    def test_roundtrip_preserves_r2_fields(self):
        # R2: doc_id + remediation summary must survive the Redis round-trip so the
        # rollback verdict can re-upsert the same incident document after a restart.
        ctx = self._make_ctx()
        restored = _dict_to_rollback(_rollback_to_dict(ctx))
        assert restored.doc_id == ctx.doc_id
        assert restored.remediation == ctx.remediation

    def test_backcompat_missing_r2_fields(self):
        # A context persisted before R2 has no doc_id/remediation keys; deserialize
        # must default them, not crash.
        legacy = _rollback_to_dict(self._make_ctx())
        del legacy["doc_id"]
        del legacy["remediation"]
        restored = _dict_to_rollback(legacy)
        assert restored.doc_id == ""
        assert restored.remediation == {}

    def test_to_dict_is_json_serializable(self):
        # Must survive json.dumps (the exact path store_rollback takes).
        json.dumps(_rollback_to_dict(self._make_ctx()))


# ── TestRollbackVerdictReupsert (R2) ──────────────────────────────────────────

class TestRollbackVerdictReupsert:
    """R2 (real learning loop): the rollback verdict re-upserts the incident
    document with the final outcome (cured / rolled_back / rollback_failed),
    reusing ctx.doc_id so the same ChromaDB doc is updated in place. Fail-open."""

    _DOC_ID = "incident-KubePodOOMKilled-1700000000"

    def _setup_context(self, incident_id: str, doc_id: str = _DOC_ID):
        IN_FLIGHT_ROLLBACKS[incident_id] = main.RollbackContext(
            incident_id=incident_id,
            snapshot=_make_snapshot(),
            alert_item=_make_alert(),
            diagnosis=mock_diagnosis_auto_remediate(),
            scheduled_at=datetime.now(timezone.utc),
            doc_id=doc_id,
            remediation={"execution_log": "patched",
                         "safe_commands": ["kubectl set resources ..."]},
        )

    def _prime_state(self, monkeypatch):
        monkeypatch.setattr("main.settings.remediation_rollback_timeout", 0)
        monkeypatch.setattr(main.app.state, "http_client", AsyncMock(), raising=False)
        monkeypatch.setattr(main.app.state, "chroma_client", object(), raising=False)

    @pytest.mark.asyncio
    async def test_healthy_reupserts_cured(self, monkeypatch):
        self._prime_state(monkeypatch)
        IN_FLIGHT_ROLLBACKS.clear()
        incident_id = "verdict-cured-001"
        self._setup_context(incident_id)
        before = _verdict_counter_value(main.INCIDENT_OUTCOME_CURED)

        healthy = PodHealthStatus(healthy=True, reason="ok",
                                  observed_phases=["Running"], observed_restarts=[0])
        with patch("main.check_pod_health", new=AsyncMock(return_value=healthy)), \
             patch("main.send_mattermost_alert", new=AsyncMock(return_value=True)), \
             patch("main.ingest_incident", new=AsyncMock()) as mock_ingest:
            await _evaluate_rollback(incident_id)

        mock_ingest.assert_called_once()
        assert mock_ingest.call_args.kwargs["doc_id"] == self._DOC_ID
        assert mock_ingest.call_args.kwargs["metadata"]["outcome"] == main.INCIDENT_OUTCOME_CURED
        assert _verdict_counter_value(main.INCIDENT_OUTCOME_CURED) == before + 1

    @pytest.mark.asyncio
    async def test_reverted_reupserts_rolled_back(self, monkeypatch):
        self._prime_state(monkeypatch)
        IN_FLIGHT_ROLLBACKS.clear()
        incident_id = "verdict-rolled-001"
        self._setup_context(incident_id)

        unhealthy = PodHealthStatus(healthy=False, reason="pods_restarting: [2]",
                                    observed_phases=["Running"], observed_restarts=[2])
        ok_result = ExecuteResult(command="kubectl set resources ...", success=True,
                                  stdout="updated", stderr="", exit_code=0, outcome="ok")
        with patch("main.check_pod_health", new=AsyncMock(return_value=unhealthy)), \
             patch("main.revert_patch", new=AsyncMock(return_value=ok_result)), \
             patch("main.send_mattermost_alert", new=AsyncMock(return_value=True)), \
             patch("main.ingest_incident", new=AsyncMock()) as mock_ingest:
            await _evaluate_rollback(incident_id)

        assert mock_ingest.call_args.kwargs["doc_id"] == self._DOC_ID
        assert mock_ingest.call_args.kwargs["metadata"]["outcome"] == main.INCIDENT_OUTCOME_ROLLED_BACK

    @pytest.mark.asyncio
    async def test_revert_failed_reupserts_rollback_failed(self, monkeypatch):
        self._prime_state(monkeypatch)
        IN_FLIGHT_ROLLBACKS.clear()
        incident_id = "verdict-failed-001"
        self._setup_context(incident_id)

        unhealthy = PodHealthStatus(healthy=False, reason="pods_not_running: ['Failed']",
                                    observed_phases=["Failed"], observed_restarts=[0])
        fail_result = ExecuteResult(command="kubectl set resources ...", success=False,
                                    stdout="", stderr="forbidden", exit_code=1, outcome="failed")
        with patch("main.check_pod_health", new=AsyncMock(return_value=unhealthy)), \
             patch("main.revert_patch", new=AsyncMock(return_value=fail_result)), \
             patch("main.send_mattermost_alert", new=AsyncMock(return_value=True)), \
             patch("main.ingest_incident", new=AsyncMock()) as mock_ingest:
            await _evaluate_rollback(incident_id)

        assert mock_ingest.call_args.kwargs["metadata"]["outcome"] == main.INCIDENT_OUTCOME_ROLLBACK_FAILED

    @pytest.mark.asyncio
    async def test_no_doc_id_skips_reupsert(self, monkeypatch):
        # Back-compat: a context persisted before R2 has no doc_id → no re-upsert,
        # but the rollback flow still completes.
        self._prime_state(monkeypatch)
        IN_FLIGHT_ROLLBACKS.clear()
        incident_id = "verdict-nodoc-001"
        self._setup_context(incident_id, doc_id="")

        healthy = PodHealthStatus(healthy=True, reason="ok",
                                  observed_phases=["Running"], observed_restarts=[0])
        with patch("main.check_pod_health", new=AsyncMock(return_value=healthy)), \
             patch("main.send_mattermost_alert", new=AsyncMock(return_value=True)) as mock_mm, \
             patch("main.ingest_incident", new=AsyncMock()) as mock_ingest:
            await _evaluate_rollback(incident_id)

        mock_ingest.assert_not_called()
        mock_mm.assert_called_once()

    @pytest.mark.asyncio
    async def test_reupsert_failure_is_fail_open(self, monkeypatch):
        # ingest_incident raising must not break the rollback flow already applied.
        self._prime_state(monkeypatch)
        IN_FLIGHT_ROLLBACKS.clear()
        incident_id = "verdict-failopen-001"
        self._setup_context(incident_id)
        before = _counter_value("healthy")

        healthy = PodHealthStatus(healthy=True, reason="ok",
                                  observed_phases=["Running"], observed_restarts=[0])
        with patch("main.check_pod_health", new=AsyncMock(return_value=healthy)), \
             patch("main.send_mattermost_alert", new=AsyncMock(return_value=True)) as mock_mm, \
             patch("main.ingest_incident", new=AsyncMock(side_effect=RuntimeError("chroma down"))):
            await _evaluate_rollback(incident_id)  # must not raise

        assert _counter_value("healthy") == before + 1
        mock_mm.assert_called_once()
        assert incident_id not in IN_FLIGHT_ROLLBACKS


# ── TestRollbackStore ─────────────────────────────────────────────────────────

class TestRollbackStore:
    """rollback_store.py — thin Redis mirror, fail-open like escalation_store."""

    @pytest.mark.asyncio
    async def test_store_and_list_roundtrip(self):
        r = FakeRedis()
        payload = {"incident_id": "s-1", "field": "resources.limits.memory"}
        assert await store_rollback("s-1", payload, 60, r) is True
        assert payload in await list_rollbacks(r)

    @pytest.mark.asyncio
    async def test_delete_removes_entry(self):
        r = FakeRedis()
        await store_rollback("d-1", {"incident_id": "d-1"}, 60, r)
        await delete_rollback("d-1", r)
        assert await list_rollbacks(r) == []

    @pytest.mark.asyncio
    async def test_store_fail_open_on_redis_error(self):
        r = AsyncMock()
        r.setex.side_effect = Exception("connection refused")
        assert await store_rollback("f-1", {}, 60, r) is False

    @pytest.mark.asyncio
    async def test_list_fail_open_on_redis_error(self):
        r = MagicMock()
        r.scan_iter = MagicMock(side_effect=Exception("connection refused"))
        assert await list_rollbacks(r) == []


# ── TestRollbackDurability ────────────────────────────────────────────────────

class TestRollbackDurability:
    """P0·3: rollback context survives a pod restart via Redis and is recovered."""

    @pytest.mark.asyncio
    async def test_schedule_persists_to_redis(self):
        IN_FLIGHT_ROLLBACKS.clear()
        r = FakeRedis()
        main.app.state.redis = r
        try:
            with patch("main.asyncio.create_task", side_effect=lambda c, *a, **k: c.close()):
                await _schedule_rollback_evaluation(
                    "dur-1", _make_snapshot(), _make_alert(), mock_diagnosis_auto_remediate()
                )
            items = await list_rollbacks(r)
            assert any(d["incident_id"] == "dur-1" for d in items)
        finally:
            main.app.state.redis = None
            IN_FLIGHT_ROLLBACKS.clear()

    @pytest.mark.asyncio
    async def test_evaluate_deletes_from_redis(self, monkeypatch):
        monkeypatch.setattr("main.settings.remediation_rollback_timeout", 0)
        IN_FLIGHT_ROLLBACKS.clear()
        r = FakeRedis()
        main.app.state.redis = r
        incident_id = "dur-eval-1"
        ctx = main.RollbackContext(
            incident_id=incident_id, snapshot=_make_snapshot(), alert_item=_make_alert(),
            diagnosis=mock_diagnosis_auto_remediate(), scheduled_at=datetime.now(timezone.utc),
        )
        IN_FLIGHT_ROLLBACKS[incident_id] = ctx
        await store_rollback(incident_id, _rollback_to_dict(ctx), 60, r)
        healthy = PodHealthStatus(
            healthy=True, reason="ok", observed_phases=["Running"], observed_restarts=[0],
        )
        try:
            with patch("main.check_pod_health", new=AsyncMock(return_value=healthy)), \
                 patch("main.send_mattermost_alert", new=AsyncMock(return_value=True)):
                await _evaluate_rollback(incident_id)
            assert await list_rollbacks(r) == []
        finally:
            main.app.state.redis = None
            IN_FLIGHT_ROLLBACKS.clear()

    @pytest.mark.asyncio
    async def test_recover_rearms_persisted_rollback(self):
        IN_FLIGHT_ROLLBACKS.clear()
        r = FakeRedis()
        main.app.state.redis = r
        incident_id = "recover-1"
        ctx = main.RollbackContext(
            incident_id=incident_id, snapshot=_make_snapshot(), alert_item=_make_alert(),
            diagnosis=mock_diagnosis_auto_remediate(), scheduled_at=datetime.now(timezone.utc),
        )
        await store_rollback(incident_id, _rollback_to_dict(ctx), 60, r)
        try:
            with patch("main.asyncio.create_task", side_effect=lambda c, *a, **k: c.close()) as mock_task:
                await _recover_rollbacks()
            assert incident_id in IN_FLIGHT_ROLLBACKS
            mock_task.assert_called_once()
        finally:
            main.app.state.redis = None
            IN_FLIGHT_ROLLBACKS.clear()

    @pytest.mark.asyncio
    async def test_recover_is_idempotent_when_already_inflight(self):
        IN_FLIGHT_ROLLBACKS.clear()
        r = FakeRedis()
        main.app.state.redis = r
        incident_id = "recover-dup-1"
        ctx = main.RollbackContext(
            incident_id=incident_id, snapshot=_make_snapshot(), alert_item=_make_alert(),
            diagnosis=mock_diagnosis_auto_remediate(), scheduled_at=datetime.now(timezone.utc),
        )
        IN_FLIGHT_ROLLBACKS[incident_id] = ctx  # already in memory
        await store_rollback(incident_id, _rollback_to_dict(ctx), 60, r)
        try:
            with patch("main.asyncio.create_task") as mock_task:
                await _recover_rollbacks()
            mock_task.assert_not_called()  # no double-spawn
        finally:
            main.app.state.redis = None
            IN_FLIGHT_ROLLBACKS.clear()

    @pytest.mark.asyncio
    async def test_recover_noop_without_redis(self):
        IN_FLIGHT_ROLLBACKS.clear()
        main.app.state.redis = None
        await _recover_rollbacks()  # must not raise
        assert len(IN_FLIGHT_ROLLBACKS) == 0
