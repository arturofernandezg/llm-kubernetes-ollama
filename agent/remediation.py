"""
Validation layer, motor de decisión y executor para remediación automática.

Flujo: diagnosis dict → validate_commands() → decide_action()
    → capture_pre_patch_value() → execute_commands()
    → build_remediation_result()

Rollback (S5): si el pod sigue fallido tras REMEDIATION_ROLLBACK_TIMEOUT s,
revert_patch() vuelve al valor capturado en PrePatchSnapshot.
"""

import asyncio
import re
import shlex
from dataclasses import dataclass
from enum import Enum

from config import logger, settings


# ── Enums ─────────────────────────────────────────────────────────────────────

class CommandSafety(str, Enum):
    SAFE = "safe"         # Read-only: describe, get, logs, top
    MUTATING = "mutating" # State-changing but permitted: set resources, rollout restart, scale, patch
    BLOCKED = "blocked"   # Destructive: delete ns/pvc/node, drain, rm -rf
    UNKNOWN = "unknown"   # No coincide con ningún patrón conocido


class RemediationAction(str, Enum):
    AUTO_REMEDIATE = "auto_remediate"
    ESCALATE = "escalate"
    SUGGEST_ONLY = "suggest_only"


# ── Structured result types ───────────────────────────────────────────────────

@dataclass(frozen=True)
class ExecuteResult:
    """Structured result for a single command execution."""
    command: str
    success: bool
    stdout: str
    stderr: str
    exit_code: int | None  # None for dry_run / skip / timeout / error
    outcome: str           # 'ok' | 'failed' | 'timeout' | 'error' | 'skip' | 'dry_run'


@dataclass
class PrePatchSnapshot:
    """Values captured from the cluster BEFORE a patch is applied."""
    deployment: str
    namespace: str
    container: str
    field: str
    value: str      # pre-patch resource value (e.g. "512Mi")
    selector: str   # label selector for pod health checks (e.g. "app=engine")


@dataclass
class PodHealthStatus:
    """Health assessment of pods after a remediation patch."""
    healthy: bool
    reason: str
    observed_phases: list[str]
    observed_restarts: list[int]


# ── Patrones de clasificación ─────────────────────────────────────────────────
# BLOCKED se evalúa primero — máxima prioridad.

BLOCKED_PATTERNS = [
    r"kubectl\s+delete\s+(namespace|ns)\b",
    r"kubectl\s+delete\s+pvc?\b",
    r"kubectl\s+delete\s+node",
    r"kubectl\s+delete\s+clusterrole",
    r"kubectl\s+delete\s+--all",
    r"kubectl\s+delete\s+pod\b",   # eliminación directa de pod → siempre BLOCKED
    r"rm\s+-rf",
    r"kubectl.*--force.*--grace-period=0",
    r"kubectl\s+drain\s",
    r"kubectl\s+cordon\s",
    r"kubectl\s+taint\s",
]

SAFE_PATTERNS = [
    r"^kubectl\s+(describe|get|logs|top)\s+",
    r"^kubectl\s+get\s+events\b",
    r"^kubectl\s+version\b",
]

MUTATING_PATTERNS = [
    r"^kubectl\s+set\s+resources\s",
    r"^kubectl\s+rollout\s+restart\s",
    r"^kubectl\s+scale\s",
    r"^kubectl\s+patch\s",
    r"^kubectl\s+label\s",
    r"^kubectl\s+annotate\s",
]

# Orden de riesgo para comparar risk levels
RISK_ORDER: dict[str, int] = {"low": 0, "medium": 1, "high": 2}

# Rule 4.5 exception (tutor-approved 2026-05-23): a controlled resource-limit bump via
# `kubectl set resources` may restart the pod, and that restart is acceptable when the
# remediation is engine-authored and bounded (see is_structured_remediation). Never applies
# to scale/rollout/patch. The model's risk self-rating is superseded by the deterministic
# engine bound; the model's diagnostic confidence is still gated by rule 6.

# Razones legibles por clasificación
_SAFETY_REASONS: dict[CommandSafety, str] = {
    CommandSafety.SAFE: "read-only command",
    CommandSafety.MUTATING: "state-changing command (permitted)",
    CommandSafety.BLOCKED: "destructive command — requires human review",
    CommandSafety.UNKNOWN: "unrecognized command pattern",
}


# ── Condición del tutor: patrones de reinicio de pod ─────────────────────────
# Fail-safe: comandos MUTATING no reconocidos explícitamente como seguros cuentan como restart.

_RESTART_PATTERNS: list[tuple[str, str]] = [
    # Comandos MUTATING que implican reinicio/recreación de pods — condición del tutor.
    # (kubectl delete pod está en BLOCKED_PATTERNS → nunca llega aquí)
    (r"kubectl\s+rollout\s+restart\b", "rollout_restart"),
    (r"kubectl\s+scale\b", "scale_command"),
    (r"kubectl\s+set\s+resources\s+(deployment|statefulset|daemonset)\b", "set_resources_triggers_rollout"),
    (r"kubectl\s+patch\s+(deployment|statefulset|daemonset)\b", "patch_workload_triggers_rollout"),
]

_SAFE_MUTATING_PATTERNS: list[str] = [
    r"^kubectl\s+label\s",
    r"^kubectl\s+annotate\s",
]


def parse_memory_to_bytes(value: str) -> int:
    """Parse Kubernetes memory string to bytes. Raises ValueError on invalid input.

    Supports IEC (Ki/Mi/Gi/Ti), SI (K/M/G/T) and raw integer bytes.
    """
    value = value.strip()
    if not value:
        raise ValueError("empty memory value")

    _IEC = [("ti", 1024 ** 4), ("gi", 1024 ** 3), ("mi", 1024 ** 2), ("ki", 1024)]
    _SI  = [("t", 1000 ** 4), ("g", 1000 ** 3), ("m", 1000 ** 2), ("k", 1000)]

    lower = value.lower()
    for suffix, mult in _IEC:
        if lower.endswith(suffix):
            num_str = lower[: -len(suffix)]
            try:
                return int(float(num_str) * mult)
            except ValueError:
                raise ValueError(f"invalid memory value: {value!r}")
    for suffix, mult in _SI:
        if lower.endswith(suffix):
            num_str = lower[: -len(suffix)]
            try:
                return int(float(num_str) * mult)
            except ValueError:
                raise ValueError(f"invalid memory value: {value!r}")
    try:
        return int(value)
    except ValueError:
        raise ValueError(f"invalid memory value: {value!r}")


def parse_cpu_to_millicores(value: str) -> int:
    """Parse a Kubernetes CPU quantity to millicores. Raises ValueError on invalid input.

    Supports millicores (e.g. "250m") and cores (e.g. "1", "0.5" → 500). A bare number
    is interpreted as cores (Kubernetes semantics): "1" → 1000m, "2" → 2000m.
    """
    value = value.strip()
    if not value:
        raise ValueError("empty cpu value")

    lower = value.lower()
    if lower.endswith("m"):
        num_str = lower[:-1]
        try:
            return int(float(num_str))
        except ValueError:
            raise ValueError(f"invalid cpu value: {value!r}")
    try:
        return int(float(lower) * 1000)
    except ValueError:
        raise ValueError(f"invalid cpu value: {value!r}")


# Maps a proposed_action.field to its parser (value → comparable integer).
_LIMIT_FIELD_PARSERS = {
    "resources.limits.memory": parse_memory_to_bytes,
    "resources.limits.cpu": parse_cpu_to_millicores,
}


def _limit_resource(field: str) -> str:
    """Resource leaf ('cpu'|'memory') from a resources.limits.* field; defaults to 'memory'."""
    leaf = (field or "").rsplit(".", 1)[-1]
    return leaf if leaf in ("cpu", "memory") else "memory"


def build_set_resources_command(
    deployment: str, namespace: str, container: str, field: str, value: str,
) -> str:
    """Synthesize a deterministic `kubectl set resources` command for a limit field.

    Shared by revert_patch (rollback to the captured value) and the structured
    auto-remediation path (apply proposed_action.new_value). Engine-built and bounded —
    never the LLM's free-text command string, so the executed command is always a
    well-formed, single-purpose `set resources` on the target container.
    """
    resource = _limit_resource(field)
    return (
        f"kubectl set resources deployment {deployment} "
        f"-n {namespace} "
        f"--containers={container} "
        f"--limits={resource}={value}"
    )


def implies_pod_restart(command: str) -> tuple[bool, str]:
    """True + reason_code if the command will restart or recreate pods.

    Fail-safe: MUTATING commands not explicitly listed as safe are treated as
    restart-implying until the tutor confirms an exception.
    """
    cmd = command.strip()

    for pattern, reason_code in _RESTART_PATTERNS:
        if re.search(pattern, cmd, re.IGNORECASE):
            return True, reason_code

    for pattern in _SAFE_MUTATING_PATTERNS:
        if re.search(pattern, cmd, re.IGNORECASE):
            return False, ""

    for pattern in SAFE_PATTERNS:
        if re.search(pattern, cmd, re.IGNORECASE):
            return False, ""

    return True, "unknown_mutating_command_fail_safe"


# ── Clasificación de comandos ─────────────────────────────────────────────────

def classify_command(cmd: str) -> CommandSafety:
    """Clasifica un comando kubectl por su nivel de seguridad.

    Evaluación en orden de prioridad:
    1. BLOCKED (destructivos) — siempre primero
    2. SAFE (read-only)
    3. MUTATING (permitidos pero con efecto)
    4. UNKNOWN (fallback)
    """
    if not isinstance(cmd, str):
        return CommandSafety.UNKNOWN
    cmd = cmd.strip()
    if not cmd:
        return CommandSafety.UNKNOWN

    for pattern in BLOCKED_PATTERNS:
        if re.search(pattern, cmd, re.IGNORECASE):
            return CommandSafety.BLOCKED

    for pattern in SAFE_PATTERNS:
        if re.search(pattern, cmd, re.IGNORECASE):
            return CommandSafety.SAFE

    for pattern in MUTATING_PATTERNS:
        if re.search(pattern, cmd, re.IGNORECASE):
            return CommandSafety.MUTATING

    return CommandSafety.UNKNOWN


def validate_commands(commands: list[str]) -> list[dict]:
    """Clasifica una lista de comandos y devuelve su validación.

    Returns:
        list of {"command": str, "safety": CommandSafety, "reason": str}
    """
    results = []
    for cmd in commands:
        if not isinstance(cmd, str):
            logger.warning("Non-string command skipped", extra={"cmd": repr(cmd)})
            continue
        safety = classify_command(cmd)
        results.append({
            "command": cmd,
            "safety": safety,
            "reason": _SAFETY_REASONS[safety],
        })
    return results


def is_structured_remediation(diagnosis: dict) -> bool:
    """True if proposed_action is a bounded, reversible, engine-authorable limit raise.

    When True, the engine synthesizes the `kubectl set resources` command itself
    (build_set_resources_command) and OWNS the danger decision: the model's `risk`
    self-rating is superseded by a deterministic bound (eligible limit field, only-raise,
    capped ≤2× by rule 4.6, reversible via snapshot/rollback). The model's diagnostic
    `confidence` is still honored (rule 6). This is a safety upgrade over executing the
    model's free-text command, not a relaxation — "the model proposes, the engine disposes".

    Eligibility:
      - proposed_action has name/namespace/container (actionable target)
      - namespace is within the tenant allow-list (remediation_auto_namespace_prefix) — no
        auto-remediation outside our own namespaces on a shared cluster (blast-radius guardrail)
      - field is a known limit field (memory always; cpu iff remediation_auto_cpu_enabled —
        gated until cluster-validated)
      - current_value and new_value present and parseable, current > 0
      - new > current (only-raise; never auto-lower a limit)
    The ≤2× cap is NOT checked here — rule 4.6 owns it (with its specific reason/logging).
    """
    proposed_action = diagnosis.get("proposed_action")
    if not isinstance(proposed_action, dict):
        return False
    if not all(proposed_action.get(k) for k in ("name", "namespace", "container")):
        return False
    prefix = settings.remediation_auto_namespace_prefix
    if prefix and not str(proposed_action.get("namespace", "")).startswith(prefix):
        return False  # auto-remediation confined to own-tenant namespaces (shared cluster)
    field = proposed_action.get("field")
    parser = _LIMIT_FIELD_PARSERS.get(field)
    if parser is None:
        return False
    if field == "resources.limits.cpu" and not settings.remediation_auto_cpu_enabled:
        return False  # CPU auto-remediation gated until cluster-validated
    current_val = proposed_action.get("current_value")
    new_val = proposed_action.get("new_value")
    if not current_val or not new_val:
        return False
    try:
        current_amount = parser(current_val)
        new_amount = parser(new_val)
    except ValueError:
        return False
    if current_amount <= 0:
        return False
    return new_amount > current_amount


def seal_proposed_action(diagnosis: dict, snapshot) -> None:
    """Overwrite the LLM's action target with cluster-sourced truth (v2 Eje A / grounding).

    "El cluster informa, el modelo razona, el motor dispone." The 1.5b hallucinates the
    identity (name/container) and current_value of proposed_action; the enrichment snapshot
    carries the real values. The LLM keeps what it does well — field (which resource) and
    direction (new_value, still capped ≤2× by rule 4.6).

    Contract (all in-place on diagnosis["proposed_action"]):
    - No snapshot / gather failed / disabled → leave the LLM values untouched (fail-soft,
      backward-compatible: dry-run and unit tests without a cluster keep working).
    - Gathered but workload unresolved (existence gate: NotFound/bare pod) → drop
      proposed_action → the structured auto path can't fire on a phantom target → escalate.
    - Gathered + workload confirmed → seal name/namespace/container + current_value.
    """
    pa = diagnosis.get("proposed_action")
    if not isinstance(pa, dict):
        return
    if snapshot is None or not getattr(snapshot, "gather_ok", False):
        return  # no cluster truth → trust the LLM (fail-soft)

    if not getattr(snapshot, "workload_name", None):
        diagnosis["proposed_action"] = None  # unconfirmed target → no auto on a phantom
        logger.info("seal_proposed_action: workload unresolved, dropping proposed_action")
        return

    pa["name"] = snapshot.workload_name
    pa["namespace"] = snapshot.namespace
    if getattr(snapshot, "container", None):
        pa["container"] = snapshot.container

    resource = _limit_resource(pa.get("field", ""))
    cluster_current = snapshot.current_limit(snapshot.container, resource)
    if cluster_current:
        pa["current_value"] = cluster_current

    # Real selector for post-patch health checks — replaces the app={name} guess in
    # capture_pre_patch_value. Only set when confirmed (else the fallback applies).
    if getattr(snapshot, "match_labels", None):
        pa["match_labels"] = snapshot.match_labels

    logger.info(
        "seal_proposed_action: sealed from cluster snapshot",
        extra={
            "workload": snapshot.workload_name, "container": snapshot.container,
            "current_value": pa.get("current_value"), "field": pa.get("field"),
        },
    )


# Terminal/waiting reasons that mark a real crash/OOM-class incident (excludes "Completed").
# restart_count is a signal for THIS class; CPU throttling doesn't restart pods (reason=None).
_FAILURE_REASONS = frozenset({
    "OOMKilled", "Error", "ContainerCannotRun", "CrashLoopBackOff", "DeadlineExceeded",
})


def derive_confidence(snapshot) -> float | None:
    """Deterministic incident confidence from the cluster snapshot, or None (no override).

    Grounding (v2 Eje A): the 1.5b's self-rated confidence is overconfident (backlog E5);
    for the crash/OOM class the cluster gives a calibrated signal — a container that
    terminated with a failure reason AND has restarted is a confirmed, recurring incident.

    - No usable snapshot / no crash-class failure reason → None (don't override: keep the
      model's confidence; covers dry-run, healthy pods, and CPU throttling with reason=None).
    - Failure reason + restart_count ≥ remediation_auto_min_restarts → 1.0 (recurring → trust).
    - Failure reason but fewer restarts → 0.5 (isolated event → below the auto gate → suggest).
    """
    if snapshot is None or not getattr(snapshot, "gather_ok", False):
        return None
    reason = getattr(snapshot, "last_state_reason", None)
    if reason not in _FAILURE_REASONS:
        return None
    restarts = getattr(snapshot, "restart_count", None) or 0
    return 1.0 if restarts >= settings.remediation_auto_min_restarts else 0.5


def ground_confidence(diagnosis: dict, snapshot) -> None:
    """Override diagnosis["confidence"] with the cluster-derived signal (in place).

    "El cluster informa, el motor dispone": rule 6 then gates the auto path on a
    deterministic metric, not the model's self-assessment. The model's original value is
    preserved under "model_confidence" for audit. A None derivation leaves things untouched.
    """
    if not isinstance(diagnosis, dict):
        return
    derived = derive_confidence(snapshot)
    if derived is None:
        return
    diagnosis["model_confidence"] = diagnosis.get("confidence")
    diagnosis["confidence"] = derived
    logger.info(
        "ground_confidence: confidence grounded from cluster snapshot",
        extra={
            "model_confidence": diagnosis.get("model_confidence"),
            "grounded_confidence": derived,
            "last_state_reason": getattr(snapshot, "last_state_reason", None),
            "restart_count": getattr(snapshot, "restart_count", None),
        },
    )


def _set_resources_exception(reason_code: str, diagnosis: dict) -> bool:
    """True if a restart-implying command qualifies for the rule 4.5 exception.

    Granted only for `kubectl set resources` (reason_code set_resources_triggers_rollout)
    when the diagnosis is a structured, engine-authorable limit raise
    (is_structured_remediation). Never scale/rollout/patch. The model's risk/confidence are
    no longer gated here: risk is superseded by the engine bound, confidence stays in rule 6.
    CPU eligibility is gated by remediation_auto_cpu_enabled inside is_structured_remediation.
    """
    if reason_code != "set_resources_triggers_rollout":
        return False
    return is_structured_remediation(diagnosis)


# ── Motor de decisión ─────────────────────────────────────────────────────────

def decide_action(
    diagnosis: dict,
    command_validations: list[dict],
    rag_degraded: bool = False,
) -> RemediationAction:
    """Decide qué acción tomar basándose en el diagnóstico y la validación de comandos.

    Args:
        rag_degraded: True si el retrieval RAG falló (ChromaDB inalcanzable) y el LLM
                      corrió zero-shot, sin grounding. En ese caso nunca se auto-remedia.

    Reglas evaluadas en orden (primera que aplica gana):
    1. remediation_enabled == False → SUGGEST_ONLY
    2. Sin comandos → SUGGEST_ONLY
    3. Algún comando BLOCKED → ESCALATE (LLM produjo algo peligroso)
    4. Algún comando UNKNOWN → SUGGEST_ONLY (incertidumbre)
    4.5 (tutor) Algún comando MUTATING implica reinicio de pod → ESCALATE
              (excepción set-resources: memory siempre; cpu solo si remediation_auto_cpu_enabled)
    4.6 (tutor) proposed_action en resources.limits.{memory,cpu}: new > 2× current → ESCALATE
    5. risk > remediation_auto_max_risk → ESCALATE
    6. confidence < remediation_auto_confidence → SUGGEST_ONLY
    7.5 (PR-04) rag_degraded → ESCALATE (no auto-remediar sin grounding RAG)
    7. Todo OK → AUTO_REMEDIATE
    """
    if not settings.remediation_enabled:
        return RemediationAction.SUGGEST_ONLY

    if not command_validations:
        return RemediationAction.SUGGEST_ONLY

    safeties = {v["safety"] for v in command_validations}

    if CommandSafety.BLOCKED in safeties:
        return RemediationAction.ESCALATE

    if CommandSafety.UNKNOWN in safeties:
        return RemediationAction.SUGGEST_ONLY

    # Rule 4.5 — tutor condition: block any command that restarts pods (except authorized set-resources bumps)
    for v in command_validations:
        if v["safety"] == CommandSafety.MUTATING:
            restarts, reason_code = implies_pod_restart(v["command"])
            if restarts:
                if _set_resources_exception(reason_code, diagnosis):
                    pa = diagnosis.get("proposed_action") or {}
                    logger.info(
                        "Rule 4.5 exception: authorized set-resources change",
                        extra={
                            "command": v["command"],
                            "reason_code": reason_code,
                            "resource": _limit_resource(pa.get("field", "")),
                        },
                    )
                    continue
                logger.warning(
                    "Remediation blocked: command implies pod restart",
                    extra={"reason_code": reason_code, "command": v["command"]},
                )
                return RemediationAction.ESCALATE

    # Rule 4.6 — tutor condition: a new resource limit must be ≤ 2× current.
    # Field-aware: applies to both memory (bytes) and cpu (millicores).
    proposed_action = diagnosis.get("proposed_action")
    if isinstance(proposed_action, dict):
        field = proposed_action.get("field")
        parser = _LIMIT_FIELD_PARSERS.get(field)
        if parser is not None:
            resource = field.rsplit(".", 1)[-1]  # "memory" | "cpu"
            current_val = proposed_action.get("current_value")
            new_val = proposed_action.get("new_value")
            if not current_val or not new_val:
                logger.warning(
                    "Remediation blocked: proposed_action missing limit fields",
                    extra={
                        "reason_code": f"missing_{resource}_value",
                        "current_value": current_val,
                        "new_value": new_val,
                    },
                )
                return RemediationAction.ESCALATE
            try:
                current_amount = parser(current_val)
                new_amount = parser(new_val)
                if current_amount == 0:
                    logger.warning(
                        "Remediation blocked: cannot evaluate 2x rule with zero current limit",
                        extra={"reason_code": f"zero_current_{resource}", "current_value": current_val},
                    )
                    return RemediationAction.ESCALATE
                if new_amount > 2 * current_amount:
                    logger.warning(
                        "Remediation blocked: proposed limit exceeds 2x current",
                        extra={
                            "reason_code": f"{resource}_exceeds_2x",
                            "current_value": current_val,
                            "new_value": new_val,
                        },
                    )
                    return RemediationAction.ESCALATE
            except ValueError as exc:
                logger.warning(
                    "Remediation blocked: cannot parse limit values",
                    extra={"reason_code": f"unparseable_{resource}", "error": str(exc)},
                )
                return RemediationAction.ESCALATE

    risk = diagnosis.get("risk", "high")
    max_risk = settings.remediation_auto_max_risk
    if RISK_ORDER.get(risk, 2) > RISK_ORDER.get(max_risk, 0):
        # Rule 5 — escalate on model-rated risk above the auto threshold, EXCEPT for a
        # structured remediation: there the engine's deterministic bound (eligible field,
        # only-raise, ≤2×, reversible) supersedes the model's risk self-rating. Confidence
        # is still gated by rule 6 below.
        if not is_structured_remediation(diagnosis):
            return RemediationAction.ESCALATE
        logger.info(
            "Rule 5 bypassed: structured remediation, engine bound supersedes model risk",
            extra={"risk": risk, "field": (diagnosis.get("proposed_action") or {}).get("field")},
        )

    confidence = diagnosis.get("confidence", 0.0)
    if confidence < settings.remediation_auto_confidence:
        return RemediationAction.SUGGEST_ONLY

    # Rule 7.5 (PR-04) — never auto-remediate without RAG grounding.
    # A degraded RAG (ChromaDB unreachable) means the LLM ran zero-shot, where safety
    # drops sharply (docs/10: 25% vs 100%) and the small model stays overconfident
    # (backlog E5). Downgrade to ESCALATE so a human approves instead of acting blind.
    if rag_degraded:
        logger.warning(
            "Remediation downgraded to escalate: RAG degraded, no grounding",
            extra={"reason_code": "rag_degraded"},
        )
        return RemediationAction.ESCALATE

    return RemediationAction.AUTO_REMEDIATE


# ── Executor ──────────────────────────────────────────────────────────────────

def results_to_log(results: list[ExecuteResult]) -> str:
    """Reconstruct a human-readable log string from a list of ExecuteResult."""
    lines = []
    for r in results:
        if r.outcome == "dry_run":
            lines.append(f"[DRY-RUN] {r.command}")
        elif r.outcome == "ok":
            lines.append(f"[OK] {r.command}\n{r.stdout}")
        elif r.outcome == "failed":
            lines.append(f"[FAILED exit={r.exit_code}] {r.command}\n{r.stderr}")
        elif r.outcome == "timeout":
            lines.append(f"[TIMEOUT] {r.command}")
        elif r.outcome == "error":
            lines.append(f"[ERROR] {r.command} — {r.stderr}")
        elif r.outcome == "skip":
            lines.append(f"[SKIP] {r.command} — {r.stderr}")
        else:
            lines.append(f"[{r.outcome.upper()}] {r.command}")
    return "\n".join(lines)


async def execute_commands(commands: list[str]) -> list[ExecuteResult]:
    """Ejecuta una lista de comandos kubectl y devuelve resultados estructurados.

    Modes:
    - remediation_dry_run=True (default): loguea sin ejecutar, outcome='dry_run'.
    - remediation_dry_run=False: ejecuta via asyncio subprocess con timeout configurado.
      Solo permite comandos que empiecen por 'kubectl'. Exit code != 0 → outcome='failed'.

    Use results_to_log(results) to get a human-readable string for Mattermost/ChromaDB.
    """
    if not commands:
        return []

    if settings.remediation_dry_run:
        results = []
        for cmd in commands:
            logger.info("DRY-RUN remediation command", extra={"command": cmd})
            results.append(ExecuteResult(
                command=cmd, success=True, stdout="", stderr="", exit_code=None, outcome="dry_run",
            ))
        return results

    # Ejecución real
    results = []
    for cmd in commands:
        try:
            args = shlex.split(cmd)
        except ValueError as exc:
            logger.warning("Cannot parse command", extra={"cmd": cmd, "error": str(exc)})
            results.append(ExecuteResult(
                command=cmd, success=False, stdout="", stderr=f"parse error: {exc}",
                exit_code=None, outcome="skip",
            ))
            continue

        if not args or args[0] != "kubectl":
            logger.warning("Non-kubectl command skipped in real mode", extra={"cmd": cmd})
            results.append(ExecuteResult(
                command=cmd, success=False, stdout="", stderr="only kubectl commands allowed",
                exit_code=None, outcome="skip",
            ))
            continue

        proc = None
        try:
            proc = await asyncio.create_subprocess_exec(
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                proc.communicate(),
                timeout=settings.remediation_command_timeout,
            )
            exit_code = proc.returncode

            if exit_code == 0:
                output = stdout_bytes.decode().strip() or "(no output)"
                logger.info(
                    "Remediation command succeeded",
                    extra={"command": cmd, "exit_code": exit_code},
                )
                results.append(ExecuteResult(
                    command=cmd, success=True, stdout=output, stderr="",
                    exit_code=exit_code, outcome="ok",
                ))
            else:
                err = stderr_bytes.decode().strip()
                logger.warning(
                    "Remediation command failed",
                    extra={"command": cmd, "exit_code": exit_code, "stderr": err},
                )
                results.append(ExecuteResult(
                    command=cmd, success=False, stdout="", stderr=err,
                    exit_code=exit_code, outcome="failed",
                ))

        except asyncio.CancelledError:
            if proc is not None:
                try:
                    proc.kill()
                except Exception as kill_exc:
                    logger.debug("Failed to kill process on cancel", extra={"err": str(kill_exc)})
            raise

        except asyncio.TimeoutError:
            logger.warning(
                "Remediation command timed out",
                extra={"cmd": cmd, "timeout": settings.remediation_command_timeout},
            )
            if proc is not None:
                try:
                    proc.kill()
                    await proc.communicate()
                except Exception as kill_exc:
                    logger.debug("Failed to kill timed-out process", extra={"err": str(kill_exc)})
            results.append(ExecuteResult(
                command=cmd, success=False, stdout="", stderr=f"timeout after {settings.remediation_command_timeout}s",
                exit_code=None, outcome="timeout",
            ))

        except Exception as exc:
            logger.warning("Remediation command error", extra={"cmd": cmd, "error": str(exc)})
            results.append(ExecuteResult(
                command=cmd, success=False, stdout="", stderr=str(exc),
                exit_code=None, outcome="error",
            ))

    return results


async def capture_pre_patch_value(proposed_action: dict) -> PrePatchSnapshot | None:
    """Query the cluster for the current resource value BEFORE a patch is applied.

    In dry-run mode, falls back to proposed_action.current_value (LLM-provided).
    Returns None if required fields are missing or the cluster query fails.
    """
    if not isinstance(proposed_action, dict):
        return None

    name = proposed_action.get("name")
    namespace = proposed_action.get("namespace")
    container = proposed_action.get("container")
    field = proposed_action.get("field", "")

    if not all([name, namespace, container]):
        logger.warning("capture_pre_patch_value: missing name/namespace/container")
        return None

    # Prefer the cluster's real selector (sealed from the enrichment snapshot's
    # matchLabels); fall back to the app={name} guess when enrichment is absent
    # (dry-run / legacy) so check_pod_health doesn't miss pods and force a false rollback.
    match_labels = proposed_action.get("match_labels")
    if isinstance(match_labels, dict) and match_labels:
        selector = ",".join(f"{k}={v}" for k, v in match_labels.items())
    else:
        selector = f"app={name}"

    if settings.remediation_dry_run:
        value = proposed_action.get("current_value") or ""
        logger.info(
            "capture_pre_patch_value: dry-run, using LLM current_value",
            extra={"value": value},
        )
        return PrePatchSnapshot(
            deployment=name, namespace=namespace, container=container,
            field=field, value=value, selector=selector,
        )

    # Query all containers' limits for the relevant resource, parse the matching one
    resource = _limit_resource(field)
    try:
        proc = await asyncio.create_subprocess_exec(
            "kubectl", "get", "deployment", name, "-n", namespace,
            "-o", f"jsonpath={{range .spec.template.spec.containers[*]}}{{.name}}:{{.resources.limits.{resource}}};{{end}}",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout_bytes, stderr_bytes = await asyncio.wait_for(
            proc.communicate(), timeout=settings.remediation_command_timeout,
        )
    except Exception as exc:
        logger.warning("capture_pre_patch_value: kubectl get exception", extra={"error": str(exc)})
        return None

    if proc.returncode != 0:
        logger.warning(
            "capture_pre_patch_value: kubectl get failed",
            extra={"deployment": name, "namespace": namespace, "stderr": stderr_bytes.decode().strip()},
        )
        return None

    raw = stdout_bytes.decode().strip()
    captured_value = ""
    for entry in raw.split(";"):
        entry = entry.strip()
        if not entry:
            continue
        parts = entry.split(":", 1)
        if len(parts) == 2 and parts[0] == container:
            captured_value = parts[1]
            break

    if not captured_value:
        logger.warning(
            "capture_pre_patch_value: container not found or empty limit",
            extra={"container": container, "resource": resource, "raw": raw},
        )
        return None

    logger.info(
        "capture_pre_patch_value: captured pre-patch value",
        extra={"deployment": name, "container": container, "value": captured_value},
    )
    return PrePatchSnapshot(
        deployment=name, namespace=namespace, container=container,
        field=field, value=captured_value, selector=selector,
    )


async def check_pod_health(snapshot: PrePatchSnapshot) -> PodHealthStatus:
    """Check if pods matching snapshot.selector are healthy after a remediation patch.

    Healthy = all pods have phase==Running and restartCount==0.
    In dry-run, always returns healthy=True (no real patch was executed).
    """
    if settings.remediation_dry_run:
        return PodHealthStatus(
            healthy=True, reason="dry_run", observed_phases=[], observed_restarts=[],
        )

    try:
        proc = await asyncio.create_subprocess_exec(
            "kubectl", "get", "pods", "-n", snapshot.namespace,
            "-l", snapshot.selector,
            "-o", "jsonpath={range .items[*]}{.status.phase}|{.status.containerStatuses[0].restartCount};{end}",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout_bytes, stderr_bytes = await asyncio.wait_for(
            proc.communicate(), timeout=settings.remediation_command_timeout,
        )
    except Exception as exc:
        logger.warning("check_pod_health: kubectl get pods exception", extra={"error": str(exc)})
        return PodHealthStatus(
            healthy=False, reason=f"kubectl_error: {exc}", observed_phases=[], observed_restarts=[],
        )

    if proc.returncode != 0:
        err = stderr_bytes.decode().strip()
        return PodHealthStatus(
            healthy=False, reason=f"kubectl_failed: {err}", observed_phases=[], observed_restarts=[],
        )

    raw = stdout_bytes.decode().strip()
    if not raw:
        return PodHealthStatus(
            healthy=False, reason="no_pods_found", observed_phases=[], observed_restarts=[],
        )

    phases: list[str] = []
    restarts: list[int] = []
    for entry in raw.split(";"):
        entry = entry.strip()
        if not entry:
            continue
        parts = entry.split("|", 1)
        phase = parts[0] if parts else ""
        try:
            restart_count = int(parts[1]) if len(parts) > 1 else 0
        except ValueError:
            restart_count = 0
        phases.append(phase)
        restarts.append(restart_count)

    if not phases:
        return PodHealthStatus(
            healthy=False, reason="no_pods_parsed", observed_phases=[], observed_restarts=[],
        )

    unhealthy = [p for p in phases if p != "Running"]
    restarting = [r for r in restarts if r > 0]

    if unhealthy:
        return PodHealthStatus(
            healthy=False, reason=f"pods_not_running: {unhealthy}",
            observed_phases=phases, observed_restarts=restarts,
        )
    if restarting:
        return PodHealthStatus(
            healthy=False, reason=f"pods_restarting: {restarting}",
            observed_phases=phases, observed_restarts=restarts,
        )

    return PodHealthStatus(
        healthy=True, reason="all_pods_running_no_restarts",
        observed_phases=phases, observed_restarts=restarts,
    )


async def revert_patch(snapshot: PrePatchSnapshot) -> ExecuteResult:
    """Revert a patch to the pre-patch value captured in snapshot.

    Constructs a kubectl set resources command and delegates to execute_commands().
    Respects dry_run (if True, execute_commands returns a dry_run result).
    Rule 4.5 does NOT apply here: a rollback intentionally restarts pods.
    """
    if not snapshot.value:
        logger.warning("revert_patch: no pre-patch value, cannot revert")
        return ExecuteResult(
            command="<no-op>", success=False, stdout="", stderr="no pre-patch value available",
            exit_code=None, outcome="error",
        )

    cmd = build_set_resources_command(
        snapshot.deployment, snapshot.namespace, snapshot.container,
        snapshot.field, snapshot.value,
    )
    logger.info(
        "revert_patch: executing rollback command",
        extra={
            "deployment": snapshot.deployment,
            "namespace": snapshot.namespace,
            "value": snapshot.value,
        },
    )
    results = await execute_commands([cmd])
    if results:
        return results[0]
    return ExecuteResult(
        command=cmd, success=False, stdout="", stderr="no result from execute_commands",
        exit_code=None, outcome="error",
    )


def _get_safe_commands(validations: list[dict]) -> list[str]:
    """Extract safe/mutating commands from validation results."""
    return [
        v["command"] for v in validations
        if v["safety"] in (CommandSafety.SAFE, CommandSafety.MUTATING)
    ]


# ── Builder de resultado ──────────────────────────────────────────────────────

def build_remediation_result(
    diagnosis: dict,
    action: RemediationAction,
    command_validations: list[dict],
    execute_results: list[ExecuteResult],
) -> dict:
    """Construye el dict de resultado de remediación.

    Keys:
        action: RemediationAction
        command_validations: list[dict]   — clasificación de cada comando
        safe_commands: list[str]          — comandos SAFE o MUTATING
        blocked_commands: list[str]       — comandos BLOCKED
        execution_attempted: bool         — True si el executor fue invocado (incluso en dry-run)
        execution_log: str                — log legible reconstruido de execute_results
        execute_results: list[ExecuteResult] — resultados estructurados por comando
    """
    safe_commands = _get_safe_commands(command_validations)
    blocked_commands = [
        v["command"] for v in command_validations
        if v["safety"] == CommandSafety.BLOCKED
    ]
    return {
        "action": action,
        "command_validations": command_validations,
        "safe_commands": safe_commands,
        "blocked_commands": blocked_commands,
        "execution_attempted": action == RemediationAction.AUTO_REMEDIATE,
        "execution_log": results_to_log(execute_results),
        "execute_results": execute_results,
    }


# ── Entry point principal ─────────────────────────────────────────────────────

async def process_remediation(diagnosis: dict, rag_degraded: bool = False) -> dict:
    """Pipeline completo: validate → decide → snapshot → execute → resultado.

    Args:
        diagnosis: dict con keys diagnosis, commands, confidence, risk, explanation...
                   (output de generate_diagnosis() en diagnosis.py)
        rag_degraded: True si el retrieval RAG falló; fuerza ESCALATE en vez de
                      AUTO_REMEDIATE (PR-04, sin grounding no se auto-actúa).

    Returns:
        build_remediation_result() dict, extended with:
            pre_patch_snapshot: PrePatchSnapshot | None  — captured before AUTO_REMEDIATE
    """
    raw_commands = diagnosis.get("commands") or []
    commands: list[str] = [c for c in raw_commands if isinstance(c, str)]
    if len(commands) != len(raw_commands):
        logger.warning(
            "Non-string commands filtered from diagnosis",
            extra={"total": len(raw_commands), "valid": len(commands)},
        )
    validations = validate_commands(commands)
    action = decide_action(diagnosis, validations, rag_degraded)

    execute_results: list[ExecuteResult] = []
    pre_patch_snapshot: PrePatchSnapshot | None = None

    if action == RemediationAction.AUTO_REMEDIATE:
        proposed_action = diagnosis.get("proposed_action")
        if is_structured_remediation(diagnosis):
            # Engine-authored remediation: synthesize the set-resources command from the
            # structured proposal (deterministic, bounded, reversible) instead of running the
            # model's free-text commands, which are typically investigative (F4: 4/5 emit no
            # executable command). The model proposes the diagnosis; the engine disposes.
            pre_patch_snapshot = await capture_pre_patch_value(proposed_action)
            command = build_set_resources_command(
                proposed_action["name"], proposed_action["namespace"],
                proposed_action["container"], proposed_action["field"],
                proposed_action["new_value"],
            )
            execute_results = await execute_commands([command])
        else:
            safe_cmds = _get_safe_commands(validations)
            if isinstance(proposed_action, dict):
                pre_patch_snapshot = await capture_pre_patch_value(proposed_action)
            execute_results = await execute_commands(safe_cmds)

    result = build_remediation_result(diagnosis, action, validations, execute_results)
    result["pre_patch_snapshot"] = pre_patch_snapshot
    logger.info(
        "Remediation decision",
        extra={
            "action": action.value,
            "risk": diagnosis.get("risk"),
            "confidence": diagnosis.get("confidence"),
            "commands_total": len(commands),
            "blocked": len(result["blocked_commands"]),
            "snapshot_captured": pre_patch_snapshot is not None,
        },
    )
    return result
