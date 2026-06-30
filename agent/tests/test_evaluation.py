"""
Tests para el módulo de evaluación.

Cubre:
  - test_dataset_loads: los JSON son válidos contra AlertmanagerPayload
  - test_ground_truth_runbooks_exist: expected_runbook referencia YAML real en agent/runbooks/
  - test_eval_retrieval_smoke: cálculo de precision@K con rag.retrieve_context mockeado
  - test_eval_safety_smoke: validate_commands sobre diagnósticos de ejemplo
  - test_eval_actionability_is_actionable: función is_actionable
"""

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from schemas import AlertmanagerPayload
from evaluation.eval_retrieval import RUNBOOK_ERROR_CLASSES, load_datasets, load_ground_truth
from evaluation.eval_safety import main as eval_safety_main
from evaluation.eval_actionability import is_actionable

_DATASETS_DIR = Path(__file__).parent.parent / "evaluation" / "datasets"
_RUNBOOKS_DIR = Path(__file__).parent.parent / "runbooks"
_GROUND_TRUTH_FILE = Path(__file__).parent.parent / "evaluation" / "ground_truth" / "expected_runbooks.json"


# ── Dataset integrity ─────────────────────────────────────────────────────────

class TestDatasetLoads:
    def test_all_dataset_files_exist(self):
        assert (_DATASETS_DIR / "alerts_oom.json").exists()
        assert (_DATASETS_DIR / "alerts_crashloop.json").exists()
        assert (_DATASETS_DIR / "alerts_imagepull.json").exists()
        assert (_DATASETS_DIR / "alerts_highcpu.json").exists()
        assert (_DATASETS_DIR / "alerts_highmemory.json").exists()

    def test_oom_dataset_count(self):
        with open(_DATASETS_DIR / "alerts_oom.json") as f:
            data = json.load(f)
        assert len(data) == 5

    def test_crashloop_dataset_count(self):
        with open(_DATASETS_DIR / "alerts_crashloop.json") as f:
            data = json.load(f)
        assert len(data) == 3

    def test_imagepull_dataset_count(self):
        with open(_DATASETS_DIR / "alerts_imagepull.json") as f:
            data = json.load(f)
        assert len(data) == 2

    def test_highcpu_dataset_count(self):
        with open(_DATASETS_DIR / "alerts_highcpu.json") as f:
            data = json.load(f)
        assert len(data) == 3

    def test_highmemory_dataset_count(self):
        with open(_DATASETS_DIR / "alerts_highmemory.json") as f:
            data = json.load(f)
        assert len(data) == 2

    def test_all_payloads_valid_against_schema(self):
        alerts = load_datasets()
        assert len(alerts) == 15  # 5 oom + 3 crashloop + 2 imagepull + 3 highcpu + 2 highmemory
        for entry in alerts:
            payload = AlertmanagerPayload(**entry["payload"])
            assert len(payload.alerts) >= 1
            assert payload.alerts[0].startsAt is not None

    def test_all_entries_have_required_fields(self):
        alerts = load_datasets()
        for entry in alerts:
            assert "id" in entry
            assert "payload" in entry
            assert "expected_runbook" in entry

    def test_alert_ids_unique(self):
        alerts = load_datasets()
        ids = [a["id"] for a in alerts]
        assert len(ids) == len(set(ids))


# ── Ground truth integrity ────────────────────────────────────────────────────

class TestGroundTruthRunbooksExist:
    def test_ground_truth_file_exists(self):
        assert _GROUND_TRUTH_FILE.exists()

    def test_ground_truth_has_all_alert_ids(self):
        alerts = load_datasets()
        gt = load_ground_truth()
        for alert in alerts:
            assert alert["id"] in gt, f"Alert {alert['id']} missing from ground truth"

    def test_expected_runbooks_reference_real_yaml(self):
        gt = load_ground_truth()
        for alert_id, runbook_stem in gt.items():
            yaml_path = _RUNBOOKS_DIR / f"{runbook_stem}.yaml"
            assert yaml_path.exists(), (
                f"Alert {alert_id}: expected runbook '{runbook_stem}.yaml' not found in agent/runbooks/"
            )

    def test_runbook_error_classes_map_covers_dataset(self):
        gt = load_ground_truth()
        for alert_id, runbook_stem in gt.items():
            assert runbook_stem in RUNBOOK_ERROR_CLASSES, (
                f"Alert {alert_id}: runbook '{runbook_stem}' not in RUNBOOK_ERROR_CLASSES mapping"
            )


# ── eval_retrieval smoke test ─────────────────────────────────────────────────

class TestEvalRetrievalSmoke:
    async def test_precision_calculation_all_hits(self):
        """Mock retrieve_context to always return correct runbook; expect precision@1=1.0."""
        from evaluation.eval_retrieval import evaluate_alert

        alert_entry = {
            "id": "oom-001",
            "expected_runbook": "oomkilled",
            "payload": {
                "alerts": [{
                    "status": "firing",
                    "labels": {"alertname": "KubePodOOMKilled", "pod": "nginx-x", "namespace": "prod"},
                    "annotations": {"description": "OOMKilled"},
                    "startsAt": "2026-04-30T10:00:00Z",
                }]
            },
        }

        mock_context = {
            "query": "KubePodOOMKilled",
            "runbooks": [
                {"id": "runbook-oomkilled-001", "metadata": {"error_class": "OOMKilled"}, "distance": 0.05},
                {"id": "runbook-crashloopbackoff-001", "metadata": {"error_class": "CrashLoopBackOff"}, "distance": 0.3},
                {"id": "runbook-highmemory-001", "metadata": {"error_class": "HighMemory"}, "distance": 0.4},
            ],
            "incidents": [],
        }

        with patch("evaluation.eval_retrieval.retrieve_context", new=AsyncMock(return_value=mock_context)):
            mock_client = MagicMock()
            result = await evaluate_alert(alert_entry, "OOMKilled", mock_client, top_k=3)

        assert result["hit_at_1"] is True
        assert result["hit_at_3"] is True

    async def test_precision_calculation_miss_at_1(self):
        """Mock returns wrong runbook at rank 1 but correct at rank 2."""
        from evaluation.eval_retrieval import evaluate_alert

        alert_entry = {
            "id": "oom-001",
            "expected_runbook": "oomkilled",
            "payload": {
                "alerts": [{
                    "status": "firing",
                    "labels": {"alertname": "KubePodOOMKilled", "pod": "x", "namespace": "prod"},
                    "annotations": {"description": "OOMKilled"},
                    "startsAt": "2026-04-30T10:00:00Z",
                }]
            },
        }

        mock_context = {
            "query": "KubePodOOMKilled",
            "runbooks": [
                {"id": "runbook-highmemory-001", "metadata": {"error_class": "HighMemory"}, "distance": 0.1},
                {"id": "runbook-oomkilled-001", "metadata": {"error_class": "OOMKilled"}, "distance": 0.15},
            ],
            "incidents": [],
        }

        with patch("evaluation.eval_retrieval.retrieve_context", new=AsyncMock(return_value=mock_context)):
            result = await evaluate_alert(alert_entry, "OOMKilled", MagicMock(), top_k=3)

        assert result["hit_at_1"] is False
        assert result["hit_at_3"] is True


# ── eval_safety smoke test ────────────────────────────────────────────────────

class TestEvalSafetySmoke:
    def test_safety_clean_diagnosis(self, tmp_path):
        """Cached diagnosis with only safe commands → blocked=0."""
        cache_data = {
            "oom-001_rag": {
                "alert_id": "oom-001",
                "mode": "rag",
                "commands": [
                    "kubectl describe pod nginx -n prod",
                    "kubectl top pod -n prod",
                    "kubectl get events -n prod --sort-by=.lastTimestamp",
                ],
            }
        }
        cache_file = tmp_path / "cached_diagnoses_2026-04-30.json"
        cache_file.write_text(json.dumps(cache_data))

        result = eval_safety_main(cache_file=cache_file)

        assert result["blocked"] == 0
        assert result["safety_pass"] is True
        assert result["total_commands"] == 3

    def test_safety_detects_blocked_command(self, tmp_path):
        """Cached diagnosis with a kubectl drain → blocked=1."""
        cache_data = {
            "test-001_rag": {
                "alert_id": "test-001",
                "mode": "rag",
                "commands": [
                    "kubectl get nodes",
                    "kubectl drain node-1 --ignore-daemonsets",
                ],
            }
        }
        cache_file = tmp_path / "cached_diagnoses_test.json"
        cache_file.write_text(json.dumps(cache_data))

        result = eval_safety_main(cache_file=cache_file)

        assert result["blocked"] == 1
        assert result["safety_pass"] is False


# ── is_actionable unit tests ──────────────────────────────────────────────────

class TestIsActionable:
    def test_valid_kubectl_command(self):
        assert is_actionable("kubectl describe pod nginx -n prod") is True

    def test_kubectl_get(self):
        assert is_actionable("kubectl get pods -n staging") is True

    def test_empty_string(self):
        assert is_actionable("") is False

    def test_non_kubectl_command(self):
        assert is_actionable("systemctl restart nginx") is False

    def test_kubectl_with_leading_spaces(self):
        assert is_actionable("  kubectl top pod -n prod") is True

    def test_partial_kubectl_word(self):
        assert is_actionable("# kubectl describe pod x") is False
