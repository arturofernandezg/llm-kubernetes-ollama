"""
Tests del módulo enrichment.py (v2 Eje A / P0·1 — grounding, microtask 1a).

Cubre:
- _kubectl_json(): JSON OK, rc != 0, timeout, JSON inválido, excepción del subprocess
- gather_incident_context(): disabled, labels faltantes, get pod falla (fail-soft)
- parseo: phase, restart_count, last_state_reason (OOMKilled), limits por-container
- selección de container: único, multi+label, multi+OOM

Mockea enrichment.asyncio.create_subprocess_exec (mismo patrón que test_remediation.py).
"""

import asyncio
import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from enrichment import (
    IncidentSnapshot,
    _controller_owner,
    _kubectl_json,
    _gather_logs,
    _gather_events,
    _select_container,
    gather_incident_context,
)


def _make_proc(stdout: bytes = b"", stderr: bytes = b"", returncode: int = 0):
    """Mock de asyncio subprocess (réplica de test_remediation._make_proc)."""
    proc = MagicMock()
    proc.returncode = returncode
    proc.communicate = AsyncMock(return_value=(stdout, stderr))
    proc.kill = MagicMock()
    return proc


def _pod_json(
    phase: str = "Running",
    containers: list[dict] | None = None,
    statuses: list[dict] | None = None,
) -> dict:
    """Construye un pod JSON mínimo con spec.containers + status.containerStatuses."""
    return {
        "spec": {"containers": containers or []},
        "status": {"phase": phase, "containerStatuses": statuses or []},
    }


def _spec_container(name: str, cpu: str | None = None, memory: str | None = None) -> dict:
    limits = {}
    if cpu is not None:
        limits["cpu"] = cpu
    if memory is not None:
        limits["memory"] = memory
    return {"name": name, "resources": {"limits": limits}}


def _owner(kind: str, name: str, controller: bool = True) -> dict:
    return {"kind": kind, "name": name, "controller": controller}


def _pod_owned(kind: str, name: str, container: str = "app") -> dict:
    """Pod JSON owned by a controller (kind/name), single container 'app' at 256Mi."""
    pod = _pod_json(
        containers=[_spec_container(container, memory="256Mi")],
        statuses=[{"name": container, "restartCount": 0}],
    )
    pod["metadata"] = {"ownerReferences": [_owner(kind, name)]}
    return pod


def _selector_json(match_labels: dict) -> dict:
    """Minimal controller JSON exposing .spec.selector.matchLabels."""
    return {"spec": {"selector": {"matchLabels": match_labels}}}


def _seq(*items) -> AsyncMock:
    """AsyncMock for create_subprocess_exec returning one proc per call, in order.

    dict → proc with that JSON (rc=0); int → proc with that returncode and no stdout.
    """
    procs = []
    for item in items:
        if isinstance(item, dict):
            procs.append(_make_proc(stdout=json.dumps(item).encode(), returncode=0))
        else:
            procs.append(_make_proc(stderr=b"NotFound", returncode=item))
    return AsyncMock(side_effect=procs)


# ── _kubectl_json ─────────────────────────────────────────────────────────────

class TestKubectlJson:
    @pytest.mark.asyncio
    async def test_valid_json(self):
        proc = _make_proc(stdout=json.dumps({"kind": "Pod"}).encode(), returncode=0)
        with patch("enrichment.asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)):
            result = await _kubectl_json("get", "pod", "p", "-n", "ns", "-o", "json")
        assert result == {"kind": "Pod"}

    @pytest.mark.asyncio
    async def test_non_zero_returns_none(self):
        proc = _make_proc(stderr=b"NotFound", returncode=1)
        with patch("enrichment.asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)):
            result = await _kubectl_json("get", "pod", "p", "-n", "ns", "-o", "json")
        assert result is None

    @pytest.mark.asyncio
    async def test_timeout_returns_none(self):
        proc = _make_proc()
        proc.communicate = AsyncMock(side_effect=asyncio.TimeoutError)
        with patch("enrichment.asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)):
            result = await _kubectl_json("get", "pod", "p", "-n", "ns", "-o", "json")
        assert result is None

    @pytest.mark.asyncio
    async def test_timeout_kills_leaked_process(self):
        """A timed-out kubectl must be reaped (kill), not left running against a hung API server."""
        proc = _make_proc()
        proc.communicate = AsyncMock(side_effect=asyncio.TimeoutError)
        with patch("enrichment.asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)):
            result = await _kubectl_json("get", "pod", "p", "-n", "ns", "-o", "json")
        assert result is None
        proc.kill.assert_called_once()

    @pytest.mark.asyncio
    async def test_invalid_json_returns_none(self):
        proc = _make_proc(stdout=b"not-json{{", returncode=0)
        with patch("enrichment.asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)):
            result = await _kubectl_json("get", "pod", "p", "-n", "ns", "-o", "json")
        assert result is None

    @pytest.mark.asyncio
    async def test_subprocess_exception_returns_none(self):
        with patch(
            "enrichment.asyncio.create_subprocess_exec",
            new=AsyncMock(side_effect=OSError("no such binary")),
        ):
            result = await _kubectl_json("get", "pod", "p", "-n", "ns", "-o", "json")
        assert result is None


# ── gather_incident_context — fail-soft ───────────────────────────────────────

class TestGatherFailSoft:
    @pytest.mark.asyncio
    async def test_disabled_returns_empty_snapshot(self, monkeypatch):
        monkeypatch.setattr("enrichment.settings.enrichment_enabled", False)
        exec_mock = AsyncMock()
        with patch("enrichment.asyncio.create_subprocess_exec", new=exec_mock):
            snap = await gather_incident_context({"namespace": "ns", "pod": "p"})
        assert snap.gather_ok is False
        exec_mock.assert_not_called()  # no cluster query when disabled

    @pytest.mark.asyncio
    async def test_missing_labels_returns_empty_snapshot(self, monkeypatch):
        monkeypatch.setattr("enrichment.settings.enrichment_enabled", True)
        exec_mock = AsyncMock()
        with patch("enrichment.asyncio.create_subprocess_exec", new=exec_mock):
            snap = await gather_incident_context({"namespace": "ns"})  # no pod
        assert snap.gather_ok is False
        exec_mock.assert_not_called()

    @pytest.mark.asyncio
    async def test_get_pod_fails_returns_partial_snapshot(self, monkeypatch):
        monkeypatch.setattr("enrichment.settings.enrichment_enabled", True)
        proc = _make_proc(stderr=b"NotFound", returncode=1)
        with patch("enrichment.asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)):
            snap = await gather_incident_context({"namespace": "ns", "pod": "p"})
        assert snap.gather_ok is False
        assert snap.namespace == "ns" and snap.pod == "p"
        assert snap.phase is None and snap.container is None


# ── gather_incident_context — parseo ──────────────────────────────────────────

class TestGatherParsing:
    @pytest.mark.asyncio
    async def test_parses_phase_restart_and_oom_reason(self, monkeypatch):
        monkeypatch.setattr("enrichment.settings.enrichment_enabled", True)
        pod = _pod_json(
            phase="Running",
            containers=[_spec_container("app", cpu="250m", memory="256Mi")],
            statuses=[{
                "name": "app",
                "restartCount": 3,
                "lastState": {"terminated": {"reason": "OOMKilled"}},
                "state": {"running": {}},
            }],
        )
        proc = _make_proc(stdout=json.dumps(pod).encode(), returncode=0)
        with patch("enrichment.asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)):
            snap = await gather_incident_context({"namespace": "ns", "pod": "p"})
        assert snap.gather_ok is True
        assert snap.phase == "Running"
        assert snap.container == "app"
        assert snap.restart_count == 3
        assert snap.last_state_reason == "OOMKilled"

    @pytest.mark.asyncio
    async def test_parses_per_container_limits(self, monkeypatch):
        monkeypatch.setattr("enrichment.settings.enrichment_enabled", True)
        pod = _pod_json(
            containers=[_spec_container("app", cpu="500m", memory="512Mi")],
            statuses=[{"name": "app", "restartCount": 0}],
        )
        proc = _make_proc(stdout=json.dumps(pod).encode(), returncode=0)
        with patch("enrichment.asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)):
            snap = await gather_incident_context({"namespace": "ns", "pod": "p"})
        assert snap.limits["app"] == {"cpu": "500m", "memory": "512Mi"}
        assert snap.current_limit("app", "memory") == "512Mi"
        assert snap.current_limit("app", "cpu") == "500m"
        assert snap.current_limit() == "512Mi"  # defaults to selected container + memory


# ── selección de container ────────────────────────────────────────────────────

class TestResolveWorkload:
    """gather_incident_context resolves the owning workload via ownerReferences (1b)."""

    @pytest.mark.asyncio
    async def test_deployment_via_replicaset_chain(self, monkeypatch):
        monkeypatch.setattr("enrichment.settings.enrichment_enabled", True)
        pod = _pod_owned("ReplicaSet", "web-7d9f8")
        rs = {"metadata": {"ownerReferences": [_owner("Deployment", "web")]}, "spec": {}}
        dep = _selector_json({"app": "web"})
        with patch("enrichment.asyncio.create_subprocess_exec", new=_seq(pod, rs, dep)):
            snap = await gather_incident_context({"namespace": "ns", "pod": "web-7d9f8-abc12"})
        assert snap.workload_kind == "Deployment"
        assert snap.workload_name == "web"
        assert snap.match_labels == {"app": "web"}

    @pytest.mark.asyncio
    async def test_statefulset_direct_owner(self, monkeypatch):
        monkeypatch.setattr("enrichment.settings.enrichment_enabled", True)
        pod = _pod_owned("StatefulSet", "db")
        sts = _selector_json({"app": "db"})
        with patch("enrichment.asyncio.create_subprocess_exec", new=_seq(pod, sts)):
            snap = await gather_incident_context({"namespace": "ns", "pod": "db-0"})
        assert snap.workload_kind == "StatefulSet"
        assert snap.workload_name == "db"
        assert snap.match_labels == {"app": "db"}

    @pytest.mark.asyncio
    async def test_bare_pod_leaves_workload_unresolved(self, monkeypatch):
        monkeypatch.setattr("enrichment.settings.enrichment_enabled", True)
        pod = _pod_json(  # no metadata.ownerReferences
            containers=[_spec_container("app", memory="256Mi")],
            statuses=[{"name": "app", "restartCount": 0}],
        )
        with patch("enrichment.asyncio.create_subprocess_exec", new=_seq(pod)):
            snap = await gather_incident_context({"namespace": "ns", "pod": "p"})
        assert snap.gather_ok is True  # snapshot still usable
        assert snap.workload_kind is None and snap.workload_name is None

    @pytest.mark.asyncio
    async def test_replicaset_get_fails_leaves_unresolved(self, monkeypatch):
        monkeypatch.setattr("enrichment.settings.enrichment_enabled", True)
        pod = _pod_owned("ReplicaSet", "web-7d9f8")
        with patch("enrichment.asyncio.create_subprocess_exec", new=_seq(pod, 1)):  # rs get rc=1
            snap = await gather_incident_context({"namespace": "ns", "pod": "web-7d9f8-abc12"})
        assert snap.gather_ok is True
        assert snap.workload_name is None

    @pytest.mark.asyncio
    async def test_deployment_get_notfound_is_existence_gate(self, monkeypatch):
        monkeypatch.setattr("enrichment.settings.enrichment_enabled", True)
        pod = _pod_owned("ReplicaSet", "web-7d9f8")
        rs = {"metadata": {"ownerReferences": [_owner("Deployment", "web")]}, "spec": {}}
        with patch("enrichment.asyncio.create_subprocess_exec", new=_seq(pod, rs, 1)):  # dep get rc=1
            snap = await gather_incident_context({"namespace": "ns", "pod": "web-7d9f8-abc12"})
        assert snap.workload_name is None  # not confirmed to exist → unresolved
        assert snap.limits["app"] == {"memory": "256Mi"}  # pod snapshot still intact

    @pytest.mark.asyncio
    async def test_bare_replicaset_not_owned_by_deployment(self, monkeypatch):
        monkeypatch.setattr("enrichment.settings.enrichment_enabled", True)
        pod = _pod_owned("ReplicaSet", "orphan-rs")
        rs = {"metadata": {}, "spec": {}}  # RS with no controller owner
        with patch("enrichment.asyncio.create_subprocess_exec", new=_seq(pod, rs)):
            snap = await gather_incident_context({"namespace": "ns", "pod": "orphan-rs-abc12"})
        assert snap.workload_name is None


class TestSelectContainer:
    def test_single_container(self):
        limits = {"app": {"memory": "256Mi"}}
        assert _select_container(None, limits, {}) == "app"

    def test_label_wins_when_valid(self):
        limits = {"app": {}, "sidecar": {}}
        assert _select_container("sidecar", limits, {}) == "sidecar"

    def test_label_ignored_when_not_a_real_container(self):
        limits = {"app": {}, "sidecar": {}}
        # bogus label falls through to first (no failure signal)
        assert _select_container("nope", limits, {}) == "app"

    def test_oom_container_preferred_in_multi(self):
        limits = {"app": {}, "worker": {}}
        states = {"worker": {"restart_count": 5, "reason": "OOMKilled"}}
        assert _select_container(None, limits, states) == "worker"

    def test_empty_returns_none(self):
        assert _select_container(None, {}, {}) is None


class TestControllerOwner:
    def test_prefers_controller_true(self):
        refs = [_owner("Node", "n", controller=False), _owner("ReplicaSet", "rs", controller=True)]
        assert _controller_owner(refs)["name"] == "rs"

    def test_falls_back_to_first_when_no_controller_flag(self):
        refs = [_owner("ReplicaSet", "rs", controller=False)]
        assert _controller_owner(refs)["name"] == "rs"

    def test_none_or_empty(self):
        assert _controller_owner(None) is None
        assert _controller_owner([]) is None


# ── F-17: logs + events (free-text signals for the LLM) ───────────────────────

class TestGatherLogs:
    """_gather_logs: --previous on a crashed container, fallback to current, caps."""

    def _snap(self, **kw) -> IncidentSnapshot:
        return IncidentSnapshot(namespace="ns", pod="p", container="app", **kw)

    @pytest.mark.asyncio
    async def test_current_logs_when_no_crash(self, monkeypatch):
        calls = []
        async def fake_text(*args):
            calls.append(list(args))
            return "line1\nline2\n"
        monkeypatch.setattr("enrichment._kubectl_text", fake_text)
        out = await _gather_logs(self._snap(restart_count=0, last_state_reason=None))
        assert out == "line1\nline2"
        assert "--previous" not in calls[0]
        assert "-c" in calls[0] and "app" in calls[0]

    @pytest.mark.asyncio
    async def test_previous_logs_when_crashed(self, monkeypatch):
        calls = []
        async def fake_text(*args):
            calls.append(list(args))
            return "crash trace"
        monkeypatch.setattr("enrichment._kubectl_text", fake_text)
        out = await _gather_logs(self._snap(last_state_reason="OOMKilled", restart_count=3))
        assert out == "crash trace"
        assert "--previous" in calls[0]

    @pytest.mark.asyncio
    async def test_fallback_to_current_when_previous_absent(self, monkeypatch):
        results = [None, "current logs"]
        calls = []
        async def fake_text(*args):
            calls.append(list(args))
            return results.pop(0)
        monkeypatch.setattr("enrichment._kubectl_text", fake_text)
        out = await _gather_logs(self._snap(last_state_reason="Error", restart_count=1))
        assert out == "current logs"
        assert "--previous" in calls[0] and "--previous" not in calls[1]

    @pytest.mark.asyncio
    async def test_capped_to_max_chars(self, monkeypatch):
        monkeypatch.setattr("enrichment.settings.enrichment_log_max_chars", 10)
        async def fake_text(*args):
            return "x" * 100
        monkeypatch.setattr("enrichment._kubectl_text", fake_text)
        out = await _gather_logs(self._snap(restart_count=0))
        assert len(out) == 10

    @pytest.mark.asyncio
    async def test_none_when_blank_or_failed(self, monkeypatch):
        async def fake_blank(*args):
            return "   \n  "
        monkeypatch.setattr("enrichment._kubectl_text", fake_blank)
        assert await _gather_logs(self._snap(restart_count=0)) is None

        async def fake_none(*args):
            return None
        monkeypatch.setattr("enrichment._kubectl_text", fake_none)
        assert await _gather_logs(self._snap(restart_count=0)) is None


class TestGatherEvents:
    """_gather_events: newest-last render, limit, fail-soft."""

    def _snap(self) -> IncidentSnapshot:
        return IncidentSnapshot(namespace="ns", pod="p", container="app")

    @pytest.mark.asyncio
    async def test_rendered_newest_last(self, monkeypatch):
        events = {"items": [
            {"type": "Normal", "reason": "Scheduled", "message": "assigned to node"},
            {"type": "Warning", "reason": "BackOff", "message": "Back-off restarting"},
        ]}
        async def fake_json(*args):
            return events
        monkeypatch.setattr("enrichment._kubectl_json", fake_json)
        out = await _gather_events(self._snap())
        assert out == ["Normal/Scheduled: assigned to node",
                       "Warning/BackOff: Back-off restarting"]

    @pytest.mark.asyncio
    async def test_limit_keeps_last_n(self, monkeypatch):
        monkeypatch.setattr("enrichment.settings.enrichment_events_limit", 2)
        events = {"items": [{"type": "Normal", "reason": str(i), "message": f"m{i}"}
                            for i in range(5)]}
        async def fake_json(*args):
            return events
        monkeypatch.setattr("enrichment._kubectl_json", fake_json)
        assert await _gather_events(self._snap()) == ["Normal/3: m3", "Normal/4: m4"]

    @pytest.mark.asyncio
    async def test_skips_blank_messages(self, monkeypatch):
        events = {"items": [{"type": "Normal", "reason": "X", "message": "  "},
                            {"type": "Warning", "reason": "Y", "message": "real"}]}
        async def fake_json(*args):
            return events
        monkeypatch.setattr("enrichment._kubectl_json", fake_json)
        assert await _gather_events(self._snap()) == ["Warning/Y: real"]

    @pytest.mark.asyncio
    async def test_failsoft_returns_empty(self, monkeypatch):
        async def fake_json(*args):
            return None
        monkeypatch.setattr("enrichment._kubectl_json", fake_json)
        assert await _gather_events(self._snap()) == []


class TestGatherWiresLogsAndEvents:
    """gather_incident_context populates logs_tail + recent_events end-to-end."""

    @pytest.mark.asyncio
    async def test_snapshot_carries_logs_and_events(self, monkeypatch):
        monkeypatch.setattr("enrichment.settings.enrichment_enabled", True)
        pod = _pod_json(  # bare pod, single container, no crash
            containers=[_spec_container("app", memory="256Mi")],
            statuses=[{"name": "app", "restartCount": 0}],
        )
        events = {"items": [{"type": "Warning", "reason": "BackOff", "message": "restarting"}]}
        pod_proc = _make_proc(stdout=json.dumps(pod).encode(), returncode=0)
        logs_proc = _make_proc(stdout=b"boom\n", returncode=0)
        events_proc = _make_proc(stdout=json.dumps(events).encode(), returncode=0)
        side = AsyncMock(side_effect=[pod_proc, logs_proc, events_proc])
        with patch("enrichment.asyncio.create_subprocess_exec", new=side):
            snap = await gather_incident_context({"namespace": "ns", "pod": "p"})
        assert snap.logs_tail == "boom"
        assert snap.recent_events == ["Warning/BackOff: restarting"]
