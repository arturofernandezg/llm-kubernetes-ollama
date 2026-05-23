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
    implies_pod_restart,
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
        assert result["execution_attempted"] is False
