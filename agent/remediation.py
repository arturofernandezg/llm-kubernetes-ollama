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


# ── Clasificación de comandos ─────────────────────────────────────────────────

def classify_command(cmd: str) -> CommandSafety:
    """Clasifica un comando kubectl por su nivel de seguridad.

    Evaluación en orden de prioridad:
    1. BLOCKED (destructivos) — siempre primero
    2. SAFE (read-only)
    3. MUTATING (permitidos pero con efecto)
    4. UNKNOWN (fallback)
    """
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
            logger.warning("Cannot parse command: %s — %s", cmd, exc)
            lines.append(f"[SKIP] {cmd} — parse error: {exc}")
            continue

        if not args or args[0] != "kubectl":
            logger.warning("Non-kubectl command skipped in real mode: %s", cmd)
            lines.append(f"[SKIP] {cmd} — only kubectl commands allowed")
            continue

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

        except asyncio.TimeoutError:
            logger.warning(
                "Remediation command timed out after %ds: %s",
                settings.remediation_command_timeout, cmd,
            )
            try:
                proc.kill()
                await proc.communicate()
            except Exception:
                pass
            lines.append(f"[TIMEOUT] {cmd}")

        except Exception as exc:
            logger.warning("Remediation command error: %s — %s", cmd, exc)
            lines.append(f"[ERROR] {cmd} — {exc}")

    return "\n".join(lines)


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
        executed: bool                   — si se ejecutó algo (dry-run)
        execution_log: str               — salida del executor stub
    """
    safe_commands = [
        v["command"] for v in command_validations
        if v["safety"] in (CommandSafety.SAFE, CommandSafety.MUTATING)
    ]
    blocked_commands = [
        v["command"] for v in command_validations
        if v["safety"] == CommandSafety.BLOCKED
    ]
    return {
        "action": action,
        "command_validations": command_validations,
        "safe_commands": safe_commands,
        "blocked_commands": blocked_commands,
        "executed": action == RemediationAction.AUTO_REMEDIATE,
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
    commands: list[str] = diagnosis.get("commands") or []
    validations = validate_commands(commands)
    action = decide_action(diagnosis, validations)

    execution_log = ""
    if action == RemediationAction.AUTO_REMEDIATE:
        safe_cmds = [
            v["command"] for v in validations
            if v["safety"] in (CommandSafety.SAFE, CommandSafety.MUTATING)
        ]
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
