"""
Validation layer y motor de decisión para remediación automática.

Flujo: diagnosis dict → validate_commands() → decide_action() → execute_commands() stub
→ build_remediation_result()

La ejecución real de comandos se implementa en S5 (subprocess / k8s client).
Este módulo es pura lógica — 0 dependencias externas nuevas.
"""

import asyncio
import re
import shlex
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


# ── Motor de decisión ─────────────────────────────────────────────────────────

def decide_action(
    diagnosis: dict,
    command_validations: list[dict],
) -> RemediationAction:
    """Decide qué acción tomar basándose en el diagnóstico y la validación de comandos.

    Reglas evaluadas en orden (primera que aplica gana):
    1. remediation_enabled == False → SUGGEST_ONLY
    2. Sin comandos → SUGGEST_ONLY
    3. Algún comando BLOCKED → ESCALATE (LLM produjo algo peligroso)
    4. Algún comando UNKNOWN → SUGGEST_ONLY (incertidumbre)
    4.5 (tutor) Algún comando MUTATING implica reinicio de pod → ESCALATE
    4.6 (tutor) proposed_action en memory.limits: new > 2× current → ESCALATE
    5. risk > remediation_auto_max_risk → ESCALATE
    6. confidence < remediation_auto_confidence → SUGGEST_ONLY
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

    # Rule 4.5 — tutor condition: block any command that restarts pods
    for v in command_validations:
        if v["safety"] == CommandSafety.MUTATING:
            restarts, reason_code = implies_pod_restart(v["command"])
            if restarts:
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

    return RemediationAction.AUTO_REMEDIATE


# ── Executor ──────────────────────────────────────────────────────────────────

async def execute_commands(commands: list[str]) -> str:
    """Ejecuta una lista de comandos kubectl.

    Modes:
    - remediation_dry_run=True (default): loguea sin ejecutar, prefija [DRY-RUN].
    - remediation_dry_run=False: ejecuta via asyncio subprocess con timeout configurado.
      Solo permite comandos que empiecen por 'kubectl'. Exit code != 0 se loguea como warning.

    La interfaz (entrada/salida) es estable entre modos — los callers no cambian.
    """
    if not commands:
        return ""

    if settings.remediation_dry_run:
        lines = []
        for cmd in commands:
            logger.info("DRY-RUN remediation command", extra={"command": cmd})
            lines.append(f"[DRY-RUN] {cmd}")
        return "\n".join(lines)

    # Ejecución real
    lines = []
    for cmd in commands:
        try:
            args = shlex.split(cmd)
        except ValueError as exc:
            logger.warning("Cannot parse command", extra={"cmd": cmd, "error": str(exc)})
            lines.append(f"[SKIP] {cmd} — parse error: {exc}")
            continue

        if not args or args[0] != "kubectl":
            logger.warning("Non-kubectl command skipped in real mode", extra={"cmd": cmd})
            lines.append(f"[SKIP] {cmd} — only kubectl commands allowed")
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
                lines.append(f"[OK] {cmd}\n{output}")
            else:
                err = stderr_bytes.decode().strip()
                logger.warning(
                    "Remediation command failed",
                    extra={"command": cmd, "exit_code": exit_code, "stderr": err},
                )
                lines.append(f"[FAILED exit={exit_code}] {cmd}\n{err}")

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
            lines.append(f"[TIMEOUT] {cmd}")

        except Exception as exc:
            logger.warning("Remediation command error", extra={"cmd": cmd, "error": str(exc)})
            lines.append(f"[ERROR] {cmd} — {exc}")

    return "\n".join(lines)


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
    execution_log: str = "",
) -> dict:
    """Construye el dict de resultado de remediación.

    Keys:
        action: RemediationAction
        command_validations: list[dict]  — clasificación de cada comando
        safe_commands: list[str]         — comandos SAFE o MUTATING
        blocked_commands: list[str]      — comandos BLOCKED
        execution_attempted: bool        — True si el executor fue invocado (incluso en dry-run)
        execution_log: str               — salida del executor stub
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
        "execution_log": execution_log,
    }


# ── Entry point principal ─────────────────────────────────────────────────────

async def process_remediation(diagnosis: dict) -> dict:
    """Pipeline completo: validate → decide → execute (stub) → resultado.

    Args:
        diagnosis: dict con keys diagnosis, commands, confidence, risk, explanation...
                   (output de generate_diagnosis() en diagnosis.py)

    Returns:
        build_remediation_result() dict
    """
    raw_commands = diagnosis.get("commands") or []
    commands: list[str] = [c for c in raw_commands if isinstance(c, str)]
    if len(commands) != len(raw_commands):
        logger.warning(
            "Non-string commands filtered from diagnosis",
            extra={"total": len(raw_commands), "valid": len(commands)},
        )
    validations = validate_commands(commands)
    action = decide_action(diagnosis, validations)

    execution_log = ""
    if action == RemediationAction.AUTO_REMEDIATE:
        safe_cmds = _get_safe_commands(validations)
        execution_log = await execute_commands(safe_cmds)

    result = build_remediation_result(diagnosis, action, validations, execution_log)
    logger.info(
        "Remediation decision",
        extra={
            "action": action.value,
            "risk": diagnosis.get("risk"),
            "confidence": diagnosis.get("confidence"),
            "commands_total": len(commands),
            "blocked": len(result["blocked_commands"]),
        },
    )
    return result
