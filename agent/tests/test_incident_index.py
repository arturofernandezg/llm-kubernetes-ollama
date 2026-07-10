"""
Tests de R5 (bucle observacional): correlación de alertas `resolved` con el
incidente que las disparó, por fingerprint.

Cubre:
- incident_index.py: record/pop roundtrip, pop consume (delete), fail-open.
- main._correlate_resolution(): hit no-auto → re-upsert resolved_observed + métrica;
  hit awaits_verdict → solo métrica (el veredicto del rollback es dueño del outcome);
  miss → métrica miss; fail-open ante ingest/redis caídos.
- main._alert_fingerprint(): fórmula única compartida firing/resolved.
- Wire en _process_alert_with_diagnosis(): un incidente ingerido queda indexado.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import main
from main import _correlate_resolution, _alert_fingerprint, _process_alert_with_diagnosis
from incident_index import record_active_incident, pop_active_incident
from rag import INCIDENT_OUTCOME_RESOLVED_OBSERVED
from schemas import AlertItem
from tests.helpers import (
    FakeRedis, mock_chroma_client, mock_rag_context, mock_diagnosis_escalate,
)


# ── Fixtures / lectores de métrica ────────────────────────────────────────────

def _make_alert(status: str = "resolved") -> AlertItem:
    return AlertItem(
        status=status,
        labels={"alertname": "KubePodCrashLooping", "severity": "warning",
                "pod": "engine-pod", "namespace": "arturo-llm-test"},
        annotations={"description": "CrashLoopBackOff"},
        startsAt="2026-07-10T10:00:00Z",
    )


def _active_payload(*, awaits_verdict: bool, started_at: float | None = None,
                    doc_id: str = "incident-KubePodCrashLooping-1700000000") -> dict:
    return {
        "doc_id": doc_id,
        "error_class": "CrashLoopBackOff",
        "started_at": started_at if started_at is not None else main.time.time() - 42.0,
        "awaits_verdict": awaits_verdict,
        "text": "Alert: KubePodCrashLooping on pod engine-pod\nAction: escalate",
        "metadata": {"error_class": "CrashLoopBackOff", "outcome": "escalate",
                     "fix_applied": "none", "confidence": 0.9, "risk": "high",
                     "timestamp": "1700000000"},
    }


def _res_counter(correlated: str) -> float:
    for metric in main.INCIDENT_RESOLUTION_COUNTER.collect():
        for sample in metric.samples:
            if sample.labels.get("correlated") == correlated:
                return sample.value
    return 0.0


def _hist_count(error_class: str) -> float:
    for metric in main.INCIDENT_RESOLUTION_HISTOGRAM.collect():
        for sample in metric.samples:
            if sample.name.endswith("_count") and sample.labels.get("error_class") == error_class:
                return sample.value
    return 0.0


def _verdict_counter(outcome: str) -> float:
    for metric in main.FEEDBACK_VERDICT_COUNTER.collect():
        for sample in metric.samples:
            if sample.labels.get("outcome") == outcome:
                return sample.value
    return 0.0


# ── incident_index.py ─────────────────────────────────────────────────────────

class TestIncidentIndex:
    @pytest.mark.asyncio
    async def test_record_and_pop_roundtrip(self):
        redis = FakeRedis()
        payload = _active_payload(awaits_verdict=False)
        assert await record_active_incident("fp-1", payload, 3600, redis) is True
        got = await pop_active_incident("fp-1", redis)
        assert got == payload

    @pytest.mark.asyncio
    async def test_pop_consumes_entry(self):
        redis = FakeRedis()
        await record_active_incident("fp-1", _active_payload(awaits_verdict=False), 3600, redis)
        assert await pop_active_incident("fp-1", redis) is not None
        # Second pop finds nothing — a resolution is consumed exactly once.
        assert await pop_active_incident("fp-1", redis) is None

    @pytest.mark.asyncio
    async def test_pop_miss_returns_none(self):
        assert await pop_active_incident("nope", FakeRedis()) is None

    @pytest.mark.asyncio
    async def test_none_redis_is_safe(self):
        assert await record_active_incident("fp", {}, 3600, None) is False
        assert await pop_active_incident("fp", None) is None

    @pytest.mark.asyncio
    async def test_record_fail_open_on_error(self):
        redis = AsyncMock()
        redis.setex.side_effect = RuntimeError("redis down")
        assert await record_active_incident("fp", {}, 3600, redis) is False

    @pytest.mark.asyncio
    async def test_pop_fail_open_on_error(self):
        redis = AsyncMock()
        redis.get.side_effect = RuntimeError("redis down")
        assert await pop_active_incident("fp", redis) is None


# ── main._alert_fingerprint ────────────────────────────────────────────────────

class TestAlertFingerprint:
    def test_fingerprint_formula(self):
        assert _alert_fingerprint(_make_alert()) == "KubePodCrashLooping:arturo-llm-test:engine-pod"

    def test_firing_and_resolved_agree(self):
        # Same labels → same fingerprint regardless of status (the correlation contract).
        firing = _make_alert(status="firing")
        resolved = _make_alert(status="resolved")
        assert _alert_fingerprint(firing) == _alert_fingerprint(resolved)


# ── main._correlate_resolution ─────────────────────────────────────────────────

class TestCorrelateResolution:
    def _prime_state(self, redis):
        main.app.state.redis = redis
        main.app.state.http_client = AsyncMock()
        main.app.state.chroma_client = object()

    @pytest.mark.asyncio
    async def test_hit_non_auto_reupserts_resolved_observed(self):
        redis = FakeRedis()
        self._prime_state(redis)
        await record_active_incident("fp-1", _active_payload(awaits_verdict=False), 3600, redis)
        hit_before = _res_counter("hit")
        verdict_before = _verdict_counter(INCIDENT_OUTCOME_RESOLVED_OBSERVED)
        hist_before = _hist_count("CrashLoopBackOff")

        with patch("main.ingest_incident", new=AsyncMock()) as mock_ingest:
            await _correlate_resolution("fp-1")

        mock_ingest.assert_called_once()
        assert mock_ingest.call_args.kwargs["metadata"]["outcome"] == INCIDENT_OUTCOME_RESOLVED_OBSERVED
        assert mock_ingest.call_args.kwargs["doc_id"] == "incident-KubePodCrashLooping-1700000000"
        assert "Resolution:" in mock_ingest.call_args.kwargs["text"]
        assert _res_counter("hit") == hit_before + 1
        assert _verdict_counter(INCIDENT_OUTCOME_RESOLVED_OBSERVED) == verdict_before + 1
        assert _hist_count("CrashLoopBackOff") == hist_before + 1

    @pytest.mark.asyncio
    async def test_hit_awaiting_verdict_metric_only(self):
        # The rollback verdict owns the outcome — resolution must NOT re-upsert.
        redis = FakeRedis()
        self._prime_state(redis)
        await record_active_incident("fp-2", _active_payload(awaits_verdict=True), 3600, redis)
        hist_before = _hist_count("CrashLoopBackOff")

        with patch("main.ingest_incident", new=AsyncMock()) as mock_ingest:
            await _correlate_resolution("fp-2")

        mock_ingest.assert_not_called()
        assert _hist_count("CrashLoopBackOff") == hist_before + 1  # metric still emitted

    @pytest.mark.asyncio
    async def test_miss_increments_miss_counter(self):
        redis = FakeRedis()
        self._prime_state(redis)
        miss_before = _res_counter("miss")

        with patch("main.ingest_incident", new=AsyncMock()) as mock_ingest:
            await _correlate_resolution("no-such-fp")

        mock_ingest.assert_not_called()
        assert _res_counter("miss") == miss_before + 1

    @pytest.mark.asyncio
    async def test_hit_missing_doc_fields_skips_reupsert(self):
        redis = FakeRedis()
        self._prime_state(redis)
        payload = _active_payload(awaits_verdict=False)
        del payload["metadata"]  # incomplete record → cannot re-upsert
        await record_active_incident("fp-3", payload, 3600, redis)

        with patch("main.ingest_incident", new=AsyncMock()) as mock_ingest:
            await _correlate_resolution("fp-3")  # must not raise

        mock_ingest.assert_not_called()

    @pytest.mark.asyncio
    async def test_reupsert_failure_is_fail_open(self):
        redis = FakeRedis()
        self._prime_state(redis)
        await record_active_incident("fp-4", _active_payload(awaits_verdict=False), 3600, redis)

        with patch("main.ingest_incident", new=AsyncMock(side_effect=RuntimeError("chroma down"))):
            await _correlate_resolution("fp-4")  # must not raise

    @pytest.mark.asyncio
    async def test_bad_started_at_still_reupserts(self):
        # A non-numeric started_at must not crash; the outcome flip still happens.
        redis = FakeRedis()
        self._prime_state(redis)
        payload = _active_payload(awaits_verdict=False)
        payload["started_at"] = "not-a-number"
        await record_active_incident("fp-5", payload, 3600, redis)

        with patch("main.ingest_incident", new=AsyncMock()) as mock_ingest:
            await _correlate_resolution("fp-5")

        mock_ingest.assert_called_once()
        assert mock_ingest.call_args.kwargs["metadata"]["outcome"] == INCIDENT_OUTCOME_RESOLVED_OBSERVED


# ── Wire en _process_alert_with_diagnosis ──────────────────────────────────────

class TestProcessAlertIndexesIncident:
    """A worth-ingesting incident is recorded in the fingerprint index; a non-auto
    (escalate) incident carries awaits_verdict=False so a later resolve can close it."""

    def _make_http_client(self):
        client = AsyncMock()
        client.get = AsyncMock(return_value=MagicMock(
            status_code=200,
            json=MagicMock(return_value={"models": [{"name": "tinyllama"}]}),
            raise_for_status=MagicMock(),
        ))
        return client

    @pytest.mark.asyncio
    async def test_escalate_incident_is_indexed(self, monkeypatch):
        monkeypatch.setattr("main.settings.remediation_rollback_enabled", True)
        redis = FakeRedis()
        alert = AlertItem(
            status="firing",
            labels={"alertname": "KubePodCrashLooping", "severity": "warning",
                    "pod": "engine-pod", "namespace": "arturo-llm-test"},
            annotations={"description": "CrashLoopBackOff"},
            startsAt="2026-07-10T10:00:00Z",
        )
        escalate_result = {
            "action": main.RemediationAction.ESCALATE,
            "command_validations": [], "safe_commands": ["kubectl get pods"],
            "blocked_commands": [], "execution_attempted": False, "execution_log": "",
            "execute_results": [], "pre_patch_snapshot": None, "structured_command": None,
        }
        with patch("main.process_remediation", new=AsyncMock(return_value=escalate_result)), \
             patch("main.generate_diagnosis", new=AsyncMock(return_value=mock_diagnosis_escalate())), \
             patch("main.retrieve_context", new=AsyncMock(return_value=mock_rag_context())), \
             patch("main.ingest_incident", new=AsyncMock()), \
             patch("main.send_escalation_with_buttons", new=AsyncMock()), \
             patch("main._schedule_rollback_evaluation", new=AsyncMock()):
            await _process_alert_with_diagnosis(
                alert, self._make_http_client(), mock_chroma_client(), redis,
            )

        fp = _alert_fingerprint(alert)
        record = await pop_active_incident(fp, redis)
        assert record is not None
        assert record["awaits_verdict"] is False
        assert record["doc_id"]
