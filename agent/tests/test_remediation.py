"""
Tests del módulo remediation.py.

Cubre:
- classify_command(): SAFE, MUTATING, BLOCKED, UNKNOWN, edge cases
- validate_commands(): lista mixta, vacía, todos seguros
- decide_action(): todas las reglas de decisión
- execute_commands(): stub dry-run
- process_remediation(): pipeline completo
"""

import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from remediation import (
    CommandSafety,
    RemediationAction,
    classify_command,
    validate_commands,
    decide_action,
    execute_commands,
    process_remediation,
)
from tests.helpers import mock_diagnosis_auto_remediate, mock_diagnosis_escalate, mock_diagnosis_result


# ── TestClassifyCommand ───────────────────────────────────────────────────────

class TestClassifyCommand:

    # SAFE — read-only
    def test_describe_pod(self):
        assert classify_command("kubectl describe pod engine -n prod") == CommandSafety.SAFE

    def test_get_pods(self):
        assert classify_command("kubectl get pods -n prod") == CommandSafety.SAFE

    def test_logs(self):
        assert classify_command("kubectl logs engine-pod -n prod --tail=100") == CommandSafety.SAFE

    def test_top_pods(self):
        assert classify_command("kubectl top pods -n prod") == CommandSafety.SAFE

    def test_get_events(self):
        assert classify_command("kubectl get events -n prod") == CommandSafety.SAFE

    def test_version(self):
        assert classify_command("kubectl version") == CommandSafety.SAFE

    # MUTATING — permitidos
    def test_set_resources(self):
        assert classify_command("kubectl set resources deployment engine --limits=memory=512Mi -n prod") == CommandSafety.MUTATING

    def test_rollout_restart(self):
        assert classify_command("kubectl rollout restart deployment engine -n prod") == CommandSafety.MUTATING

    def test_scale(self):
        assert classify_command("kubectl scale deployment engine --replicas=2 -n prod") == CommandSafety.MUTATING

    def test_patch(self):
        assert classify_command('kubectl patch deployment engine -n prod -p \'{"spec":{"replicas":2}}\'') == CommandSafety.MUTATING

    def test_label(self):
        assert classify_command("kubectl label pod engine-pod env=prod -n prod") == CommandSafety.MUTATING

    def test_annotate(self):
        assert classify_command("kubectl annotate pod engine-pod note=test -n prod") == CommandSafety.MUTATING

    # BLOCKED — destructivos
    def test_delete_namespace(self):
        assert classify_command("kubectl delete namespace arturo-llm-test") == CommandSafety.BLOCKED

    def test_delete_ns_short(self):
        assert classify_command("kubectl delete ns arturo-llm-test") == CommandSafety.BLOCKED

    def test_delete_pvc(self):
        assert classify_command("kubectl delete pvc ollama-pvc") == CommandSafety.BLOCKED

    def test_rm_rf(self):
        assert classify_command("rm -rf /data") == CommandSafety.BLOCKED

    def test_force_grace_period(self):
        assert classify_command("kubectl delete pod engine-pod --force --grace-period=0") == CommandSafety.BLOCKED

    def test_drain(self):
        assert classify_command("kubectl drain node-1 --ignore-daemonsets") == CommandSafety.BLOCKED

    def test_cordon(self):
        assert classify_command("kubectl cordon node-1") == CommandSafety.BLOCKED

    def test_taint(self):
        assert classify_command("kubectl taint nodes node-1 key=value:NoSchedule") == CommandSafety.BLOCKED

    # BLOCKED tiene prioridad sobre SAFE/MUTATING
    def test_blocked_takes_priority_over_safe_pattern(self):
        # drain contiene "kubectl" pero debe ser BLOCKED, no SAFE
        assert classify_command("kubectl drain node-1") == CommandSafety.BLOCKED

    # UNKNOWN / edge cases
    def test_empty_string(self):
        assert classify_command("") == CommandSafety.UNKNOWN

    def test_whitespace_only(self):
        assert classify_command("   ") == CommandSafety.UNKNOWN

    def test_non_kubectl(self):
        assert classify_command("helm upgrade engine ./chart") == CommandSafety.UNKNOWN

    def test_unknown_kubectl_subcommand(self):
        assert classify_command("kubectl exec -it engine-pod -- /bin/sh") == CommandSafety.UNKNOWN

    def test_leading_whitespace_stripped(self):
        # Comando válido con espacios al inicio
        assert classify_command("  kubectl describe pod engine") == CommandSafety.SAFE


# ── TestValidateCommands ──────────────────────────────────────────────────────

class TestValidateCommands:

    def test_empty_list(self):
        result = validate_commands([])
        assert result == []

    def test_all_safe(self):
        cmds = ["kubectl describe pod p -n ns", "kubectl get pods -n ns"]
        result = validate_commands(cmds)
        assert len(result) == 2
        assert all(v["safety"] == CommandSafety.SAFE for v in result)
        assert all("reason" in v for v in result)

    def test_mixed_commands(self):
        cmds = [
            "kubectl describe pod p -n ns",
            "kubectl set resources deployment d --limits=memory=512Mi -n ns",
            "kubectl delete namespace x",
        ]
        result = validate_commands(cmds)
        assert result[0]["safety"] == CommandSafety.SAFE
        assert result[1]["safety"] == CommandSafety.MUTATING
        assert result[2]["safety"] == CommandSafety.BLOCKED

    def test_result_has_required_keys(self):
        result = validate_commands(["kubectl get pods"])
        assert set(result[0].keys()) == {"command", "safety", "reason"}

    def test_command_preserved_in_result(self):
        cmd = "kubectl describe pod engine -n prod"
        result = validate_commands([cmd])
        assert result[0]["command"] == cmd


# ── TestDecideAction ──────────────────────────────────────────────────────────

class TestDecideAction:

    def _validations(self, *commands):
        return validate_commands(list(commands))

    def test_suggest_only_when_disabled(self, monkeypatch):
        monkeypatch.setattr("remediation.settings.remediation_enabled", False)
        diagnosis = mock_diagnosis_auto_remediate()
        validations = self._validations("kubectl describe pod p -n ns")
        assert decide_action(diagnosis, validations) == RemediationAction.SUGGEST_ONLY

    def test_suggest_only_when_no_commands(self, monkeypatch):
        monkeypatch.setattr("remediation.settings.remediation_enabled", True)
        diagnosis = mock_diagnosis_auto_remediate()
        assert decide_action(diagnosis, []) == RemediationAction.SUGGEST_ONLY

    def test_escalate_on_blocked_command(self, monkeypatch):
        monkeypatch.setattr("remediation.settings.remediation_enabled", True)
        diagnosis = mock_diagnosis_auto_remediate()
        validations = self._validations("kubectl drain node-1 --ignore-daemonsets")
        assert decide_action(diagnosis, validations) == RemediationAction.ESCALATE

    def test_suggest_only_on_unknown_command(self, monkeypatch):
        monkeypatch.setattr("remediation.settings.remediation_enabled", True)
        diagnosis = mock_diagnosis_auto_remediate()
        validations = self._validations("helm upgrade engine ./chart")
        assert decide_action(diagnosis, validations) == RemediationAction.SUGGEST_ONLY

    def test_escalate_on_high_risk(self, monkeypatch):
        monkeypatch.setattr("remediation.settings.remediation_enabled", True)
        monkeypatch.setattr("remediation.settings.remediation_auto_max_risk", "low")
        diagnosis = {**mock_diagnosis_auto_remediate(), "risk": "high"}
        validations = self._validations("kubectl describe pod p -n ns")
        assert decide_action(diagnosis, validations) == RemediationAction.ESCALATE

    def test_escalate_on_medium_risk_when_max_is_low(self, monkeypatch):
        monkeypatch.setattr("remediation.settings.remediation_enabled", True)
        monkeypatch.setattr("remediation.settings.remediation_auto_max_risk", "low")
        diagnosis = {**mock_diagnosis_auto_remediate(), "risk": "medium"}
        validations = self._validations("kubectl describe pod p -n ns")
        assert decide_action(diagnosis, validations) == RemediationAction.ESCALATE

    def test_suggest_only_on_low_confidence(self, monkeypatch):
        monkeypatch.setattr("remediation.settings.remediation_enabled", True)
        monkeypatch.setattr("remediation.settings.remediation_auto_confidence", 0.8)
        diagnosis = {**mock_diagnosis_auto_remediate(), "confidence": 0.5}
        validations = self._validations("kubectl describe pod p -n ns")
        assert decide_action(diagnosis, validations) == RemediationAction.SUGGEST_ONLY

    def test_auto_remediate_all_conditions_met(self, monkeypatch):
        monkeypatch.setattr("remediation.settings.remediation_enabled", True)
        monkeypatch.setattr("remediation.settings.remediation_auto_max_risk", "low")
        monkeypatch.setattr("remediation.settings.remediation_auto_confidence", 0.8)
        diagnosis = mock_diagnosis_auto_remediate()  # risk=low, confidence=0.9, safe+mutating cmds
        validations = self._validations(
            "kubectl describe pod engine-pod -n prod",
            "kubectl set resources deployment engine --limits=memory=512Mi -n prod",
        )
        assert decide_action(diagnosis, validations) == RemediationAction.AUTO_REMEDIATE

    def test_medium_risk_ok_when_max_is_medium(self, monkeypatch):
        monkeypatch.setattr("remediation.settings.remediation_enabled", True)
        monkeypatch.setattr("remediation.settings.remediation_auto_max_risk", "medium")
        monkeypatch.setattr("remediation.settings.remediation_auto_confidence", 0.8)
        diagnosis = {**mock_diagnosis_auto_remediate(), "risk": "medium"}
        validations = self._validations("kubectl describe pod p -n ns")
        assert decide_action(diagnosis, validations) == RemediationAction.AUTO_REMEDIATE


# ── TestExecuteCommands ───────────────────────────────────────────────────────

class TestExecuteCommands:

    @pytest.mark.asyncio
    async def test_dry_run_prefix(self):
        cmds = ["kubectl describe pod engine -n prod"]
        result = await execute_commands(cmds)
        assert "[DRY-RUN]" in result
        assert "kubectl describe pod engine -n prod" in result

    @pytest.mark.asyncio
    async def test_multiple_commands(self):
        cmds = ["kubectl get pods", "kubectl describe pod p"]
        result = await execute_commands(cmds)
        lines = result.strip().split("\n")
        assert len(lines) == 2
        assert all("[DRY-RUN]" in line for line in lines)

    @pytest.mark.asyncio
    async def test_empty_list_returns_empty_string(self):
        result = await execute_commands([])
        assert result == ""


# ── TestExecuteCommandsRealMode ───────────────────────────────────────────────

def _make_proc(stdout: bytes = b"", stderr: bytes = b"", returncode: int = 0):
    """Crea un mock de asyncio subprocess."""
    proc = MagicMock()
    proc.returncode = returncode
    proc.communicate = AsyncMock(return_value=(stdout, stderr))
    proc.kill = MagicMock()
    return proc


class TestExecuteCommandsRealMode:
    """Tests para execute_commands() con remediation_dry_run=False."""

    @pytest.mark.asyncio
    async def test_dry_run_true_returns_dry_run_prefix(self, monkeypatch):
        """Regression: dry_run=True (default) sigue devolviendo [DRY-RUN]."""
        monkeypatch.setattr("remediation.settings.remediation_dry_run", True)
        result = await execute_commands(["kubectl get pods"])
        assert "[DRY-RUN]" in result

    @pytest.mark.asyncio
    async def test_real_execution_success(self, monkeypatch):
        monkeypatch.setattr("remediation.settings.remediation_dry_run", False)
        monkeypatch.setattr("remediation.settings.remediation_command_timeout", 30)
        proc = _make_proc(stdout=b"pod/engine Running", returncode=0)
        with patch("remediation.asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)):
            result = await execute_commands(["kubectl get pods -n prod"])
        assert "[OK]" in result
        assert "pod/engine Running" in result

    @pytest.mark.asyncio
    async def test_real_execution_nonzero_exit_code(self, monkeypatch):
        monkeypatch.setattr("remediation.settings.remediation_dry_run", False)
        monkeypatch.setattr("remediation.settings.remediation_command_timeout", 30)
        proc = _make_proc(stderr=b"not found", returncode=1)
        with patch("remediation.asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)):
            result = await execute_commands(["kubectl get pods -n prod"])
        assert "[FAILED exit=1]" in result
        assert "not found" in result

    @pytest.mark.asyncio
    async def test_real_execution_timeout(self, monkeypatch):
        monkeypatch.setattr("remediation.settings.remediation_dry_run", False)
        monkeypatch.setattr("remediation.settings.remediation_command_timeout", 1)
        proc = _make_proc()
        proc.communicate = AsyncMock(side_effect=asyncio.TimeoutError)
        with patch("remediation.asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)):
            result = await execute_commands(["kubectl get pods -n prod"])
        assert "[TIMEOUT]" in result

    @pytest.mark.asyncio
    async def test_real_execution_skips_non_kubectl(self, monkeypatch):
        monkeypatch.setattr("remediation.settings.remediation_dry_run", False)
        with patch("remediation.asyncio.create_subprocess_exec", new=AsyncMock()) as mock_exec:
            result = await execute_commands(["helm upgrade engine ./chart"])
        mock_exec.assert_not_called()
        assert "[SKIP]" in result
        assert "only kubectl commands allowed" in result

    @pytest.mark.asyncio
    async def test_real_execution_empty_list(self, monkeypatch):
        monkeypatch.setattr("remediation.settings.remediation_dry_run", False)
        result = await execute_commands([])
        assert result == ""

    @pytest.mark.asyncio
    async def test_real_execution_multiple_commands(self, monkeypatch):
        """Múltiples comandos: el primero OK, el segundo falla."""
        monkeypatch.setattr("remediation.settings.remediation_dry_run", False)
        monkeypatch.setattr("remediation.settings.remediation_command_timeout", 30)
        proc_ok = _make_proc(stdout=b"running", returncode=0)
        proc_fail = _make_proc(stderr=b"error", returncode=1)
        with patch(
            "remediation.asyncio.create_subprocess_exec",
            new=AsyncMock(side_effect=[proc_ok, proc_fail]),
        ):
            result = await execute_commands([
                "kubectl get pods -n prod",
                "kubectl describe pod bad-pod -n prod",
            ])
        assert "[OK]" in result
        assert "[FAILED exit=1]" in result


# ── TestProcessRemediation ────────────────────────────────────────────────────

class TestProcessRemediation:

    @pytest.mark.asyncio
    async def test_suggest_only_when_disabled(self, monkeypatch):
        monkeypatch.setattr("remediation.settings.remediation_enabled", False)
        result = await process_remediation(mock_diagnosis_auto_remediate())
        assert result["action"] == RemediationAction.SUGGEST_ONLY
        assert result["executed"] is False

    @pytest.mark.asyncio
    async def test_escalate_with_blocked_command(self, monkeypatch):
        monkeypatch.setattr("remediation.settings.remediation_enabled", True)
        result = await process_remediation(mock_diagnosis_escalate())
        assert result["action"] == RemediationAction.ESCALATE
        assert result["executed"] is False
        assert len(result["blocked_commands"]) > 0

    @pytest.mark.asyncio
    async def test_auto_remediate_result_structure(self, monkeypatch):
        monkeypatch.setattr("remediation.settings.remediation_enabled", True)
        monkeypatch.setattr("remediation.settings.remediation_auto_max_risk", "low")
        monkeypatch.setattr("remediation.settings.remediation_auto_confidence", 0.8)
        result = await process_remediation(mock_diagnosis_auto_remediate())
        assert result["action"] == RemediationAction.AUTO_REMEDIATE
        assert result["executed"] is True
        assert "[DRY-RUN]" in result["execution_log"]
        assert "command_validations" in result
        assert "safe_commands" in result
        assert "blocked_commands" in result

    @pytest.mark.asyncio
    async def test_no_commands_gives_suggest_only(self, monkeypatch):
        monkeypatch.setattr("remediation.settings.remediation_enabled", True)
        diagnosis = {**mock_diagnosis_result(), "commands": []}
        result = await process_remediation(diagnosis)
        assert result["action"] == RemediationAction.SUGGEST_ONLY
        assert result["executed"] is False
