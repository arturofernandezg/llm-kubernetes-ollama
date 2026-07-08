"""
Tests for the R4 feedback-loop evaluation (evaluation/eval_feedback.py).

Covers the offline logic (no cluster): fixture integrity, the pre-registered
Layer-B outcome extraction, the Layer-A class-match retrieval against a mocked
ChromaDB collection, and the two-stage runbook fallback. The LLM/ChromaDB HTTP
paths are exercised in cluster by Jay; here they are mocked.
"""

from unittest.mock import MagicMock

import pytest

from evaluation.eval_feedback import (
    _FAILURE_KEYWORDS,
    _query_incident_classes,
    _query_incidents_full,
    _query_runbooks_full,
    extract_outcome,
    load_fixture,
)
from evaluation.eval_retrieval import load_datasets


# ── Fixture integrity ─────────────────────────────────────────────────────────

class TestFixture:
    def test_loads_and_is_disclosed_synthetic(self):
        fx = load_fixture()
        assert fx["_meta"]["synthetic"] is True
        assert "SYNTHETIC" in fx["_meta"]["disclosure"]

    def test_retrieval_seed_spans_five_classes(self):
        fx = load_fixture()
        classes = {i["error_class"] for i in fx["retrieval_seed"]}
        assert classes == {
            "OOMKilled", "CrashLoopBackOff", "ImagePullBackOff", "HighCPU", "HighMemory",
        }

    def test_retrieval_seed_entries_are_wellformed(self):
        fx = load_fixture()
        for inc in fx["retrieval_seed"]:
            assert inc["id"] and inc["error_class"] and inc["text"]
            assert inc["outcome"] in ("cured", "rolled_back")

    def test_behavioral_has_three_ablation_arms(self):
        fx = load_fixture()
        arms = {a["name"]: a["outcome"] for a in fx["behavioral"]["arms"]}
        assert arms == {"control": None, "negative": "rolled_back", "positive_ablation": "cured"}

    def test_behavioral_stimulus_is_a_real_dataset_alert(self):
        fx = load_fixture()
        ids = {a["id"] for a in load_datasets()}
        assert fx["behavioral"]["stimulus_alert_id"] in ids

    def test_ablation_arms_share_identical_base_text(self):
        # The only difference between negative and positive must be the outcome word.
        fx = load_fixture()
        tmpl = fx["behavioral"]["base_incident"]["text_template"]
        neg = tmpl.format(outcome="rolled_back")
        pos = tmpl.format(outcome="cured")
        assert neg.replace("rolled_back", "X") == pos.replace("cured", "X")


# ── Layer B — outcome extraction ──────────────────────────────────────────────

class TestExtractOutcome:
    def test_memory_bump_detected(self):
        diag = {
            "proposed_action": {"field": "resources.limits.memory", "current_value": "64Mi", "new_value": "128Mi"},
            "confidence": 0.8, "risk": "medium", "diagnosis": "OOM", "explanation": "raise the limit",
        }
        out = extract_outcome(diag)
        assert out["proposes_action"] is True
        assert out["proposes_memory_bump"] is True
        assert out["new_value"] == "128Mi"

    def test_no_action_is_not_a_bump(self):
        diag = {"proposed_action": None, "confidence": 0.3, "risk": "high", "diagnosis": "?", "explanation": ""}
        out = extract_outcome(diag)
        assert out["proposes_action"] is False
        assert out["proposes_memory_bump"] is False

    def test_cpu_action_is_not_a_memory_bump(self):
        diag = {
            "proposed_action": {"field": "resources.limits.cpu", "current_value": "250m", "new_value": "500m"},
            "confidence": 0.7, "risk": "medium", "diagnosis": "cpu", "explanation": "",
        }
        assert extract_outcome(diag)["proposes_memory_bump"] is False

    def test_mentions_prior_failure_from_keywords(self):
        diag = {
            "proposed_action": None, "confidence": 0.4, "risk": "high",
            "diagnosis": "This fix was rolled back before", "explanation": "do not repeat it",
        }
        assert extract_outcome(diag)["mentions_prior_failure"] is True

    def test_clean_explanation_does_not_flag_failure(self):
        diag = {
            "proposed_action": None, "confidence": 0.9, "risk": "low",
            "diagnosis": "container OOMKilled", "explanation": "raise memory to 128Mi",
        }
        assert extract_outcome(diag)["mentions_prior_failure"] is False

    def test_failure_keywords_are_lowercase(self):
        # extract_outcome lowercases the text; keywords must be lowercase to match.
        assert all(k == k.lower() for k in _FAILURE_KEYWORDS)


# ── Layer A — incident class retrieval (mocked ChromaDB) ──────────────────────

def _mock_collection(count, classes):
    coll = MagicMock()
    coll.count.return_value = count
    coll.query.return_value = {
        "ids": [[f"seed-{i}" for i in range(len(classes))]],
        "metadatas": [[{"error_class": c} for c in classes]],
        "distances": [[0.2 for _ in classes]],
        "documents": [[f"doc {c}" for c in classes]],
    }
    return coll

class TestIncidentRetrieval:
    def test_empty_collection_returns_no_classes(self):
        coll = _mock_collection(0, [])
        assert _query_incident_classes(coll, [0.1] * 3, top_k=2) == []

    def test_returns_retrieved_classes_in_order(self):
        coll = _mock_collection(3, ["OOMKilled", "HighMemory"])
        assert _query_incident_classes(coll, [0.1] * 3, top_k=2) == ["OOMKilled", "HighMemory"]

    def test_full_incidents_shape_matches_diagnosis_contract(self):
        coll = _mock_collection(2, ["OOMKilled", "CrashLoopBackOff"])
        docs = _query_incidents_full(coll, [0.1] * 3, top_k=2)
        assert docs[0]["metadata"]["error_class"] == "OOMKilled"
        assert set(docs[0]) == {"id", "document", "distance", "metadata"}


# ── Runbook two-stage fallback ────────────────────────────────────────────────

class TestRunbookTwoStage:
    def test_falls_back_to_unfiltered_when_filter_misses(self):
        client = MagicMock()
        coll = MagicMock()
        client.get_or_create_collection.return_value = coll
        empty = {"ids": [[]], "documents": [[]], "metadatas": [[]], "distances": [[]]}
        full = {
            "ids": [["rb-oom"]], "documents": [["runbook"]],
            "metadatas": [[{"error_class": "OOMKilled"}]], "distances": [[0.2]],
        }
        coll.query.side_effect = [empty, full]  # filtered miss → unfiltered hit
        out = _query_runbooks_full(client, [0.1] * 3, {"error_class": "OOMKilled"}, top_k=3)
        assert len(out) == 1 and out[0]["metadata"]["error_class"] == "OOMKilled"
        assert coll.query.call_count == 2
