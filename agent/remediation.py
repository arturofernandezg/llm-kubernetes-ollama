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

# Rule 4.5 exception (tutor-approved 2026-05-23): a controlled memory-limit bump via
# `kubectl set resources` may restart the pod, and that restart is acceptable when the
# diagnosis is high-confidence and bounded-risk. Never applies to scale/rollout/patch.
_SET_RESOURCES_EXCEPTION_MIN_CONFIDENCE = 0.9
_SET_RESOURCES_EXCEPTION_MAX_RISK = "medium"

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


def _set_resources_memory_exception(reason_code: str, diagnosis: dict) -> bool:
    """True if a restart-implying command qualifies for the rule 4.5 exception.

    Only `kubectl set resources` (reason_code set_resources_triggers_rollout) on a
    memory limit, with confidence >= 0.9 and risk <= medium. Never scale/rollout/patch.
    """
    if reason_code != "set_resources_triggers_rollout":
        return False
    proposed_action = diagnosis.get("proposed_action")
    if not (isinstance(proposed_action, dict)
            and proposed_action.get("field") == "resources.limits.memory"):
        return False
    if diagnosis.get("confidence", 0.0) < _SET_RESOURCES_EXCEPTION_MIN_CONFIDENCE:
        return False
    risk = diagnosis.get("risk", "high")
    return RISK_ORDER.get(risk, 2) <= RISK_ORDER.get(_SET_RESOURCES_EXCEPTION_MAX_RISK, 1)


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
    4.6 (tutor) proposed_action en memory.limits: new > 2× current → ESCALATE
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

    # Rule 4.5 — tutor condition: block any command that restarts pods (except authorized memory bumps)
    for v in command_validations:
        if v["safety"] == CommandSafety.MUTATING:
            restarts, reason_code = implies_pod_restart(v["command"])
            if restarts:
                if _set_resources_memory_exception(reason_code, diagnosis):
                    logger.info(
                        "Rule 4.5 exception: authorized set-resources memory change",
                        extra={"command": v["command"], "reason_code": reason_code},
                    )
                    continue
                logger.warning(
                    "Remediation blocked: command implies pod restart",
                    extra={"reason_code": reason_code, "command": v["command"]},
                )
                return RemediationAction.ESCALATE

    # Rule 4.6 — tutor condition: new memory limit must be ≤ 2× current
    proposed_action = diagnosis.get("proposed_action")
    if (
        isinstance(proposed_action, dict)
        and proposed_action.get("field") == "resources.limits.memory"
    ):
        current_val = proposed_action.get("current_value")
        new_val = proposed_action.get("new_value")
        if not current_val or not new_val:
            logger.warning(
                "Remediation blocked: proposed_action missing memory fields",
                extra={
                    "reason_code": "missing_memory_value",
                    "current_value": current_val,
                    "new_value": new_val,
                },
            )
            return RemediationAction.ESCALATE
        try:
            current_bytes = parse_memory_to_bytes(current_val)
            new_bytes = parse_memory_to_bytes(new_val)
            if current_bytes == 0:
                logger.warning(
                    "Remediation blocked: cannot evaluate 2x rule with zero current memory",
                    extra={"reason_code": "zero_current_memory", "current_value": current_val},
                )
                return RemediationAction.ESCALATE
            if new_bytes > 2 * current_bytes:
                logger.warning(
                    "Remediation blocked: proposed memory exceeds 2x current",
                    extra={
                        "reason_code": "memory_exceeds_2x",
                        "current_value": current_val,
                        "new_value": new_val,
                    },
                )
                return RemediationAction.ESCALATE
        except ValueError as exc:
            logger.warning(
                "Remediation blocked: cannot parse memory values",
                extra={"reason_code": "unparseable_memory", "error": str(exc)},
            )
            return RemediationAction.ESCALATE

    risk = diagnosis.get("risk", "high")
    max_risk = settings.remediation_auto_max_risk
    if RISK_ORDER.get(risk, 2) > RISK_ORDER.get(max_risk, 0):
        return RemediationAction.ESCALATE

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

    # Query all containers' memory limits, parse the matching one
    try:
        proc = await asyncio.create_subprocess_exec(
            "kubectl", "get", "deployment", name, "-n", namespace,
            "-o", "jsonpath={range .spec.template.spec.containers[*]}{.name}:{.resources.limits.memory};{end}",
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
            "capture_pre_patch_value: container not found or empty memory limit",
            extra={"container": container, "raw": raw},
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

    cmd = (
        f"kubectl set resources deployment {snapshot.deployment} "
        f"-n {snapshot.namespace} "
        f"--containers={snapshot.container} "
        f"--limits=memory={snapshot.value}"
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
        safe_cmds = _get_safe_commands(validations)
        proposed_action = diagnosis.get("proposed_action")
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
