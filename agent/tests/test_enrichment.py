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
