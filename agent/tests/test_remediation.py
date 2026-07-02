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
    ExecuteResult,
    PrePatchSnapshot,
    PodHealthStatus,
    classify_command,
    validate_commands,
    decide_action,
    execute_commands,
    results_to_log,
    capture_pre_patch_value,
    check_pod_health,
    revert_patch,
    process_remediation,
    parse_memory_to_bytes,
    parse_cpu_to_millicores,
    implies_pod_restart,
    is_structured_remediation,
    build_set_resources_command,
    seal_proposed_action,
    derive_confidence,
    ground_confidence,
    _limit_resource,
    _double_limit_value,
)
from enrichment import IncidentSnapshot
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
        # non-restart commands only (annotate is safe mutating, no rollout triggered)
        diagnosis = mock_diagnosis_auto_remediate()  # risk=low, confidence=0.9
        validations = self._validations(
            "kubectl describe pod engine-pod -n prod",
            "kubectl annotate deployment engine aiops-checked=true -n prod",
        )
        assert decide_action(diagnosis, validations) == RemediationAction.AUTO_REMEDIATE

    def test_medium_risk_ok_when_max_is_medium(self, monkeypatch):
        monkeypatch.setattr("remediation.settings.remediation_enabled", True)
        monkeypatch.setattr("remediation.settings.remediation_auto_max_risk", "medium")
        monkeypatch.setattr("remediation.settings.remediation_auto_confidence", 0.8)
        diagnosis = {**mock_diagnosis_auto_remediate(), "risk": "medium"}
        validations = self._validations("kubectl describe pod p -n ns")
        assert decide_action(diagnosis, validations) == RemediationAction.AUTO_REMEDIATE

    # ── PR-04: rag_degraded nunca auto-remedia (sin grounding RAG) ──────────────

    def test_rag_degraded_downgrades_auto_remediate_to_escalate(self, monkeypatch):
        """Mismo caso que test_auto_remediate_all_conditions_met, pero rag_degraded=True → ESCALATE."""
        monkeypatch.setattr("remediation.settings.remediation_enabled", True)
        monkeypatch.setattr("remediation.settings.remediation_auto_max_risk", "low")
        monkeypatch.setattr("remediation.settings.remediation_auto_confidence", 0.8)
        diagnosis = mock_diagnosis_auto_remediate()
        validations = self._validations(
            "kubectl describe pod engine-pod -n prod",
            "kubectl annotate deployment engine aiops-checked=true -n prod",
        )
        assert decide_action(diagnosis, validations, rag_degraded=True) == RemediationAction.ESCALATE
        # Sin degradación (default) sigue auto-remediando
        assert decide_action(diagnosis, validations) == RemediationAction.AUTO_REMEDIATE

    def test_rag_degraded_leaves_suggest_only_untouched(self, monkeypatch):
        """El guard solo intercepta AUTO_REMEDIATE; baja confianza sigue SUGGEST_ONLY (no escala)."""
        monkeypatch.setattr("remediation.settings.remediation_enabled", True)
        monkeypatch.setattr("remediation.settings.remediation_auto_confidence", 0.8)
        diagnosis = {**mock_diagnosis_auto_remediate(), "confidence": 0.5}
        validations = self._validations("kubectl describe pod p -n ns")
        assert decide_action(diagnosis, validations, rag_degraded=True) == RemediationAction.SUGGEST_ONLY


# ── TestExecuteResult ─────────────────────────────────────────────────────────

class TestExecuteResult:

    def test_equality(self):
        r1 = ExecuteResult(command="kubectl get pods", success=True, stdout="ok", stderr="", exit_code=0, outcome="ok")
        r2 = ExecuteResult(command="kubectl get pods", success=True, stdout="ok", stderr="", exit_code=0, outcome="ok")
        assert r1 == r2

    def test_frozen_immutable(self):
        r = ExecuteResult(command="kubectl get pods", success=True, stdout="", stderr="", exit_code=None, outcome="dry_run")
        with pytest.raises(Exception):
            r.success = False  # type: ignore[misc]

    def test_results_to_log_dry_run(self):
        results = [ExecuteResult(command="kubectl get pods", success=True, stdout="", stderr="", exit_code=None, outcome="dry_run")]
        log = results_to_log(results)
        assert "[DRY-RUN]" in log
        assert "kubectl get pods" in log

    def test_results_to_log_ok(self):
        results = [ExecuteResult(command="kubectl get pods", success=True, stdout="pod Running", stderr="", exit_code=0, outcome="ok")]
        log = results_to_log(results)
        assert "[OK]" in log
        assert "pod Running" in log

    def test_results_to_log_failed(self):
        results = [ExecuteResult(command="kubectl get pods", success=False, stdout="", stderr="not found", exit_code=1, outcome="failed")]
        log = results_to_log(results)
        assert "[FAILED exit=1]" in log
        assert "not found" in log

    def test_results_to_log_skip(self):
        results = [ExecuteResult(command="helm up", success=False, stdout="", stderr="only kubectl commands allowed", exit_code=None, outcome="skip")]
        log = results_to_log(results)
        assert "[SKIP]" in log
        assert "only kubectl commands allowed" in log

    def test_results_to_log_empty(self):
        assert results_to_log([]) == ""


# ── TestExecuteCommands ───────────────────────────────────────────────────────

class TestExecuteCommands:

    @pytest.mark.asyncio
    async def test_dry_run_returns_list_with_dry_run_outcome(self):
        cmds = ["kubectl describe pod engine -n prod"]
        result = await execute_commands(cmds)
        assert len(result) == 1
        assert result[0].outcome == "dry_run"
        assert result[0].success is True
        assert result[0].command == "kubectl describe pod engine -n prod"

    @pytest.mark.asyncio
    async def test_multiple_commands_all_dry_run(self):
        cmds = ["kubectl get pods", "kubectl describe pod p"]
        result = await execute_commands(cmds)
        assert len(result) == 2
        assert all(r.outcome == "dry_run" for r in result)

    @pytest.mark.asyncio
    async def test_empty_list_returns_empty_list(self):
        result = await execute_commands([])
        assert result == []


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
    async def test_dry_run_true_returns_dry_run_outcome(self, monkeypatch):
        """Regression: dry_run=True (default) sigue devolviendo outcome=dry_run."""
        monkeypatch.setattr("remediation.settings.remediation_dry_run", True)
        result = await execute_commands(["kubectl get pods"])
        assert len(result) == 1
        assert result[0].outcome == "dry_run"

    @pytest.mark.asyncio
    async def test_real_execution_success(self, monkeypatch):
        monkeypatch.setattr("remediation.settings.remediation_dry_run", False)
        monkeypatch.setattr("remediation.settings.remediation_command_timeout", 30)
        proc = _make_proc(stdout=b"pod/engine Running", returncode=0)
        with patch("remediation.asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)):
            result = await execute_commands(["kubectl get pods -n prod"])
        assert len(result) == 1
        assert result[0].outcome == "ok"
        assert result[0].success is True
        assert "pod/engine Running" in result[0].stdout

    @pytest.mark.asyncio
    async def test_real_execution_nonzero_exit_code(self, monkeypatch):
        monkeypatch.setattr("remediation.settings.remediation_dry_run", False)
        monkeypatch.setattr("remediation.settings.remediation_command_timeout", 30)
        proc = _make_proc(stderr=b"not found", returncode=1)
        with patch("remediation.asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)):
            result = await execute_commands(["kubectl get pods -n prod"])
        assert result[0].outcome == "failed"
        assert result[0].success is False
        assert result[0].exit_code == 1
        assert "not found" in result[0].stderr

    @pytest.mark.asyncio
    async def test_real_execution_timeout(self, monkeypatch):
        monkeypatch.setattr("remediation.settings.remediation_dry_run", False)
        monkeypatch.setattr("remediation.settings.remediation_command_timeout", 1)
        proc = _make_proc()
        proc.communicate = AsyncMock(side_effect=asyncio.TimeoutError)
        with patch("remediation.asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)):
            result = await execute_commands(["kubectl get pods -n prod"])
        assert result[0].outcome == "timeout"
        assert result[0].success is False

    @pytest.mark.asyncio
    async def test_real_execution_skips_non_kubectl(self, monkeypatch):
        monkeypatch.setattr("remediation.settings.remediation_dry_run", False)
        with patch("remediation.asyncio.create_subprocess_exec", new=AsyncMock()) as mock_exec:
            result = await execute_commands(["helm upgrade engine ./chart"])
        mock_exec.assert_not_called()
        assert result[0].outcome == "skip"
        assert "only kubectl commands allowed" in result[0].stderr

    @pytest.mark.asyncio
    async def test_real_execution_empty_list(self, monkeypatch):
        monkeypatch.setattr("remediation.settings.remediation_dry_run", False)
        result = await execute_commands([])
        assert result == []

    @pytest.mark.asyncio
    async def test_create_subprocess_raises_timeout_no_unbound_error(self, monkeypatch):
        """create_subprocess_exec raises TimeoutError before proc is assigned — outcome=timeout."""
        monkeypatch.setattr("remediation.settings.remediation_dry_run", False)
        monkeypatch.setattr("remediation.settings.remediation_command_timeout", 30)
        with patch("remediation.asyncio.create_subprocess_exec", new=AsyncMock(side_effect=asyncio.TimeoutError)):
            result = await execute_commands(["kubectl get pods -n prod"])
        assert result[0].outcome == "timeout"

    @pytest.mark.asyncio
    async def test_create_subprocess_raises_oserror_returns_error_entry(self, monkeypatch):
        """create_subprocess_exec raises OSError — outcome=error, no crash."""
        monkeypatch.setattr("remediation.settings.remediation_dry_run", False)
        monkeypatch.setattr("remediation.settings.remediation_command_timeout", 30)
        with patch("remediation.asyncio.create_subprocess_exec", new=AsyncMock(side_effect=OSError("no such binary"))):
            result = await execute_commands(["kubectl get pods -n prod"])
        assert result[0].outcome == "error"
        assert "no such binary" in result[0].stderr

    @pytest.mark.asyncio
    async def test_cancelled_error_propagates(self, monkeypatch):
        """CancelledError (BaseException) is re-raised explicitly, not swallowed."""
        monkeypatch.setattr("remediation.settings.remediation_dry_run", False)
        monkeypatch.setattr("remediation.settings.remediation_command_timeout", 30)
        proc = _make_proc()
        with patch("remediation.asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)), \
             patch("remediation.asyncio.wait_for", new=AsyncMock(side_effect=asyncio.CancelledError)):
            with pytest.raises(asyncio.CancelledError):
                await execute_commands(["kubectl get pods -n prod"])

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
        assert result[0].outcome == "ok"
        assert result[1].outcome == "failed"


# ── TestProcessRemediation ────────────────────────────────────────────────────

class TestProcessRemediation:

    @pytest.mark.asyncio
    async def test_suggest_only_when_disabled(self, monkeypatch):
        monkeypatch.setattr("remediation.settings.remediation_enabled", False)
        result = await process_remediation(mock_diagnosis_auto_remediate())
        assert result["action"] == RemediationAction.SUGGEST_ONLY
        assert result["execution_attempted"] is False

    @pytest.mark.asyncio
    async def test_escalate_with_blocked_command(self, monkeypatch):
        monkeypatch.setattr("remediation.settings.remediation_enabled", True)
        result = await process_remediation(mock_diagnosis_escalate())
        assert result["action"] == RemediationAction.ESCALATE
        assert result["execution_attempted"] is False
        assert len(result["blocked_commands"]) > 0

    @pytest.mark.asyncio
    async def test_auto_remediate_result_structure(self, monkeypatch):
        monkeypatch.setattr("remediation.settings.remediation_enabled", True)
        monkeypatch.setattr("remediation.settings.remediation_auto_max_risk", "low")
        monkeypatch.setattr("remediation.settings.remediation_auto_confidence", 0.8)
        result = await process_remediation(mock_diagnosis_auto_remediate())
        assert result["action"] == RemediationAction.AUTO_REMEDIATE
        assert result["execution_attempted"] is True
        assert "[DRY-RUN]" in result["execution_log"]
        assert "command_validations" in result
        assert "safe_commands" in result
        assert "blocked_commands" in result
        assert "execute_results" in result
        assert all(r.outcome == "dry_run" for r in result["execute_results"])

    @pytest.mark.asyncio
    async def test_no_commands_gives_suggest_only(self, monkeypatch):
        monkeypatch.setattr("remediation.settings.remediation_enabled", True)
        diagnosis = {**mock_diagnosis_result(), "commands": []}
        result = await process_remediation(diagnosis)
        assert result["action"] == RemediationAction.SUGGEST_ONLY
        assert result["execution_attempted"] is False

    @pytest.mark.asyncio
    async def test_rag_degraded_forces_escalate_with_approvable_commands(self, monkeypatch):
        """PR-04: un caso auto-remediable, con RAG degradado, escala con comandos aprobables (botones)."""
        monkeypatch.setattr("remediation.settings.remediation_enabled", True)
        monkeypatch.setattr("remediation.settings.remediation_auto_max_risk", "low")
        monkeypatch.setattr("remediation.settings.remediation_auto_confidence", 0.8)
        result = await process_remediation(mock_diagnosis_auto_remediate(), rag_degraded=True)
        assert result["action"] == RemediationAction.ESCALATE
        assert result["execution_attempted"] is False        # no se ejecutó nada
        assert len(result["safe_commands"]) > 0               # hay comandos que el operador puede aprobar


# ── TestParseMemoryToBytes ────────────────────────────────────────────────────

class TestParseMemoryToBytes:

    def test_mebibytes(self):
        assert parse_memory_to_bytes("256Mi") == 256 * 1024 ** 2

    def test_gibibytes(self):
        assert parse_memory_to_bytes("1Gi") == 1024 ** 3

    def test_kibibytes(self):
        assert parse_memory_to_bytes("512Ki") == 512 * 1024

    def test_raw_bytes(self):
        assert parse_memory_to_bytes("1024") == 1024

    def test_megabytes_si(self):
        assert parse_memory_to_bytes("500M") == 500 * 1000 ** 2

    def test_case_insensitive(self):
        assert parse_memory_to_bytes("256mi") == parse_memory_to_bytes("256Mi")

    def test_invalid_raises(self):
        import pytest
        with pytest.raises(ValueError):
            parse_memory_to_bytes("garbage")

    def test_empty_raises(self):
        import pytest
        with pytest.raises(ValueError):
            parse_memory_to_bytes("")


# ── TestImpliesPodRestart ─────────────────────────────────────────────────────

class TestImpliesPodRestart:

    def test_rollout_restart_is_restart(self):
        restarts, reason = implies_pod_restart("kubectl rollout restart deployment engine -n prod")
        assert restarts is True
        assert reason == "rollout_restart"

    def test_set_resources_deployment_is_restart(self):
        restarts, reason = implies_pod_restart(
            "kubectl set resources deployment engine --limits=memory=512Mi -n prod"
        )
        assert restarts is True
        assert reason == "set_resources_triggers_rollout"

    def test_scale_is_restart(self):
        restarts, reason = implies_pod_restart("kubectl scale deployment engine --replicas=2 -n prod")
        assert restarts is True
        assert reason == "scale_command"

    def test_patch_deployment_is_restart(self):
        restarts, reason = implies_pod_restart(
            'kubectl patch deployment engine -n prod -p \'{"spec":{"template":{"metadata":{"labels":{"ts":"1"}}}}}\'')
        assert restarts is True
        assert reason == "patch_workload_triggers_rollout"

    def test_scale_to_zero_is_restart(self):
        restarts, reason = implies_pod_restart("kubectl scale deployment engine --replicas=0 -n prod")
        assert restarts is True
        assert reason == "scale_command"

    def test_annotate_is_not_restart(self):
        restarts, _ = implies_pod_restart("kubectl annotate deployment engine note=ok -n prod")
        assert restarts is False

    def test_label_is_not_restart(self):
        restarts, _ = implies_pod_restart("kubectl label deployment engine env=prod -n prod")
        assert restarts is False

    def test_safe_get_is_not_restart(self):
        restarts, _ = implies_pod_restart("kubectl get pods -n prod")
        assert restarts is False

    def test_unknown_mutating_fail_safe(self):
        restarts, reason = implies_pod_restart("kubectl exec -it engine-pod -- /bin/sh")
        assert restarts is True
        assert reason == "unknown_mutating_command_fail_safe"


# ── TestDecideActionTutorRule ─────────────────────────────────────────────────

class TestDecideActionTutorRule:
    """Reglas 4.5 y 4.6 introducidas por condición del tutor."""

    def _validations(self, *commands):
        return validate_commands(list(commands))

    def _base_monkeypatch(self, monkeypatch):
        monkeypatch.setattr("remediation.settings.remediation_enabled", True)
        monkeypatch.setattr("remediation.settings.remediation_auto_max_risk", "low")
        monkeypatch.setattr("remediation.settings.remediation_auto_confidence", 0.8)

    # ── Rule 4.5: restart-implying commands → ESCALATE ────────────────────────

    def test_set_resources_deployment_escalates(self, monkeypatch):
        self._base_monkeypatch(monkeypatch)
        diagnosis = {**mock_diagnosis_auto_remediate(), "risk": "low", "confidence": 0.9}
        validations = self._validations(
            "kubectl set resources deployment engine --limits=memory=512Mi -n prod"
        )
        assert decide_action(diagnosis, validations) == RemediationAction.ESCALATE

    def test_rollout_restart_escalates(self, monkeypatch):
        self._base_monkeypatch(monkeypatch)
        diagnosis = {**mock_diagnosis_auto_remediate(), "risk": "low", "confidence": 0.9}
        validations = self._validations("kubectl rollout restart deployment engine -n prod")
        assert decide_action(diagnosis, validations) == RemediationAction.ESCALATE

    def test_patch_deployment_spec_template_escalates(self, monkeypatch):
        self._base_monkeypatch(monkeypatch)
        diagnosis = {**mock_diagnosis_auto_remediate(), "risk": "low", "confidence": 0.9}
        validations = self._validations(
            'kubectl patch deployment engine -n prod -p \'{"spec":{"template":{"metadata":{"labels":{"ts":"1"}}}}}\''
        )
        assert decide_action(diagnosis, validations) == RemediationAction.ESCALATE

    def test_scale_command_escalates(self, monkeypatch):
        self._base_monkeypatch(monkeypatch)
        diagnosis = {**mock_diagnosis_auto_remediate(), "risk": "low", "confidence": 0.9}
        validations = self._validations("kubectl scale deployment engine --replicas=0 -n prod")
        assert decide_action(diagnosis, validations) == RemediationAction.ESCALATE

    def test_restart_rule_overrides_low_risk(self, monkeypatch):
        """risk=low + confidence alta no basta si el comando reinicia pods."""
        self._base_monkeypatch(monkeypatch)
        diagnosis = {"risk": "low", "confidence": 0.99, "proposed_action": None}
        validations = self._validations("kubectl rollout restart deployment engine -n prod")
        assert decide_action(diagnosis, validations) == RemediationAction.ESCALATE

    # ── Rule 4.6: memory > 2× current → ESCALATE ─────────────────────────────

    def test_memory_4x_escalates(self, monkeypatch):
        self._base_monkeypatch(monkeypatch)
        diagnosis = {
            **mock_diagnosis_auto_remediate(),
            "risk": "low", "confidence": 0.9,
            "proposed_action": {
                "kind": "Deployment", "name": "engine", "namespace": "prod",
                "container": "engine", "field": "resources.limits.memory",
                "current_value": "256Mi", "new_value": "1Gi",
            },
        }
        validations = self._validations("kubectl annotate deployment engine note=ok -n prod")
        assert decide_action(diagnosis, validations) == RemediationAction.ESCALATE

    def test_memory_2x_non_restart_auto_remediates(self, monkeypatch):
        """256Mi → 512Mi (exactamente 2×) con comando no-restart → AUTO_REMEDIATE."""
        self._base_monkeypatch(monkeypatch)
        diagnosis = {
            **mock_diagnosis_auto_remediate(),
            "risk": "low", "confidence": 0.9,
            "proposed_action": {
                "kind": "Deployment", "name": "engine", "namespace": "prod",
                "container": "engine", "field": "resources.limits.memory",
                "current_value": "256Mi", "new_value": "512Mi",
            },
        }
        validations = self._validations("kubectl annotate deployment engine note=ok -n prod")
        assert decide_action(diagnosis, validations) == RemediationAction.AUTO_REMEDIATE

    def test_memory_unparseable_escalates(self, monkeypatch):
        self._base_monkeypatch(monkeypatch)
        diagnosis = {
            **mock_diagnosis_auto_remediate(),
            "risk": "low", "confidence": 0.9,
            "proposed_action": {
                "kind": "Deployment", "name": "engine", "namespace": "prod",
                "container": "engine", "field": "resources.limits.memory",
                "current_value": "256Mi", "new_value": "not-a-value",
            },
        }
        validations = self._validations("kubectl annotate deployment engine note=ok -n prod")
        assert decide_action(diagnosis, validations) == RemediationAction.ESCALATE

    def test_memory_zero_current_escalates(self, monkeypatch):
        """Zero current memory → cannot evaluate 2x rule → reason_code zero_current_memory → ESCALATE."""
        self._base_monkeypatch(monkeypatch)
        diagnosis = {
            **mock_diagnosis_auto_remediate(),
            "risk": "low", "confidence": 0.9,
            "proposed_action": {
                "kind": "Deployment", "name": "engine", "namespace": "prod",
                "container": "engine", "field": "resources.limits.memory",
                "current_value": "0Mi", "new_value": "256Mi",
            },
        }
        validations = self._validations("kubectl annotate deployment engine note=ok -n prod")
        assert decide_action(diagnosis, validations) == RemediationAction.ESCALATE

    def test_no_proposed_action_falls_back_to_legacy(self, monkeypatch):
        """Sin proposed_action, regla 4.6 se salta; decision la toman risk/confidence."""
        self._base_monkeypatch(monkeypatch)
        diagnosis = {**mock_diagnosis_auto_remediate(), "proposed_action": None}
        validations = self._validations(
            "kubectl describe pod engine-pod -n prod",
            "kubectl annotate deployment engine note=ok -n prod",
        )
        assert decide_action(diagnosis, validations) == RemediationAction.AUTO_REMEDIATE

    def test_proposed_action_non_memory_field_skipped(self, monkeypatch):
        """proposed_action con field distinto a resources.limits.memory → no aplica regla 4.6."""
        self._base_monkeypatch(monkeypatch)
        diagnosis = {
            **mock_diagnosis_auto_remediate(),
            "risk": "low", "confidence": 0.9,
            "proposed_action": {
                "kind": "Deployment", "name": "engine", "namespace": "prod",
                "container": "engine", "field": "spec.replicas",
                "current_value": "1", "new_value": "3",
            },
        }
        validations = self._validations("kubectl annotate deployment engine note=ok -n prod")
        assert decide_action(diagnosis, validations) == RemediationAction.AUTO_REMEDIATE

    def test_memory_missing_current_value_escalates(self, monkeypatch):
        """Missing current_value in proposed_action → reason_code missing_memory_value → ESCALATE."""
        self._base_monkeypatch(monkeypatch)
        diagnosis = {
            **mock_diagnosis_auto_remediate(),
            "risk": "low", "confidence": 0.9,
            "proposed_action": {
                "kind": "Deployment", "name": "engine", "namespace": "prod",
                "container": "engine", "field": "resources.limits.memory",
                "new_value": "512Mi",
            },
        }
        validations = self._validations("kubectl annotate deployment engine note=ok -n prod")
        assert decide_action(diagnosis, validations) == RemediationAction.ESCALATE

    def test_memory_missing_new_value_escalates(self, monkeypatch):
        """Missing new_value in proposed_action → reason_code missing_memory_value → ESCALATE."""
        self._base_monkeypatch(monkeypatch)
        diagnosis = {
            **mock_diagnosis_auto_remediate(),
            "risk": "low", "confidence": 0.9,
            "proposed_action": {
                "kind": "Deployment", "name": "engine", "namespace": "prod",
                "container": "engine", "field": "resources.limits.memory",
                "current_value": "256Mi",
            },
        }
        validations = self._validations("kubectl annotate deployment engine note=ok -n prod")
        assert decide_action(diagnosis, validations) == RemediationAction.ESCALATE

    # ── Rule 4.5 exception (tutor-approved 2026-05-23) ────────────────────────

    def _memory_proposed_action(self, current: str = "256Mi", new: str = "512Mi") -> dict:
        return {
            "kind": "Deployment", "name": "engine", "namespace": "arturo-llm-test",
            "container": "engine", "field": "resources.limits.memory",
            "current_value": current, "new_value": new,
        }

    def test_set_resources_memory_exception_auto_remediates(self, monkeypatch):
        """set resources memory + conf≥0.9 + risk=low + bump≤2× → rule 4.5 exception → AUTO_REMEDIATE."""
        self._base_monkeypatch(monkeypatch)
        diagnosis = {
            **mock_diagnosis_auto_remediate(),
            "risk": "low", "confidence": 0.9,
            "proposed_action": self._memory_proposed_action("256Mi", "512Mi"),
        }
        validations = self._validations(
            "kubectl set resources deployment engine --limits=memory=512Mi -n arturo-llm-test"
        )
        assert decide_action(diagnosis, validations) == RemediationAction.AUTO_REMEDIATE

    def test_set_resources_exception_medium_risk_with_medium_max(self, monkeypatch):
        """risk=medium passes the exception (≤ medium); rule 5 also needs max_risk=medium to allow it."""
        self._base_monkeypatch(monkeypatch)
        monkeypatch.setattr("remediation.settings.remediation_auto_max_risk", "medium")
        diagnosis = {
            **mock_diagnosis_auto_remediate(),
            "risk": "medium", "confidence": 0.9,
            "proposed_action": self._memory_proposed_action("256Mi", "512Mi"),
        }
        validations = self._validations(
            "kubectl set resources deployment engine --limits=memory=512Mi -n arturo-llm-test"
        )
        assert decide_action(diagnosis, validations) == RemediationAction.AUTO_REMEDIATE

    def test_set_resources_confidence_above_base_floor_auto(self, monkeypatch):
        """confidence=0.85 ≥ 0.8 base floor → AUTO_REMEDIATE.

        New contract: the structured path no longer requires the old 0.9 exception threshold;
        the model's diagnostic confidence is gated only by the base rule-6 floor (0.8). The
        sub-floor case (conf < 0.8 → SUGGEST_ONLY) is covered in TestStructuredAutoRemediation.
        """
        self._base_monkeypatch(monkeypatch)
        diagnosis = {
            **mock_diagnosis_auto_remediate(),
            "risk": "low", "confidence": 0.85,
            "proposed_action": self._memory_proposed_action("256Mi", "512Mi"),
        }
        validations = self._validations(
            "kubectl set resources deployment engine --limits=memory=512Mi -n arturo-llm-test"
        )
        assert decide_action(diagnosis, validations) == RemediationAction.AUTO_REMEDIATE

    def test_set_resources_high_risk_auto_via_engine_bound(self, monkeypatch):
        """risk=high but structured (bounded ≤2× + reversible) → rule 5 bypassed → AUTO_REMEDIATE.

        New contract (re-sourcing): the engine's deterministic bound supersedes the model's
        risk self-rating for an engine-authored set-resources raise. Was ESCALATE pre-re-sourcing.
        """
        self._base_monkeypatch(monkeypatch)
        diagnosis = {
            **mock_diagnosis_auto_remediate(),
            "risk": "high", "confidence": 0.9,
            "proposed_action": self._memory_proposed_action("256Mi", "512Mi"),
        }
        validations = self._validations(
            "kubectl set resources deployment engine --limits=memory=512Mi -n arturo-llm-test"
        )
        assert decide_action(diagnosis, validations) == RemediationAction.AUTO_REMEDIATE

    def test_set_resources_non_memory_field_escalates(self, monkeypatch):
        """field=resources.limits.cpu → exception gated (auto-cpu flag off, default) → rule 4.5 blocks → ESCALATE."""
        self._base_monkeypatch(monkeypatch)
        diagnosis = {
            **mock_diagnosis_auto_remediate(),
            "risk": "low", "confidence": 0.9,
            "proposed_action": {
                "kind": "Deployment", "name": "engine", "namespace": "arturo-llm-test",
                "container": "engine", "field": "resources.limits.cpu",
                "current_value": "100m", "new_value": "200m",
            },
        }
        validations = self._validations(
            "kubectl set resources deployment engine --limits=memory=512Mi -n arturo-llm-test"
        )
        assert decide_action(diagnosis, validations) == RemediationAction.ESCALATE

    def test_set_resources_no_proposed_action_escalates(self, monkeypatch):
        """proposed_action=None → exception not granted (no field to check) → rule 4.5 blocks → ESCALATE."""
        self._base_monkeypatch(monkeypatch)
        diagnosis = {**mock_diagnosis_auto_remediate(), "risk": "low", "confidence": 0.9, "proposed_action": None}
        validations = self._validations(
            "kubectl set resources deployment engine --limits=memory=512Mi -n arturo-llm-test"
        )
        assert decide_action(diagnosis, validations) == RemediationAction.ESCALATE

    def test_scale_escalates_despite_high_conf_low_risk(self, monkeypatch):
        """scale is not covered by the exception (only set-resources) → ESCALATE. Critical guard."""
        self._base_monkeypatch(monkeypatch)
        diagnosis = {
            **mock_diagnosis_auto_remediate(),
            "risk": "low", "confidence": 0.9,
            "proposed_action": self._memory_proposed_action(),
        }
        validations = self._validations("kubectl scale deployment engine --replicas=2 -n arturo-llm-test")
        assert decide_action(diagnosis, validations) == RemediationAction.ESCALATE

    def test_rollout_restart_escalates_despite_high_conf(self, monkeypatch):
        """rollout restart is not covered by the exception → ESCALATE."""
        self._base_monkeypatch(monkeypatch)
        diagnosis = {
            **mock_diagnosis_auto_remediate(),
            "risk": "low", "confidence": 0.9,
            "proposed_action": self._memory_proposed_action(),
        }
        validations = self._validations("kubectl rollout restart deployment engine -n arturo-llm-test")
        assert decide_action(diagnosis, validations) == RemediationAction.ESCALATE

    def test_set_resources_plus_scale_escalates(self, monkeypatch):
        """Authorized set-resources + scale in same list: exception lets set-resources through,
        scale (second command) still escalates via rule 4.5. continue does not skip remaining."""
        self._base_monkeypatch(monkeypatch)
        diagnosis = {
            **mock_diagnosis_auto_remediate(),
            "risk": "low", "confidence": 0.9,
            "proposed_action": self._memory_proposed_action(),
        }
        validations = self._validations(
            "kubectl set resources deployment engine --limits=memory=512Mi -n arturo-llm-test",
            "kubectl scale deployment engine --replicas=0 -n arturo-llm-test",
        )
        assert decide_action(diagnosis, validations) == RemediationAction.ESCALATE

    def test_set_resources_exception_then_46_caps_memory(self, monkeypatch):
        """Passes rule 4.5 exception but new_value=1Gi > 2× 256Mi → rule 4.6 escalates. Defense in depth."""
        self._base_monkeypatch(monkeypatch)
        diagnosis = {
            **mock_diagnosis_auto_remediate(),
            "risk": "low", "confidence": 0.9,
            "proposed_action": self._memory_proposed_action("256Mi", "1Gi"),
        }
        validations = self._validations(
            "kubectl set resources deployment engine --limits=memory=1Gi -n arturo-llm-test"
        )
        assert decide_action(diagnosis, validations) == RemediationAction.ESCALATE


# ── TestClassifyCommandTypeGuard ──────────────────────────────────────────────

class TestClassifyCommandTypeGuard:
    """R1 — classify_command rechaza inputs no-string sin lanzar AttributeError."""

    def test_none_returns_unknown(self):
        assert classify_command(None) == CommandSafety.UNKNOWN

    def test_integer_returns_unknown(self):
        assert classify_command(123) == CommandSafety.UNKNOWN

    def test_list_returns_unknown(self):
        assert classify_command(["kubectl", "get", "pods"]) == CommandSafety.UNKNOWN


# ── TestValidateCommandsNonString ─────────────────────────────────────────────

class TestValidateCommandsNonString:
    """R2 — validate_commands filtra items no-string con warning."""

    def test_none_in_list_is_skipped(self):
        result = validate_commands([None, "kubectl get pods"])
        assert len(result) == 1
        assert result[0]["safety"] == CommandSafety.SAFE

    def test_integer_in_list_is_skipped(self):
        result = validate_commands([42, "kubectl get pods"])
        assert len(result) == 1
        assert result[0]["command"] == "kubectl get pods"

    def test_all_non_strings_returns_empty(self):
        result = validate_commands([None, 42, True])
        assert result == []

    def test_mixed_valid_strings_preserved(self):
        result = validate_commands(["kubectl get pods", None, "kubectl drain node-1"])
        assert len(result) == 2
        assert result[0]["safety"] == CommandSafety.SAFE
        assert result[1]["safety"] == CommandSafety.BLOCKED


# ── TestCapturePrePatchValue ──────────────────────────────────────────────────

class TestCapturePrePatchValue:
    """capture_pre_patch_value() queries the cluster or falls back to LLM value."""

    def _proposed(self, **kwargs):
        base = {
            "kind": "Deployment", "name": "engine", "namespace": "prod",
            "container": "engine", "field": "resources.limits.memory",
            "current_value": "256Mi", "new_value": "512Mi",
        }
        return {**base, **kwargs}

    @pytest.mark.asyncio
    async def test_dry_run_returns_snapshot_with_llm_value(self, monkeypatch):
        monkeypatch.setattr("remediation.settings.remediation_dry_run", True)
        result = await capture_pre_patch_value(self._proposed())
        assert result is not None
        assert result.value == "256Mi"
        assert result.deployment == "engine"
        assert result.namespace == "prod"
        assert result.container == "engine"

    @pytest.mark.asyncio
    async def test_dry_run_missing_current_value_uses_empty_string(self, monkeypatch):
        monkeypatch.setattr("remediation.settings.remediation_dry_run", True)
        result = await capture_pre_patch_value(self._proposed(current_value=None))
        assert result is not None
        assert result.value == ""

    @pytest.mark.asyncio
    async def test_returns_none_for_non_dict(self, monkeypatch):
        monkeypatch.setattr("remediation.settings.remediation_dry_run", True)
        assert await capture_pre_patch_value(None) is None
        assert await capture_pre_patch_value("string") is None

    @pytest.mark.asyncio
    async def test_returns_none_when_missing_required_fields(self, monkeypatch):
        monkeypatch.setattr("remediation.settings.remediation_dry_run", True)
        assert await capture_pre_patch_value({"name": "engine"}) is None  # missing namespace+container

    @pytest.mark.asyncio
    async def test_real_mode_success(self, monkeypatch):
        monkeypatch.setattr("remediation.settings.remediation_dry_run", False)
        monkeypatch.setattr("remediation.settings.remediation_command_timeout", 30)
        proc = _make_proc(stdout=b"engine:512Mi;sidecar:128Mi;", returncode=0)
        with patch("remediation.asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)):
            result = await capture_pre_patch_value(self._proposed())
        assert result is not None
        assert result.value == "512Mi"

    @pytest.mark.asyncio
    async def test_dry_run_cpu_field_returns_snapshot(self, monkeypatch):
        monkeypatch.setattr("remediation.settings.remediation_dry_run", True)
        result = await capture_pre_patch_value(
            self._proposed(field="resources.limits.cpu", current_value="250m", new_value="500m")
        )
        assert result is not None
        assert result.value == "250m"
        assert result.field == "resources.limits.cpu"

    @pytest.mark.asyncio
    async def test_real_mode_cpu_field_queries_cpu_jsonpath(self, monkeypatch):
        """F3 slice 1b: el jsonpath usa resources.limits.cpu y parsea el valor cpu."""
        monkeypatch.setattr("remediation.settings.remediation_dry_run", False)
        monkeypatch.setattr("remediation.settings.remediation_command_timeout", 30)
        proc = _make_proc(stdout=b"engine:500m;sidecar:100m;", returncode=0)
        mock_exec = AsyncMock(return_value=proc)
        with patch("remediation.asyncio.create_subprocess_exec", new=mock_exec):
            result = await capture_pre_patch_value(
                self._proposed(field="resources.limits.cpu", current_value="250m", new_value="500m")
            )
        assert result is not None
        assert result.value == "500m"
        # el comando kubectl debe consultar el límite de cpu, no el de memory
        jsonpath_arg = " ".join(str(a) for a in mock_exec.call_args.args)
        assert "resources.limits.cpu" in jsonpath_arg
        assert "resources.limits.memory" not in jsonpath_arg

    @pytest.mark.asyncio
    async def test_real_mode_kubectl_fails_returns_none(self, monkeypatch):
        monkeypatch.setattr("remediation.settings.remediation_dry_run", False)
        monkeypatch.setattr("remediation.settings.remediation_command_timeout", 30)
        proc = _make_proc(stderr=b"not found", returncode=1)
        with patch("remediation.asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)):
            result = await capture_pre_patch_value(self._proposed())
        assert result is None

    @pytest.mark.asyncio
    async def test_real_mode_container_not_found_returns_none(self, monkeypatch):
        monkeypatch.setattr("remediation.settings.remediation_dry_run", False)
        monkeypatch.setattr("remediation.settings.remediation_command_timeout", 30)
        proc = _make_proc(stdout=b"other-container:256Mi;", returncode=0)
        with patch("remediation.asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)):
            result = await capture_pre_patch_value(self._proposed())
        assert result is None

    @pytest.mark.asyncio
    async def test_real_mode_exception_returns_none(self, monkeypatch):
        monkeypatch.setattr("remediation.settings.remediation_dry_run", False)
        with patch("remediation.asyncio.create_subprocess_exec", new=AsyncMock(side_effect=OSError("no binary"))):
            result = await capture_pre_patch_value(self._proposed())
        assert result is None

    @pytest.mark.asyncio
    async def test_selector_uses_match_labels_when_sealed(self, monkeypatch):
        """Sealed match_labels build the real multi-label selector (no app={name} guess)."""
        monkeypatch.setattr("remediation.settings.remediation_dry_run", True)
        proposed = self._proposed(match_labels={"app": "engine", "tier": "backend"})
        result = await capture_pre_patch_value(proposed)
        assert result is not None
        assert result.selector == "app=engine,tier=backend"

    @pytest.mark.asyncio
    async def test_selector_falls_back_to_app_name_without_match_labels(self, monkeypatch):
        """No enrichment (dry-run/legacy) → keep the app={name} fallback (backward-compat)."""
        monkeypatch.setattr("remediation.settings.remediation_dry_run", True)
        result = await capture_pre_patch_value(self._proposed())
        assert result is not None
        assert result.selector == "app=engine"


# ── TestCheckPodHealth ────────────────────────────────────────────────────────

class TestCheckPodHealth:
    """check_pod_health() assesses pod state after a remediation patch."""

    def _snapshot(self):
        return PrePatchSnapshot(
            deployment="engine", namespace="prod", container="engine",
            field="resources.limits.memory", value="256Mi", selector="app=engine",
        )

    @pytest.mark.asyncio
    async def test_dry_run_always_healthy(self, monkeypatch):
        monkeypatch.setattr("remediation.settings.remediation_dry_run", True)
        result = await check_pod_health(self._snapshot())
        assert result.healthy is True
        assert result.reason == "dry_run"

    @pytest.mark.asyncio
    async def test_all_running_zero_restarts_is_healthy(self, monkeypatch):
        monkeypatch.setattr("remediation.settings.remediation_dry_run", False)
        monkeypatch.setattr("remediation.settings.remediation_command_timeout", 30)
        proc = _make_proc(stdout=b"Running|0;Running|0;", returncode=0)
        with patch("remediation.asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)):
            result = await check_pod_health(self._snapshot())
        assert result.healthy is True
        assert result.observed_phases == ["Running", "Running"]

    @pytest.mark.asyncio
    async def test_pending_pod_is_unhealthy(self, monkeypatch):
        monkeypatch.setattr("remediation.settings.remediation_dry_run", False)
        monkeypatch.setattr("remediation.settings.remediation_command_timeout", 30)
        proc = _make_proc(stdout=b"Pending|0;", returncode=0)
        with patch("remediation.asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)):
            result = await check_pod_health(self._snapshot())
        assert result.healthy is False
        assert "Pending" in result.reason

    @pytest.mark.asyncio
    async def test_restart_count_nonzero_is_unhealthy(self, monkeypatch):
        monkeypatch.setattr("remediation.settings.remediation_dry_run", False)
        monkeypatch.setattr("remediation.settings.remediation_command_timeout", 30)
        proc = _make_proc(stdout=b"Running|3;", returncode=0)
        with patch("remediation.asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)):
            result = await check_pod_health(self._snapshot())
        assert result.healthy is False
        assert result.observed_restarts == [3]

    @pytest.mark.asyncio
    async def test_no_pods_found_is_unhealthy(self, monkeypatch):
        monkeypatch.setattr("remediation.settings.remediation_dry_run", False)
        monkeypatch.setattr("remediation.settings.remediation_command_timeout", 30)
        proc = _make_proc(stdout=b"", returncode=0)
        with patch("remediation.asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)):
            result = await check_pod_health(self._snapshot())
        assert result.healthy is False
        assert result.reason == "no_pods_found"

    @pytest.mark.asyncio
    async def test_kubectl_error_is_unhealthy(self, monkeypatch):
        monkeypatch.setattr("remediation.settings.remediation_dry_run", False)
        monkeypatch.setattr("remediation.settings.remediation_command_timeout", 30)
        with patch("remediation.asyncio.create_subprocess_exec", new=AsyncMock(side_effect=OSError("fail"))):
            result = await check_pod_health(self._snapshot())
        assert result.healthy is False
        assert "kubectl_error" in result.reason

    @pytest.mark.asyncio
    async def test_kubectl_nonzero_exit_is_unhealthy(self, monkeypatch):
        monkeypatch.setattr("remediation.settings.remediation_dry_run", False)
        monkeypatch.setattr("remediation.settings.remediation_command_timeout", 30)
        proc = _make_proc(stderr=b"forbidden", returncode=1)
        with patch("remediation.asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)):
            result = await check_pod_health(self._snapshot())
        assert result.healthy is False
        assert "kubectl_failed" in result.reason


# ── TestRevertPatch ───────────────────────────────────────────────────────────

class TestRevertPatch:
    """revert_patch() builds and executes the kubectl set resources rollback command."""

    def _snapshot(self, value="256Mi"):
        return PrePatchSnapshot(
            deployment="engine", namespace="prod", container="engine",
            field="resources.limits.memory", value=value, selector="app=engine",
        )

    @pytest.mark.asyncio
    async def test_dry_run_returns_dry_run_outcome(self, monkeypatch):
        monkeypatch.setattr("remediation.settings.remediation_dry_run", True)
        result = await revert_patch(self._snapshot())
        assert result.outcome == "dry_run"
        assert result.success is True
        assert "256Mi" in result.command

    @pytest.mark.asyncio
    async def test_real_mode_success_returns_ok_outcome(self, monkeypatch):
        monkeypatch.setattr("remediation.settings.remediation_dry_run", False)
        monkeypatch.setattr("remediation.settings.remediation_command_timeout", 30)
        proc = _make_proc(stdout=b"deployment.apps/engine resource requirements updated", returncode=0)
        with patch("remediation.asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)):
            result = await revert_patch(self._snapshot())
        assert result.outcome == "ok"
        assert result.success is True

    @pytest.mark.asyncio
    async def test_empty_value_returns_error(self, monkeypatch):
        monkeypatch.setattr("remediation.settings.remediation_dry_run", False)
        result = await revert_patch(self._snapshot(value=""))
        assert result.outcome == "error"
        assert result.success is False

    @pytest.mark.asyncio
    async def test_cpu_field_builds_cpu_limits_command(self, monkeypatch):
        """F3 slice 1b: con field=cpu el comando revierte --limits=cpu, no memory."""
        monkeypatch.setattr("remediation.settings.remediation_dry_run", True)
        snapshot = PrePatchSnapshot(
            deployment="engine", namespace="prod", container="engine",
            field="resources.limits.cpu", value="500m", selector="app=engine",
        )
        result = await revert_patch(snapshot)
        assert "--limits=cpu=500m" in result.command
        assert "memory" not in result.command


# ── TestProcessRemediationSnapshot ────────────────────────────────────────────

class TestProcessRemediationSnapshot:
    """process_remediation() captures pre_patch_snapshot when AUTO_REMEDIATE."""

    @pytest.mark.asyncio
    async def test_snapshot_captured_when_auto_remediate(self, monkeypatch):
        monkeypatch.setattr("remediation.settings.remediation_enabled", True)
        monkeypatch.setattr("remediation.settings.remediation_auto_max_risk", "low")
        monkeypatch.setattr("remediation.settings.remediation_auto_confidence", 0.8)
        monkeypatch.setattr("remediation.settings.remediation_dry_run", True)
        diagnosis = {
            **mock_diagnosis_auto_remediate(),
            "risk": "low", "confidence": 0.9,
            "proposed_action": {
                "kind": "Deployment", "name": "engine", "namespace": "prod",
                "container": "engine", "field": "resources.limits.memory",
                "current_value": "256Mi", "new_value": "512Mi",
            },
        }
        result = await process_remediation(diagnosis)
        assert result["action"] == RemediationAction.AUTO_REMEDIATE
        assert result["pre_patch_snapshot"] is not None
        assert result["pre_patch_snapshot"].value == "256Mi"

    @pytest.mark.asyncio
    async def test_snapshot_none_when_escalate(self, monkeypatch):
        monkeypatch.setattr("remediation.settings.remediation_enabled", True)
        result = await process_remediation(mock_diagnosis_escalate())
        assert result["action"] == RemediationAction.ESCALATE
        assert result["pre_patch_snapshot"] is None

    @pytest.mark.asyncio
    async def test_snapshot_none_when_no_proposed_action(self, monkeypatch):
        monkeypatch.setattr("remediation.settings.remediation_enabled", True)
        monkeypatch.setattr("remediation.settings.remediation_auto_max_risk", "low")
        monkeypatch.setattr("remediation.settings.remediation_auto_confidence", 0.8)
        monkeypatch.setattr("remediation.settings.remediation_dry_run", True)
        diagnosis = {**mock_diagnosis_auto_remediate(), "proposed_action": None}
        result = await process_remediation(diagnosis)
        assert result["pre_patch_snapshot"] is None


# ── TestProcessRemediationCpuAuto ─────────────────────────────────────────────

class TestProcessRemediationCpuAuto:
    """F3 slice 2 — el camino auto-CPU recorrido entero por process_remediation().

    A diferencia de TestDecideActionCpuAuto (que aísla decide_action) y de los tests
    de captura/revert CPU aislados, esto engancha las piezas de extremo a extremo:
    validate → decide (gate del flag) → capture (field-aware) → execute → revert
    (field-aware). Todo en dry-run, determinista, sin cluster ni LLM.
    """

    def _base_monkeypatch(self, monkeypatch):
        monkeypatch.setattr("remediation.settings.remediation_enabled", True)
        monkeypatch.setattr("remediation.settings.remediation_auto_max_risk", "low")
        monkeypatch.setattr("remediation.settings.remediation_auto_confidence", 0.8)
        monkeypatch.setattr("remediation.settings.remediation_dry_run", True)

    def _cpu_diagnosis(self):
        return {
            **mock_diagnosis_auto_remediate(),
            "risk": "low", "confidence": 0.9,
            "commands": [
                "kubectl set resources deployment engine --containers=engine --limits=cpu=500m -n prod"
            ],
            "proposed_action": {
                "kind": "Deployment", "name": "engine", "namespace": "arturo-llm-test",
                "container": "engine", "field": "resources.limits.cpu",
                "current_value": "250m", "new_value": "500m",
            },
        }

    @pytest.mark.asyncio
    async def test_flag_off_escalates_without_effects(self, monkeypatch):
        """Flag OFF (default): escalate-first → ESCALATE, sin snapshot ni ejecución."""
        self._base_monkeypatch(monkeypatch)
        monkeypatch.setattr("remediation.settings.remediation_auto_cpu_enabled", False)
        result = await process_remediation(self._cpu_diagnosis())
        assert result["action"] == RemediationAction.ESCALATE
        assert result["pre_patch_snapshot"] is None
        assert result["execute_results"] == []
        assert result["execution_attempted"] is False

    @pytest.mark.asyncio
    async def test_flag_on_auto_remediates_with_cpu_snapshot(self, monkeypatch):
        """Flag ON: AUTO_REMEDIATE → captura CPU field-aware + ejecuta set resources cpu."""
        self._base_monkeypatch(monkeypatch)
        monkeypatch.setattr("remediation.settings.remediation_auto_cpu_enabled", True)
        result = await process_remediation(self._cpu_diagnosis())
        assert result["action"] == RemediationAction.AUTO_REMEDIATE
        assert result["pre_patch_snapshot"] is not None
        assert result["pre_patch_snapshot"].value == "250m"
        assert result["pre_patch_snapshot"].field == "resources.limits.cpu"
        assert any(
            "--limits=cpu=500m" in r.command for r in result["execute_results"]
        )

    @pytest.mark.asyncio
    async def test_rollback_from_snapshot_reverts_cpu(self, monkeypatch):
        """Cierra el lazo: el snapshot capturado revierte CPU (no memory)."""
        self._base_monkeypatch(monkeypatch)
        monkeypatch.setattr("remediation.settings.remediation_auto_cpu_enabled", True)
        result = await process_remediation(self._cpu_diagnosis())
        rollback = await revert_patch(result["pre_patch_snapshot"])
        assert "--limits=cpu=250m" in rollback.command
        assert "memory" not in rollback.command


# ── TestStructuredAutoRemediation ─────────────────────────────────────────────

class TestStructuredAutoRemediation:
    """F3 re-sourcing — the engine-authored auto path that fires on the REAL model output.

    F4 showed the model emits a correct structured proposed_action 5/5 but rarely the
    executable command (has_set_resources=null 4/5), and rates risk=high / conf≈0.84. The old
    engine escalated on the model's risk float, so auto never fired E2E. Here the engine
    synthesizes the bounded set-resources command itself and owns the risk decision; the
    model's diagnostic confidence is still gated. Deterministic, dry-run, no cluster/LLM.
    """

    _INVESTIGATIVE = [
        "kubectl describe deployment engine -n arturo-llm-test",
        "kubectl top pod -n arturo-llm-test",
    ]

    def _base_monkeypatch(self, monkeypatch):
        monkeypatch.setattr("remediation.settings.remediation_enabled", True)
        monkeypatch.setattr("remediation.settings.remediation_auto_max_risk", "low")
        monkeypatch.setattr("remediation.settings.remediation_auto_confidence", 0.8)
        monkeypatch.setattr("remediation.settings.remediation_dry_run", True)

    def _memory_diagnosis(self, current="256Mi", new="512Mi", risk="high", confidence=0.84):
        return {
            **mock_diagnosis_auto_remediate(),
            "risk": risk, "confidence": confidence,
            "commands": list(self._INVESTIGATIVE),  # investigative only — model emits no executable command
            "proposed_action": {
                "kind": "Deployment", "name": "engine", "namespace": "arturo-llm-test",
                "container": "engine", "field": "resources.limits.memory",
                "current_value": current, "new_value": new,
            },
        }

    def _cpu_diagnosis(self, current="250m", new="500m", risk="high", confidence=0.84):
        return {
            **mock_diagnosis_auto_remediate(),
            "risk": risk, "confidence": confidence,
            "commands": list(self._INVESTIGATIVE),
            "proposed_action": {
                "kind": "Deployment", "name": "engine", "namespace": "arturo-llm-test",
                "container": "engine", "field": "resources.limits.cpu",
                "current_value": current, "new_value": new,
            },
        }

    @pytest.mark.asyncio
    async def test_high_risk_investigative_only_auto_via_engine(self, monkeypatch):
        """THE headline case: risk=high, conf=0.84, only investigative commands, valid
        proposed_action → AUTO_REMEDIATE via the engine-synthesized command."""
        self._base_monkeypatch(monkeypatch)
        result = await process_remediation(self._memory_diagnosis())
        assert result["action"] == RemediationAction.AUTO_REMEDIATE
        # Engine ran its OWN bounded set-resources command, not the model's investigative ones
        assert any(
            "kubectl set resources" in r.command and "--limits=memory=512Mi" in r.command
            for r in result["execute_results"]
        )
        assert not any("describe" in r.command or "top" in r.command for r in result["execute_results"])
        assert result["pre_patch_snapshot"] is not None
        assert result["pre_patch_snapshot"].value == "256Mi"

    @pytest.mark.asyncio
    async def test_cpu_high_risk_auto_via_engine_with_flag(self, monkeypatch):
        """CPU symmetric: flag ON + structured cpu raise + risk=high → AUTO + synth cpu command."""
        self._base_monkeypatch(monkeypatch)
        monkeypatch.setattr("remediation.settings.remediation_auto_cpu_enabled", True)
        result = await process_remediation(self._cpu_diagnosis())
        assert result["action"] == RemediationAction.AUTO_REMEDIATE
        assert any("--limits=cpu=500m" in r.command for r in result["execute_results"])
        assert result["pre_patch_snapshot"].field == "resources.limits.cpu"

    @pytest.mark.asyncio
    async def test_cpu_flag_off_still_escalates(self, monkeypatch):
        """CPU without the flag is not structured-eligible → risk gate applies → ESCALATE."""
        self._base_monkeypatch(monkeypatch)
        monkeypatch.setattr("remediation.settings.remediation_auto_cpu_enabled", False)
        result = await process_remediation(self._cpu_diagnosis())
        assert result["action"] == RemediationAction.ESCALATE
        assert result["execute_results"] == []

    @pytest.mark.asyncio
    async def test_lowering_limit_not_structured_escalates(self, monkeypatch):
        """Guardrail only-raise: new < current is not structured → risk gate applies → ESCALATE
        (never auto-lower a limit)."""
        self._base_monkeypatch(monkeypatch)
        result = await process_remediation(self._memory_diagnosis(current="512Mi", new="256Mi"))
        assert result["action"] == RemediationAction.ESCALATE
        assert result["execute_results"] == []

    @pytest.mark.asyncio
    async def test_foreign_namespace_not_structured_escalates(self, monkeypatch):
        """Guardrail (Slice 4) blast-radius: a structured-looking raise in a non-tenant
        namespace is NOT engine-authorable → risk gate applies → ESCALATE (no auto cross-tenant)."""
        self._base_monkeypatch(monkeypatch)
        diag = self._memory_diagnosis()
        diag["proposed_action"]["namespace"] = "kube-system"
        result = await process_remediation(diag)
        assert result["action"] == RemediationAction.ESCALATE
        assert result["execute_results"] == []

    @pytest.mark.asyncio
    async def test_confidence_below_floor_suggests_only(self, monkeypatch):
        """Confidence floor still bites: conf=0.7 < 0.8 → SUGGEST_ONLY even when structured.

        Re-sourcing dropped the model's RISK gate, not its diagnostic CONFIDENCE gate.
        """
        self._base_monkeypatch(monkeypatch)
        result = await process_remediation(self._memory_diagnosis(confidence=0.7))
        assert result["action"] == RemediationAction.SUGGEST_ONLY
        assert result["execute_results"] == []

    def test_is_structured_remediation_eligibility(self, monkeypatch):
        """Unit: the eligibility predicate that drives the whole structured path."""
        monkeypatch.setattr("remediation.settings.remediation_auto_cpu_enabled", False)
        mem = lambda **kw: {"action": "patch", **{
            "kind": "Deployment", "name": "engine", "namespace": "arturo-llm-test",
            "container": "engine", "field": "resources.limits.memory",
            "current_value": "256Mi", "new_value": "512Mi", **kw}}
        assert is_structured_remediation({"proposed_action": mem()}) is True
        # only-raise
        assert is_structured_remediation({"proposed_action": mem(current_value="512Mi", new_value="256Mi")}) is False
        # missing target
        assert is_structured_remediation({"proposed_action": mem(container=None)}) is False
        # foreign namespace (blast-radius guardrail)
        assert is_structured_remediation({"proposed_action": mem(namespace="kube-system")}) is False
        # unparseable
        assert is_structured_remediation({"proposed_action": mem(new_value="lots")}) is False
        # no proposed_action
        assert is_structured_remediation({"proposed_action": None}) is False
        # cpu gated by flag
        cpu = mem(field="resources.limits.cpu", current_value="250m", new_value="500m")
        assert is_structured_remediation({"proposed_action": cpu}) is False
        monkeypatch.setattr("remediation.settings.remediation_auto_cpu_enabled", True)
        assert is_structured_remediation({"proposed_action": cpu}) is True


# ── TestProcessRemediationNonStringFilter ─────────────────────────────────────

class TestProcessRemediationNonStringFilter:
    """R6 — process_remediation filtra comandos no-string del diagnóstico."""

    @pytest.mark.asyncio
    async def test_non_string_commands_filtered(self, monkeypatch):
        monkeypatch.setattr("remediation.settings.remediation_enabled", True)
        diagnosis = {**mock_diagnosis_result(), "commands": [None, 42, "kubectl get pods -n prod"]}
        result = await process_remediation(diagnosis)
        assert len(result["command_validations"]) == 1
        assert result["command_validations"][0]["command"] == "kubectl get pods -n prod"

    @pytest.mark.asyncio
    async def test_all_non_string_commands_give_suggest_only(self, monkeypatch):
        monkeypatch.setattr("remediation.settings.remediation_enabled", True)
        diagnosis = {**mock_diagnosis_result(), "commands": [None, 42]}
        result = await process_remediation(diagnosis)
        assert result["action"] == RemediationAction.SUGGEST_ONLY


# ── TestParseCpuToMillicores ──────────────────────────────────────────────────

class TestParseCpuToMillicores:
    """F3 — parser de CPU análogo a parse_memory_to_bytes."""

    def test_millicores(self):
        assert parse_cpu_to_millicores("250m") == 250

    def test_one_core(self):
        assert parse_cpu_to_millicores("1") == 1000

    def test_two_cores(self):
        assert parse_cpu_to_millicores("2") == 2000

    def test_fractional_core(self):
        assert parse_cpu_to_millicores("0.5") == 500

    def test_case_insensitive_suffix(self):
        assert parse_cpu_to_millicores("500M") == parse_cpu_to_millicores("500m")

    def test_invalid_raises(self):
        with pytest.raises(ValueError):
            parse_cpu_to_millicores("garbage")

    def test_empty_raises(self):
        with pytest.raises(ValueError):
            parse_cpu_to_millicores("")


# ── TestDecideActionCpu ───────────────────────────────────────────────────────

class TestDecideActionCpu:
    """F3 slice 1 — la dimensión CPU del motor de decisión.

    Por defecto (auto-cpu flag OFF) HighCPU es escalate-first: deja de morir en
    suggest_only, produce un proposed_action(cpu) y el comando real (`set resources cpu`)
    escala vía regla 4.5. El cap ≤2× (regla 4.6) aplica también a CPU (millicores).
    Los casos flag-ON (F3 slice 2) viven en TestDecideActionCpuAuto.
    """

    def _base_monkeypatch(self, monkeypatch):
        monkeypatch.setattr("remediation.settings.remediation_enabled", True)
        monkeypatch.setattr("remediation.settings.remediation_auto_max_risk", "low")
        monkeypatch.setattr("remediation.settings.remediation_auto_confidence", 0.8)
        monkeypatch.setattr("remediation.settings.remediation_auto_cpu_enabled", False)

    def _cpu_action(self, current, new):
        return {
            "kind": "Deployment", "name": "engine", "namespace": "arturo-llm-test",
            "container": "engine", "field": "resources.limits.cpu",
            "current_value": current, "new_value": new,
        }

    def test_cpu_set_resources_escalates(self, monkeypatch):
        """auto-cpu flag OFF (default): el comando de remediación CPU reinicia pods → ESCALATE (rule 4.5, escalate-first)."""
        self._base_monkeypatch(monkeypatch)
        diagnosis = {
            **mock_diagnosis_auto_remediate(),
            "risk": "low", "confidence": 0.9,
            "proposed_action": self._cpu_action("250m", "500m"),
        }
        validations = validate_commands(
            ["kubectl set resources deployment engine --containers=engine --limits=cpu=500m -n prod"]
        )
        assert decide_action(diagnosis, validations) == RemediationAction.ESCALATE

    def test_cpu_4x_escalates_via_2x_cap(self, monkeypatch):
        """250m → 1000m (>2×) con comando no-restart → lo atrapa la regla 4.6 CPU → ESCALATE."""
        self._base_monkeypatch(monkeypatch)
        diagnosis = {
            **mock_diagnosis_auto_remediate(),
            "risk": "low", "confidence": 0.9,
            "proposed_action": self._cpu_action("250m", "1000m"),
        }
        validations = validate_commands(["kubectl annotate deployment engine note=ok -n prod"])
        assert decide_action(diagnosis, validations) == RemediationAction.ESCALATE

    def test_cpu_2x_within_cap_passes_rule_4_6(self, monkeypatch):
        """250m → 500m (exactamente 2×) con comando no-restart → la 4.6 no bloquea → AUTO_REMEDIATE."""
        self._base_monkeypatch(monkeypatch)
        diagnosis = {
            **mock_diagnosis_auto_remediate(),
            "risk": "low", "confidence": 0.9,
            "proposed_action": self._cpu_action("250m", "500m"),
        }
        validations = validate_commands(["kubectl annotate deployment engine note=ok -n prod"])
        assert decide_action(diagnosis, validations) == RemediationAction.AUTO_REMEDIATE

    def test_cpu_cores_unit_within_cap(self, monkeypatch):
        """current="1" new="2" (cores, 2×) → la 4.6 parsea millicores → AUTO_REMEDIATE."""
        self._base_monkeypatch(monkeypatch)
        diagnosis = {
            **mock_diagnosis_auto_remediate(),
            "risk": "low", "confidence": 0.9,
            "proposed_action": self._cpu_action("1", "2"),
        }
        validations = validate_commands(["kubectl annotate deployment engine note=ok -n prod"])
        assert decide_action(diagnosis, validations) == RemediationAction.AUTO_REMEDIATE

    def test_cpu_unparseable_escalates(self, monkeypatch):
        self._base_monkeypatch(monkeypatch)
        diagnosis = {
            **mock_diagnosis_auto_remediate(),
            "risk": "low", "confidence": 0.9,
            "proposed_action": self._cpu_action("250m", "not-a-value"),
        }
        validations = validate_commands(["kubectl annotate deployment engine note=ok -n prod"])
        assert decide_action(diagnosis, validations) == RemediationAction.ESCALATE

    def test_cpu_missing_current_value_escalates(self, monkeypatch):
        self._base_monkeypatch(monkeypatch)
        diagnosis = {
            **mock_diagnosis_auto_remediate(),
            "risk": "low", "confidence": 0.9,
            "proposed_action": self._cpu_action(None, "500m"),
        }
        validations = validate_commands(["kubectl annotate deployment engine note=ok -n prod"])
        assert decide_action(diagnosis, validations) == RemediationAction.ESCALATE

    def test_cpu_investigation_commands_classify_safe(self):
        """Smoke: los comandos de investigación del runbook HighCPU son SAFE."""
        validations = validate_commands([
            "kubectl top pod -n prod --sort-by=cpu",
            "kubectl top node",
        ])
        assert all(v["safety"] == CommandSafety.SAFE for v in validations)


# ── TestDecideActionCpuAuto ───────────────────────────────────────────────────

class TestDecideActionCpuAuto:
    """F3 slice 2 — auto-remediación de CPU tras el flag remediation_auto_cpu_enabled.

    Con el flag ON, `set resources cpu` de alta confianza y riesgo acotado obtiene la
    excepción de la regla 4.5 (análoga a memoria) y auto-remedia. El cap ≤2× (regla 4.6)
    y los umbrales conf≥0.9 / risk≤medium siguen vigentes por encima.
    """

    _CPU_CMD = "kubectl set resources deployment engine --containers=engine --limits=cpu=500m -n prod"

    def _base_monkeypatch(self, monkeypatch):
        monkeypatch.setattr("remediation.settings.remediation_enabled", True)
        monkeypatch.setattr("remediation.settings.remediation_auto_max_risk", "low")
        monkeypatch.setattr("remediation.settings.remediation_auto_confidence", 0.8)
        monkeypatch.setattr("remediation.settings.remediation_auto_cpu_enabled", True)

    def _cpu_action(self, current, new):
        return {
            "kind": "Deployment", "name": "engine", "namespace": "arturo-llm-test",
            "container": "engine", "field": "resources.limits.cpu",
            "current_value": current, "new_value": new,
        }

    def test_flag_on_cpu_set_resources_auto_remediates(self, monkeypatch):
        """flag ON + set resources cpu + conf≥0.9 + risk≤max + ≤2× → AUTO_REMEDIATE (caso central slice 2)."""
        self._base_monkeypatch(monkeypatch)
        diagnosis = {
            **mock_diagnosis_auto_remediate(),
            "risk": "low", "confidence": 0.9,
            "proposed_action": self._cpu_action("250m", "500m"),
        }
        validations = validate_commands([self._CPU_CMD])
        assert decide_action(diagnosis, validations) == RemediationAction.AUTO_REMEDIATE

    def test_flag_off_cpu_set_resources_escalates(self, monkeypatch):
        """flag OFF (explícito) + mismo caso → ESCALATE: el gate mantiene escalate-first."""
        self._base_monkeypatch(monkeypatch)
        monkeypatch.setattr("remediation.settings.remediation_auto_cpu_enabled", False)
        diagnosis = {
            **mock_diagnosis_auto_remediate(),
            "risk": "low", "confidence": 0.9,
            "proposed_action": self._cpu_action("250m", "500m"),
        }
        validations = validate_commands([self._CPU_CMD])
        assert decide_action(diagnosis, validations) == RemediationAction.ESCALATE

    def test_flag_on_confidence_above_base_floor_auto(self, monkeypatch):
        """flag ON + conf=0.85 ≥ 0.8 base floor → AUTO_REMEDIATE.

        New contract: the structured CPU path drops the old 0.9 exception threshold; confidence
        is gated only by the base rule-6 floor. Was ESCALATE pre-re-sourcing.
        """
        self._base_monkeypatch(monkeypatch)
        diagnosis = {
            **mock_diagnosis_auto_remediate(),
            "risk": "low", "confidence": 0.85,
            "proposed_action": self._cpu_action("250m", "500m"),
        }
        validations = validate_commands([self._CPU_CMD])
        assert decide_action(diagnosis, validations) == RemediationAction.AUTO_REMEDIATE

    def test_flag_on_high_risk_auto_via_engine_bound(self, monkeypatch):
        """flag ON + risk=high but structured CPU raise → rule 5 bypassed → AUTO_REMEDIATE.

        The engine's deterministic bound (≤2× + reversible) supersedes the model's risk
        self-rating. This is the real F4 case (risk=high, conf≈0.84). Was ESCALATE pre-re-sourcing.
        """
        self._base_monkeypatch(monkeypatch)
        diagnosis = {
            **mock_diagnosis_auto_remediate(),
            "risk": "high", "confidence": 0.9,
            "proposed_action": self._cpu_action("250m", "500m"),
        }
        validations = validate_commands([self._CPU_CMD])
        assert decide_action(diagnosis, validations) == RemediationAction.AUTO_REMEDIATE

    def test_flag_on_cpu_exceeds_2x_escalates(self, monkeypatch):
        """flag ON + 250m→1000m: pasa la 4.5 pero la 4.6 (cap ≤2×) lo atrapa → ESCALATE. Defensa en profundidad."""
        self._base_monkeypatch(monkeypatch)
        diagnosis = {
            **mock_diagnosis_auto_remediate(),
            "risk": "low", "confidence": 0.9,
            "proposed_action": self._cpu_action("250m", "1000m"),
        }
        validations = validate_commands(
            ["kubectl set resources deployment engine --containers=engine --limits=cpu=1000m -n prod"]
        )
        assert decide_action(diagnosis, validations) == RemediationAction.ESCALATE

    def test_flag_on_scale_still_escalates(self, monkeypatch):
        """flag ON + proposed_action(cpu) pero el comando es scale → ESCALATE: la excepción es solo set-resources."""
        self._base_monkeypatch(monkeypatch)
        diagnosis = {
            **mock_diagnosis_auto_remediate(),
            "risk": "low", "confidence": 0.9,
            "proposed_action": self._cpu_action("250m", "500m"),
        }
        validations = validate_commands(["kubectl scale deployment engine --replicas=2 -n prod"])
        assert decide_action(diagnosis, validations) == RemediationAction.ESCALATE


# ── TestLimitResource ─────────────────────────────────────────────────────────

class TestLimitResource:
    """F3 slice 1b — _limit_resource deriva la hoja de recurso del field."""

    def test_cpu(self):
        assert _limit_resource("resources.limits.cpu") == "cpu"

    def test_memory(self):
        assert _limit_resource("resources.limits.memory") == "memory"

    def test_unknown_field_defaults_to_memory(self):
        assert _limit_resource("spec.replicas") == "memory"

    def test_empty_defaults_to_memory(self):
        assert _limit_resource("") == "memory"

    def test_none_defaults_to_memory(self):
        assert _limit_resource(None) == "memory"


# ── TestSealProposedAction (v2 Eje A — grounding) ─────────────────────────────

def _snapshot(**kw) -> IncidentSnapshot:
    base = dict(namespace="arturo-x", pod="p")
    base.update(kw)
    return IncidentSnapshot(**base)


class TestSealProposedAction:
    """seal_proposed_action overwrites the LLM's target with cluster truth (or drops it)."""

    def _diagnosis(self) -> dict:
        # name/container deliberately wrong (as the 1.5b hallucinates them)
        return {"proposed_action": {
            "kind": "Deployment", "name": "WRONG-podname", "namespace": "arturo-x",
            "container": "WRONG", "field": "resources.limits.memory",
            "current_value": "999Mi", "new_value": "512Mi",
        }}

    def test_no_snapshot_leaves_llm_values(self):
        d = self._diagnosis()
        seal_proposed_action(d, None)
        assert d["proposed_action"]["name"] == "WRONG-podname"

    def test_gather_failed_leaves_llm_values(self):
        d = self._diagnosis()
        seal_proposed_action(d, _snapshot(gather_ok=False))
        assert d["proposed_action"]["name"] == "WRONG-podname"
        assert d["proposed_action"]["current_value"] == "999Mi"

    def test_seals_identity_and_current_value(self):
        d = self._diagnosis()
        snap = _snapshot(
            gather_ok=True, container="app", workload_name="web", workload_kind="Deployment",
            limits={"app": {"memory": "256Mi"}},
        )
        seal_proposed_action(d, snap)
        pa = d["proposed_action"]
        assert pa["name"] == "web"                # target sealed from cluster
        assert pa["container"] == "app"
        assert pa["current_value"] == "256Mi"     # real cluster value, not "999Mi"
        assert pa["new_value"] == "512Mi"         # LLM direction preserved

    def test_unresolved_workload_drops_action(self):
        d = self._diagnosis()
        # gathered the pod but couldn't confirm a patchable workload (existence gate)
        snap = _snapshot(gather_ok=True, container="app", limits={"app": {"memory": "256Mi"}})
        seal_proposed_action(d, snap)
        assert d["proposed_action"] is None       # → escalate, no auto on a phantom target

    def test_no_proposed_action_is_noop(self):
        d = {"proposed_action": None}
        seal_proposed_action(d, _snapshot(gather_ok=True, workload_name="web"))
        assert d["proposed_action"] is None

    def test_missing_cluster_limit_keeps_llm_current(self):
        d = self._diagnosis()
        snap = _snapshot(
            gather_ok=True, container="app", workload_name="web", limits={"app": {}},
        )
        seal_proposed_action(d, snap)
        assert d["proposed_action"]["current_value"] == "999Mi"  # no cluster value → keep LLM
        assert d["proposed_action"]["name"] == "web"             # identity still sealed

    def test_propagates_match_labels_for_health_check(self):
        d = self._diagnosis()
        snap = _snapshot(
            gather_ok=True, container="app", workload_name="web",
            limits={"app": {"memory": "256Mi"}},
            match_labels={"app": "web", "tier": "backend"},
        )
        seal_proposed_action(d, snap)
        # real selector threads through to capture_pre_patch_value → check_pod_health
        assert d["proposed_action"]["match_labels"] == {"app": "web", "tier": "backend"}

    def test_no_match_labels_leaves_key_absent(self):
        d = self._diagnosis()
        snap = _snapshot(
            gather_ok=True, container="app", workload_name="web",
            limits={"app": {"memory": "256Mi"}},
        )
        seal_proposed_action(d, snap)
        assert "match_labels" not in d["proposed_action"]  # → app={name} fallback applies


# ── TestDeriveConfidence / TestGroundConfidence (v2 Eje A — grounded gating) ──

class TestDeriveConfidence:
    """derive_confidence maps the cluster snapshot to a calibrated confidence, or None."""

    def test_no_snapshot_is_none(self):
        assert derive_confidence(None) is None

    def test_not_gathered_is_none(self):
        assert derive_confidence(_snapshot(gather_ok=False, last_state_reason="OOMKilled")) is None

    def test_no_failure_reason_is_none(self):
        # healthy / CPU throttling → reason=None → don't override, keep model confidence
        assert derive_confidence(_snapshot(gather_ok=True, restart_count=5)) is None

    def test_completed_reason_is_not_a_failure(self):
        assert derive_confidence(_snapshot(gather_ok=True, last_state_reason="Completed", restart_count=3)) is None

    def test_oom_with_restarts_is_full_confidence(self, monkeypatch):
        monkeypatch.setattr("remediation.settings.remediation_auto_min_restarts", 1)
        snap = _snapshot(gather_ok=True, last_state_reason="OOMKilled", restart_count=7)
        assert derive_confidence(snap) == 1.0

    def test_oom_zero_restarts_is_half_confidence(self, monkeypatch):
        monkeypatch.setattr("remediation.settings.remediation_auto_min_restarts", 1)
        snap = _snapshot(gather_ok=True, last_state_reason="OOMKilled", restart_count=0)
        assert derive_confidence(snap) == 0.5

    def test_respects_min_restarts_threshold(self, monkeypatch):
        monkeypatch.setattr("remediation.settings.remediation_auto_min_restarts", 3)
        below = _snapshot(gather_ok=True, last_state_reason="CrashLoopBackOff", restart_count=2)
        at = _snapshot(gather_ok=True, last_state_reason="CrashLoopBackOff", restart_count=3)
        assert derive_confidence(below) == 0.5
        assert derive_confidence(at) == 1.0


class TestGroundConfidence:
    """ground_confidence overrides diagnosis['confidence'] with the cluster signal."""

    def _diagnosis(self, confidence=0.9) -> dict:
        return {**mock_diagnosis_auto_remediate(), "confidence": confidence}

    def test_overrides_and_preserves_model_value(self, monkeypatch):
        monkeypatch.setattr("remediation.settings.remediation_auto_min_restarts", 1)
        d = self._diagnosis(confidence=0.95)
        ground_confidence(d, _snapshot(gather_ok=True, last_state_reason="OOMKilled", restart_count=4))
        assert d["confidence"] == 1.0
        assert d["model_confidence"] == 0.95  # original kept for audit

    def test_none_derivation_leaves_untouched(self):
        d = self._diagnosis(confidence=0.9)
        ground_confidence(d, _snapshot(gather_ok=True, restart_count=2))  # no failure reason
        assert d["confidence"] == 0.9
        assert "model_confidence" not in d

    def test_non_dict_is_noop(self):
        ground_confidence(None, _snapshot(gather_ok=True, last_state_reason="OOMKilled"))  # must not raise

    def test_grounded_low_confidence_blocks_auto(self, monkeypatch):
        """Overconfident model (0.95) but a single OOM event → grounded 0.5 → SUGGEST_ONLY."""
        monkeypatch.setattr("remediation.settings.remediation_enabled", True)
        monkeypatch.setattr("remediation.settings.remediation_auto_confidence", 0.8)
        monkeypatch.setattr("remediation.settings.remediation_auto_min_restarts", 1)
        d = self._diagnosis(confidence=0.95)
        ground_confidence(d, _snapshot(gather_ok=True, last_state_reason="OOMKilled", restart_count=0))
        validations = validate_commands(["kubectl describe pod engine-pod -n prod"])
        assert decide_action(d, validations) == RemediationAction.SUGGEST_ONLY

    def test_grounded_high_confidence_allows_auto(self, monkeypatch):
        """A confirmed recurring OOM → grounded 1.0 → passes the gate."""
        monkeypatch.setattr("remediation.settings.remediation_enabled", True)
        monkeypatch.setattr("remediation.settings.remediation_auto_max_risk", "low")
        monkeypatch.setattr("remediation.settings.remediation_auto_confidence", 0.8)
        monkeypatch.setattr("remediation.settings.remediation_auto_min_restarts", 1)
        d = self._diagnosis(confidence=0.3)  # model underconfident; cluster overrides
        ground_confidence(d, _snapshot(gather_ok=True, last_state_reason="OOMKilled", restart_count=5))
        validations = validate_commands([
            "kubectl describe pod engine-pod -n prod",
            "kubectl annotate deployment engine aiops-checked=true -n prod",
        ])
        assert decide_action(d, validations) == RemediationAction.AUTO_REMEDIATE


# ── TestDoubleLimitValue (v2 — engine-authored new_value) ─────────────────────

class TestDoubleLimitValue:
    """_double_limit_value doubles a k8s quantity preserving its unit."""

    def test_memory_mi(self):
        assert _double_limit_value("64Mi") == "128Mi"

    def test_memory_gi(self):
        assert _double_limit_value("1Gi") == "2Gi"

    def test_cpu_millicores(self):
        assert _double_limit_value("250m") == "500m"

    def test_cpu_fractional_cores(self):
        assert _double_limit_value("0.5") == "1"

    def test_plain_bytes(self):
        assert _double_limit_value("134217728") == "268435456"

    def test_garbage_returns_none(self):
        assert _double_limit_value("a lot") is None


# ── TestSealNewValueSynthesis (v2 — engine authors the value when unusable) ───

class TestSealNewValueSynthesis:
    """With identity+current sealed, an unusable LLM new_value is engine-authored (2×current)."""

    def _diag(self, new_value) -> dict:
        pa = {
            "kind": "Deployment", "name": "WRONG", "namespace": "arturo-x",
            "container": "WRONG", "field": "resources.limits.memory",
            "current_value": "999Mi",
        }
        if new_value is not None:
            pa["new_value"] = new_value
        return {"proposed_action": pa}

    def _snap(self) -> IncidentSnapshot:
        return IncidentSnapshot(
            namespace="arturo-x", pod="p", gather_ok=True, container="app",
            workload_kind="Deployment", workload_name="web",
            limits={"app": {"memory": "256Mi"}},
        )

    def test_synthesizes_when_new_not_above_current(self):
        # LLM anchored on its hallucinated current (999Mi→128Mi "raise" is below real 256Mi)
        d = self._diag("128Mi")
        seal_proposed_action(d, self._snap())
        assert d["proposed_action"]["new_value"] == "512Mi"          # 2× real current
        assert d["proposed_action"]["model_new_value"] == "128Mi"    # audit trail

    def test_synthesizes_when_missing(self):
        d = self._diag(None)
        seal_proposed_action(d, self._snap())
        assert d["proposed_action"]["new_value"] == "512Mi"
        assert d["proposed_action"]["model_new_value"] is None

    def test_synthesizes_when_unparseable(self):
        d = self._diag("mucho")
        seal_proposed_action(d, self._snap())
        assert d["proposed_action"]["new_value"] == "512Mi"

    def test_keeps_sane_llm_value(self):
        d = self._diag("400Mi")  # current < 400Mi ≤ 2×current → model keeps agency
        seal_proposed_action(d, self._snap())
        assert d["proposed_action"]["new_value"] == "400Mi"
        assert "model_new_value" not in d["proposed_action"]

    def test_keeps_overshoot_for_rule_46(self):
        # >2× is NOT clamped — the tutor rule must escalate it, not a silent rewrite
        d = self._diag("2Gi")
        seal_proposed_action(d, self._snap())
        assert d["proposed_action"]["new_value"] == "2Gi"

    def test_no_synthesis_without_cluster_current(self):
        d = self._diag("128Mi")
        snap = self._snap()
        snap.limits = {"app": {}}  # no memory limit observed
        seal_proposed_action(d, snap)
        assert d["proposed_action"]["new_value"] == "128Mi"          # nothing to derive from


# ── TestSealTargetUnresolvedMarker + rule 4.7 (v2 — phantom target escalates) ─

class TestSealTargetUnresolvedMarker:
    """Dropping proposed_action must leave a marker so decide_action escalates."""

    def _diag(self) -> dict:
        return {"proposed_action": {
            "kind": "Deployment", "name": "x", "namespace": "arturo-x",
            "container": "app", "field": "resources.limits.memory",
            "current_value": "256Mi", "new_value": "512Mi",
        }}

    def test_drop_sets_marker(self):
        d = self._diag()
        snap = IncidentSnapshot(
            namespace="arturo-x", pod="p", gather_ok=True, container="app",
            limits={"app": {"memory": "256Mi"}},  # gathered, but workload unresolved
        )
        seal_proposed_action(d, snap)
        assert d["proposed_action"] is None
        assert d["target_unresolved"] is True

    def test_confirmed_workload_does_not_set_marker(self):
        d = self._diag()
        snap = IncidentSnapshot(
            namespace="arturo-x", pod="p", gather_ok=True, container="app",
            workload_kind="Deployment", workload_name="web",
            limits={"app": {"memory": "256Mi"}},
        )
        seal_proposed_action(d, snap)
        assert "target_unresolved" not in d


class TestDecideActionTargetUnresolved:
    """Rule 4.7: an unconfirmed target escalates regardless of the model's risk/confidence."""

    def test_phantom_target_escalates_despite_safe_commands(self, monkeypatch):
        monkeypatch.setattr("remediation.settings.remediation_enabled", True)
        monkeypatch.setattr("remediation.settings.remediation_auto_max_risk", "low")
        monkeypatch.setattr("remediation.settings.remediation_auto_confidence", 0.8)
        # risk=low + confidence 1.0 + SAFE commands: without 4.7 this would AUTO_REMEDIATE
        # the model's investigative leftovers and report a "remediation" that fixed nothing
        diagnosis = {**mock_diagnosis_auto_remediate(), "confidence": 1.0, "target_unresolved": True}
        validations = validate_commands(["kubectl describe pod p -n arturo-x"])
        assert decide_action(diagnosis, validations) == RemediationAction.ESCALATE

    def test_same_diagnosis_without_marker_auto_remediates(self, monkeypatch):
        """Contrast: rule 4.7 (not risk/confidence) is what blocks the phantom case."""
        monkeypatch.setattr("remediation.settings.remediation_enabled", True)
        monkeypatch.setattr("remediation.settings.remediation_auto_max_risk", "low")
        monkeypatch.setattr("remediation.settings.remediation_auto_confidence", 0.8)
        diagnosis = {**mock_diagnosis_auto_remediate(), "confidence": 1.0}
        validations = validate_commands(["kubectl describe pod p -n arturo-x"])
        assert decide_action(diagnosis, validations) == RemediationAction.AUTO_REMEDIATE


# ── TestProcessRemediationStructuredCommand (v2 — single source human/auto) ───

class TestProcessRemediationStructuredCommand:
    """process_remediation exposes the engine synthesis for the escalation/approve path."""

    def _structured_diagnosis(self) -> dict:
        return {
            "diagnosis": "OOMKilled: memory limit too low",
            "commands": ["kubectl describe pod engine-0 -n arturo-prod"],
            "confidence": 1.0,
            "risk": "low",
            "proposed_action": {
                "kind": "Deployment", "name": "engine", "namespace": "arturo-prod",
                "container": "app", "field": "resources.limits.memory",
                "current_value": "256Mi", "new_value": "512Mi",
            },
        }

    @pytest.mark.asyncio
    async def test_escalate_result_carries_structured_command(self, monkeypatch):
        monkeypatch.setattr("remediation.settings.remediation_enabled", True)
        # rag_degraded forces ESCALATE (rule 7.5) on an otherwise auto-eligible diagnosis
        result = await process_remediation(self._structured_diagnosis(), rag_degraded=True)
        assert result["action"] == RemediationAction.ESCALATE
        assert result["structured_command"] == (
            "kubectl set resources deployment engine -n arturo-prod "
            "--containers=app --limits=memory=512Mi"
        )

    @pytest.mark.asyncio
    async def test_non_structured_result_has_none(self, monkeypatch):
        monkeypatch.setattr("remediation.settings.remediation_enabled", True)
        result = await process_remediation(mock_diagnosis_result())
        assert result["structured_command"] is None
