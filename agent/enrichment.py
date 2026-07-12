"""
Deterministic Kubernetes context gathering — the grounding stage (v2 Eje A / P0·1).

"El cluster informa, el modelo razona, el motor dispone." Before the LLM diagnoses,
query the K8s API for the REAL state of the alerting pod (current limits, phase,
restarts, last-state reason) so downstream `current_value` and target identity come
from the cluster, not from the model. The 1.5b model hallucinates these facts; the
cluster does not.

Fail-soft by contract: any query failure degrades to a partial/empty snapshot and the
pipeline continues in LLM-only mode. This stage NEVER blocks ingestion.

Scope (1a): pod snapshot (limits/phase/restart/last-state + deterministic container
selection). Workload identity via ownerReferences (target + selector) lands in 1b.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field

from config import settings, logger


@dataclass
class IncidentSnapshot:
    """Cluster-sourced snapshot of the alerting pod. Source of truth for current_value/target."""

    namespace: str
    pod: str
    container: str | None = None
    # Per-container current limits: {container_name: {"cpu": "250m", "memory": "256Mi"}}
    limits: dict[str, dict[str, str]] = field(default_factory=dict)
    phase: str | None = None
    restart_count: int | None = None
    last_state_reason: str | None = None  # e.g. OOMKilled, CrashLoopBackOff — deterministic signal
    # Owning workload resolved from the cluster (ownerReferences), NOT the LLM. workload_name
    # being set means the controller was confirmed to exist (existence gate). match_labels is
    # the controller's real .spec.selector (feeds check_pod_health, replacing the app={name} guess).
    workload_kind: str | None = None    # Deployment | StatefulSet | DaemonSet
    workload_name: str | None = None
    match_labels: dict[str, str] = field(default_factory=dict)
    # F-17: free-text signals for the LLM to reason over (language only, never executed).
    logs_tail: str | None = None            # last N log lines (--previous if the container died)
    recent_events: list[str] = field(default_factory=list)  # "Type/Reason: message" per event
    gather_ok: bool = False

    def current_limit(self, container: str | None = None, resource: str = "memory") -> str | None:
        """Return the cluster-observed limit for a container's resource, or None if absent."""
        c = container or self.container
        if not c:
            return None
        return (self.limits.get(c) or {}).get(resource)


async def _kubectl_json(*args: str) -> dict | None:
    """Run `kubectl <args>` and parse stdout as JSON. Fail-soft — returns None, never raises.

    Same safe invocation as remediation.execute_commands (argv, no shell). Short timeout:
    enrichment is best-effort context, not on the critical path of correctness.
    """
    proc = None
    try:
        proc = await asyncio.create_subprocess_exec(
            "kubectl", *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout_bytes, stderr_bytes = await asyncio.wait_for(
            proc.communicate(), timeout=settings.enrichment_timeout,
        )
    except asyncio.CancelledError:
        if proc is not None:
            try:
                proc.kill()
            except Exception:
                pass
        raise
    except Exception as exc:
        # Reap the child: wait_for cancels communicate() but a timed-out kubectl would
        # keep running — against a hung API server that leaks a process per query.
        if proc is not None:
            try:
                proc.kill()
                await proc.communicate()
            except Exception:
                pass
        logger.warning("enrichment: kubectl exception", extra={"cmd_args": list(args), "error": str(exc)})
        return None

    if proc.returncode != 0:
        logger.info(
            "enrichment: kubectl non-zero",
            extra={"cmd_args": list(args), "stderr": stderr_bytes.decode(errors="replace").strip()[:200]},
        )
        return None

    try:
        return json.loads(stdout_bytes.decode())
    except (ValueError, UnicodeDecodeError) as exc:
        logger.warning("enrichment: kubectl output not JSON", extra={"cmd_args": list(args), "error": str(exc)})
        return None


async def _kubectl_text(*args: str) -> str | None:
    """Run `kubectl <args>` and return stdout as text. Fail-soft — returns None, never raises.

    Sibling of _kubectl_json for commands whose output is not JSON (logs). Same safe
    argv invocation, short timeout, and child reaping on failure/timeout.
    """
    proc = None
    try:
        proc = await asyncio.create_subprocess_exec(
            "kubectl", *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout_bytes, stderr_bytes = await asyncio.wait_for(
            proc.communicate(), timeout=settings.enrichment_timeout,
        )
    except asyncio.CancelledError:
        if proc is not None:
            try:
                proc.kill()
            except Exception:
                pass
        raise
    except Exception as exc:
        if proc is not None:
            try:
                proc.kill()
                await proc.communicate()
            except Exception:
                pass
        logger.warning("enrichment: kubectl text exception", extra={"cmd_args": list(args), "error": str(exc)})
        return None

    if proc.returncode != 0:
        logger.info(
            "enrichment: kubectl text non-zero",
            extra={"cmd_args": list(args), "stderr": stderr_bytes.decode(errors="replace").strip()[:200]},
        )
        return None

    return stdout_bytes.decode(errors="replace")


async def _gather_logs(snapshot: IncidentSnapshot) -> str | None:
    """Best-effort `kubectl logs --tail`, capped in lines and chars. Uses --previous when
    the container last terminated (OOM/crash) — that is where the useful trace lives, the
    fresh container often logs nothing. Fail-soft: returns None, never raises."""
    args = [
        "logs", snapshot.pod, "-n", snapshot.namespace,
        f"--tail={settings.enrichment_log_tail_lines}",
    ]
    if snapshot.container:
        args += ["-c", snapshot.container]
    # A recorded termination reason (or a restart) means the current container is a fresh
    # replacement — the crash trace is in the previous instance.
    if snapshot.last_state_reason or (snapshot.restart_count or 0) > 0:
        args.append("--previous")

    text = await _kubectl_text(*args)
    if text is None and "--previous" in args:
        # No previous container (e.g. first crash not yet restarted) → fall back to current.
        args.remove("--previous")
        text = await _kubectl_text(*args)
    if not text or not text.strip():
        return None
    return text.strip()[-settings.enrichment_log_max_chars:]


async def _gather_events(snapshot: IncidentSnapshot) -> list[str]:
    """Best-effort recent Kubernetes events for the pod, newest last, as "Type/Reason: message"
    strings. Fail-soft: returns [] on any failure."""
    events_json = await _kubectl_json(
        "get", "events", "-n", snapshot.namespace,
        "--field-selector", f"involvedObject.name={snapshot.pod}",
        "--sort-by=.lastTimestamp", "-o", "json",
    )
    if not events_json:
        return []
    items = events_json.get("items", []) or []
    rendered: list[str] = []
    for ev in items[-settings.enrichment_events_limit:]:
        reason = ev.get("reason", "")
        etype = ev.get("type", "")
        message = (ev.get("message", "") or "").strip()
        if not message:
            continue
        rendered.append(f"{etype}/{reason}: {message}" if (etype or reason) else message)
    return rendered


def _container_states(pod_json: dict) -> dict[str, dict]:
    """Map container name → {restart_count, reason} from .status.containerStatuses."""
    states: dict[str, dict] = {}
    for cs in pod_json.get("status", {}).get("containerStatuses", []) or []:
        name = cs.get("name")
        if not name:
            continue
        # Prefer a terminated reason (why it last died); fall back to a waiting reason.
        reason = (
            (cs.get("lastState", {}).get("terminated", {}) or {}).get("reason")
            or (cs.get("state", {}).get("terminated", {}) or {}).get("reason")
            or (cs.get("state", {}).get("waiting", {}) or {}).get("reason")
        )
        states[name] = {"restart_count": cs.get("restartCount", 0), "reason": reason}
    return states


def _container_limits(pod_json: dict) -> dict[str, dict[str, str]]:
    """Map container name → {cpu, memory} from .spec.containers[*].resources.limits."""
    limits: dict[str, dict[str, str]] = {}
    for c in pod_json.get("spec", {}).get("containers", []) or []:
        name = c.get("name")
        if not name:
            continue
        lim = (c.get("resources", {}) or {}).get("limits", {}) or {}
        limits[name] = {k: str(v) for k, v in lim.items() if k in ("cpu", "memory")}
    return limits


def _select_container(
    label_container: str | None, limits: dict[str, dict[str, str]], states: dict[str, dict],
) -> str | None:
    """Pick the target container deterministically — never from the LLM.

    Priority: alert label (if a real container) → the one that OOMKilled/is waiting →
    the single container → the first container.
    """
    spec_names = list(limits.keys())
    if label_container and label_container in spec_names:
        return label_container

    # A container reporting a failure reason (OOMKilled preferred) is the likely culprit.
    culprit = None
    for name in spec_names:
        reason = states.get(name, {}).get("reason")
        if reason == "OOMKilled":
            return name
        if reason and culprit is None:
            culprit = name
    if culprit:
        return culprit

    return spec_names[0] if spec_names else None


_SUPPORTED_WORKLOADS = ("Deployment", "StatefulSet", "DaemonSet")


def _controller_owner(owner_refs: list[dict] | None) -> dict | None:
    """Return the controlling ownerReference (controller==True), or the first, or None."""
    refs = owner_refs or []
    for ref in refs:
        if ref.get("controller"):
            return ref
    return refs[0] if refs else None


async def _resolve_workload(snapshot: IncidentSnapshot, pod_json: dict) -> None:
    """Resolve the owning workload (kind/name/match_labels) from ownerReferences. Fail-soft.

    Pod → ReplicaSet → Deployment (the RS owner is canonical; avoids strip-hash heuristics),
    or Pod → StatefulSet/DaemonSet directly. The final `get <kind> <name>` doubles as the
    existence gate: workload_name is only set when the controller is confirmed to exist.
    """
    ns = snapshot.namespace
    owner = _controller_owner(pod_json.get("metadata", {}).get("ownerReferences"))
    if not owner:
        return  # bare pod — no controller to patch

    kind, name = owner.get("kind"), owner.get("name")
    if kind == "ReplicaSet":
        rs = await _kubectl_json("get", "replicaset", name, "-n", ns, "-o", "json")
        if not rs:
            return
        dep = _controller_owner(rs.get("metadata", {}).get("ownerReferences"))
        if not dep or dep.get("kind") != "Deployment":
            return  # bare ReplicaSet — set resources on a RS would be undone by no controller
        kind, name = "Deployment", dep.get("name")

    if kind not in _SUPPORTED_WORKLOADS or not name:
        return

    controller = await _kubectl_json("get", kind.lower(), name, "-n", ns, "-o", "json")
    if not controller:
        return  # existence gate: NotFound → leave unresolved → downstream escalates

    snapshot.workload_kind = kind
    snapshot.workload_name = name
    snapshot.match_labels = controller.get("spec", {}).get("selector", {}).get("matchLabels", {}) or {}


async def gather_incident_context(labels: dict[str, str]) -> IncidentSnapshot:
    """Gather a cluster-sourced snapshot for the alerting pod. Fail-soft; never raises.

    A failed/disabled gather returns a snapshot with gather_ok=False, and downstream code
    falls back to LLM-only diagnosis.
    """
    namespace = (labels or {}).get("namespace", "")
    pod = (labels or {}).get("pod", "")
    snapshot = IncidentSnapshot(namespace=namespace, pod=pod)

    if not settings.enrichment_enabled:
        logger.info("enrichment: disabled, skipping gather")
        return snapshot
    if not namespace or not pod:
        logger.info("enrichment: missing namespace/pod label, skipping gather")
        return snapshot

    pod_json = await _kubectl_json("get", "pod", pod, "-n", namespace, "-o", "json")
    if not pod_json:
        return snapshot  # fail-soft: partial snapshot, LLM-only downstream

    snapshot.gather_ok = True
    snapshot.phase = pod_json.get("status", {}).get("phase")

    states = _container_states(pod_json)
    snapshot.limits = _container_limits(pod_json)
    snapshot.container = _select_container(labels.get("container"), snapshot.limits, states)

    if snapshot.container and snapshot.container in states:
        snapshot.restart_count = states[snapshot.container].get("restart_count")
        snapshot.last_state_reason = states[snapshot.container].get("reason")

    await _resolve_workload(snapshot, pod_json)

    # F-17: free-text signals (logs + events) for the LLM. Best-effort — each is fail-soft
    # on its own; a failure leaves the field empty and never blocks the gather.
    snapshot.logs_tail = await _gather_logs(snapshot)
    snapshot.recent_events = await _gather_events(snapshot)

    logger.info(
        "enrichment: snapshot gathered",
        extra={
            "namespace": namespace, "pod": pod, "container": snapshot.container,
            "phase": snapshot.phase, "restart_count": snapshot.restart_count,
            "last_state_reason": snapshot.last_state_reason,
            "workload_kind": snapshot.workload_kind, "workload_name": snapshot.workload_name,
            "has_logs": snapshot.logs_tail is not None, "events_count": len(snapshot.recent_events),
        },
    )
    return snapshot
