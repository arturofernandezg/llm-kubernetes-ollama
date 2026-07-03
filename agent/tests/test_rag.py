"""
Tests for the RAG module (rag.py).

All tests mock both ChromaDB and Ollama — no external dependencies needed.
"""

import pytest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from rag import (
    build_rag_query,
    strip_pod_hash,
    make_incident_doc_id,
    incident_worth_ingesting,
    INCIDENT_OUTCOME_PENDING,
    INCIDENT_OUTCOME_CURED,
    INCIDENT_OUTCOME_ROLLED_BACK,
    INCIDENTS_RETRIEVAL_FILTER,
    generate_embedding,
    retrieve_context,
    ingest_runbook,
    ingest_incident,
    load_runbooks_from_dir,
    ingest_all_runbooks,
    build_incident_document,
    error_class_for_alertname,
    runbook_filter_for_alert,
    COLLECTION_RUNBOOKS,
    COLLECTION_INCIDENTS,
)
from remediation import RemediationAction


# ── Test data ─────────────────────────────────────────────────────────────────

SAMPLE_LABELS = {
    "alertname": "KubePodOOMKilled",
    "pod": "nginx-7d4f8b-x2k",
    "namespace": "arturo-llm-test",
    "severity": "critical",
    "container": "nginx",
}

SAMPLE_EMBEDDING = [0.1] * 768  # nomic-embed-text produces 768 dims


# ── Mock helpers ──────────────────────────────────────────────────────────────

def mock_ollama_embedding_client():
    """Mock httpx client that returns a fake embedding from Ollama."""
    mock_response = MagicMock()
    mock_response.json.return_value = {"embedding": SAMPLE_EMBEDDING}
    mock_response.raise_for_status = MagicMock()

    client = AsyncMock()
    client.post = AsyncMock(return_value=mock_response)
    return client


def mock_chroma_client(runbook_docs=None, incident_docs=None):
    """
    Mock ChromaDB HttpClient with configurable query results.
    Both collections are mocked with .query() and .count() support.
    """
    def make_query_result(docs):
        if docs is None:
            docs = []
        return {
            "ids": [[d["id"] for d in docs]],
            "documents": [[d["document"] for d in docs]],
            "distances": [[d.get("distance", 0.1) for d in docs]],
            "metadatas": [[d.get("metadata", {}) for d in docs]],
        }

    runbooks_col = MagicMock()
    runbooks_col.query.return_value = make_query_result(runbook_docs or [])
    runbooks_col.count.return_value = len(runbook_docs or [])
    runbooks_col.upsert = MagicMock()

    incidents_col = MagicMock()
    incidents_col.query.return_value = make_query_result(incident_docs or [])
    incidents_col.count.return_value = len(incident_docs or [])
    incidents_col.upsert = MagicMock()

    client = MagicMock()
    client.get_or_create_collection = MagicMock(
        side_effect=lambda name, **kwargs: (
            runbooks_col if name == COLLECTION_RUNBOOKS else incidents_col
        )
    )

    return client, runbooks_col, incidents_col


# ── build_rag_query tests ────────────────────────────────────────────────────

class TestBuildRagQuery:
    def test_full_labels(self):
        query = build_rag_query(SAMPLE_LABELS, "Container was OOM killed")
        assert "KubePodOOMKilled" in query
        assert "nginx-7d4f8b-x2k" in query
        assert "arturo-llm-test" in query
        assert "critical" in query
        assert "Container was OOM killed" in query

    def test_minimal_labels(self):
        query = build_rag_query({"alertname": "TargetDown"}, "")
        assert query == "TargetDown"

    def test_empty_labels(self):
        query = build_rag_query({}, "something went wrong")
        assert "UnknownAlert" in query
        assert "something went wrong" in query

    def test_no_description(self):
        query = build_rag_query({"alertname": "HighCPU", "severity": "warning"}, "")
        assert "HighCPU" in query
        assert "warning" in query

    def test_strips_deployment_pod_hash_in_query(self):
        # Real k8s Deployment pod: 5-char rand suffix triggers the strip.
        labels = {"alertname": "KubePodOOMKilled", "pod": "frontend-8d7c4b-xnq2p"}
        query = build_rag_query(labels, "")
        assert "pod frontend" in query
        assert "8d7c4b" not in query
        assert "xnq2p" not in query


# ── strip_pod_hash tests (R3 — query hygiene) ─────────────────────────────────

class TestStripPodHash:
    def test_deployment_pod(self):
        # <name>-<rs-hash>-<rand>
        assert strip_pod_hash("frontend-8d7c4b-xnq2p") == "frontend"

    def test_deployment_pod_10char_hash(self):
        assert strip_pod_hash("api-6799fc88d8-2k4m9") == "api"

    def test_multi_segment_workload_name_preserved(self):
        assert strip_pod_hash("my-cool-app-8d7c4b-xnq2p") == "my-cool-app"

    def test_statefulset_ordinal(self):
        assert strip_pod_hash("postgres-0") == "postgres"
        assert strip_pod_hash("redis-12") == "redis"

    def test_bare_rand_suffix_with_digit(self):
        # ReplicaSet/DaemonSet pod: single rand suffix containing a digit.
        assert strip_pod_hash("frontend-x2k9p") == "frontend"

    def test_real_word_suffix_not_stripped(self):
        # No digit + not a hash pattern → keep it (avoid eating real names).
        assert strip_pod_hash("my-redis") == "my-redis"

    def test_short_suffix_not_stripped(self):
        # 3-char suffix doesn't match the 5-char rand pattern.
        assert strip_pod_hash("nginx-7d4f8b-x2k") == "nginx-7d4f8b-x2k"

    def test_plain_name_unchanged(self):
        assert strip_pod_hash("frontend") == "frontend"

    def test_empty_string(self):
        assert strip_pod_hash("") == ""

    def test_never_returns_empty(self):
        # Pathological input that would strip to nothing falls back to original.
        assert strip_pod_hash("-0") == "-0"


# ── generate_embedding tests ──────────────────────────────────────────────────

class TestGenerateEmbedding:
    @pytest.mark.asyncio
    async def test_returns_embedding_vector(self):
        client = mock_ollama_embedding_client()
        result = await generate_embedding("test text", client)
        assert result == SAMPLE_EMBEDDING
        assert len(result) == 768

    @pytest.mark.asyncio
    async def test_calls_ollama_with_correct_payload(self):
        client = mock_ollama_embedding_client()
        await generate_embedding("OOMKilled pod nginx", client)
        call_args = client.post.call_args
        payload = call_args.kwargs.get("json") or call_args[1].get("json")
        assert payload["model"] == "nomic-embed-text"
        assert payload["prompt"] == "OOMKilled pod nginx"

    @pytest.mark.asyncio
    async def test_raises_on_ollama_error(self):
        client = AsyncMock()
        mock_resp = MagicMock()
        mock_resp.raise_for_status.side_effect = Exception("Ollama down")
        client.post = AsyncMock(return_value=mock_resp)
        with pytest.raises(Exception, match="Ollama down"):
            await generate_embedding("test", client)

    @pytest.mark.asyncio
    async def test_raises_value_error_when_embedding_key_missing(self):
        """S2: Ollama response without 'embedding' key raises ValueError with context."""
        client = AsyncMock()
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = {"error": "model not found"}
        client.post = AsyncMock(return_value=mock_resp)
        with pytest.raises(ValueError, match="missing 'embedding' key"):
            await generate_embedding("test", client)

    @pytest.mark.asyncio
    async def test_raises_value_error_when_embedding_is_empty_list(self):
        """S2: Ollama response with empty embedding list raises ValueError."""
        client = AsyncMock()
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = {"embedding": []}
        client.post = AsyncMock(return_value=mock_resp)
        with pytest.raises(ValueError, match="missing 'embedding' key"):
            await generate_embedding("test", client)


# ── retrieve_context tests ────────────────────────────────────────────────────

class TestRetrieveContext:
    @pytest.mark.asyncio
    async def test_returns_runbooks_and_incidents(self):
        http_client = mock_ollama_embedding_client()
        chroma, _, _ = mock_chroma_client(
            runbook_docs=[
                {"id": "rb-001", "document": "Fix OOM by increasing limits", "distance": 0.1,
                 "metadata": {"error_class": "OOMKilled"}},
            ],
            incident_docs=[
                {"id": "inc-001", "document": "Past OOM in nginx pod", "distance": 0.2,
                 "metadata": {"error_class": "OOMKilled", "outcome": "resolved"}},
            ],
        )

        result = await retrieve_context(
            "OOMKilled pod nginx",
            http_client,
            chroma_client=chroma,
        )

        assert len(result["runbooks"]) == 1
        assert result["runbooks"][0]["id"] == "rb-001"
        assert len(result["incidents"]) == 1
        assert result["incidents"][0]["id"] == "inc-001"
        assert result["query"] == "OOMKilled pod nginx"

    @pytest.mark.asyncio
    async def test_empty_collections(self):
        http_client = mock_ollama_embedding_client()
        chroma, _, _ = mock_chroma_client()

        result = await retrieve_context(
            "some alert",
            http_client,
            chroma_client=chroma,
        )

        assert result["runbooks"] == []
        assert result["incidents"] == []

    @pytest.mark.asyncio
    async def test_incidents_skipped_when_empty(self):
        """When incidents collection has count=0, query should not be called."""
        http_client = mock_ollama_embedding_client()
        chroma, _, incidents_col = mock_chroma_client(
            runbook_docs=[{"id": "rb-001", "document": "doc", "distance": 0.1, "metadata": {}}],
        )

        await retrieve_context("test", http_client, chroma_client=chroma)
        incidents_col.query.assert_not_called()


# ── R1: metadata-guided retrieval ─────────────────────────────────────────────

class TestErrorClassForAlertname:
    def test_prefixed_prometheus_alert_maps_to_bare_class(self):
        # The mismatch that blocked the naive filter: Prometheus prefixes these.
        assert error_class_for_alertname("KubePodOOMKilled") == "OOMKilled"
        assert error_class_for_alertname("KubePodCrashLoopBackOff") == "CrashLoopBackOff"
        assert error_class_for_alertname("KubePodImagePullBackOff") == "ImagePullBackOff"

    def test_alert_already_named_after_class_is_identity(self):
        assert error_class_for_alertname("HighCPU") == "HighCPU"
        assert error_class_for_alertname("HighMemory") == "HighMemory"
        assert error_class_for_alertname("TargetDown") == "TargetDown"

    def test_unknown_alertname_falls_through_to_identity(self):
        # Unknown class → identity; retrieve_context's empty-result fallback covers it.
        assert error_class_for_alertname("SomethingNew") == "SomethingNew"

    def test_empty_alertname_returns_none(self):
        assert error_class_for_alertname("") is None

    def test_filter_shape(self):
        assert runbook_filter_for_alert({"alertname": "KubePodOOMKilled"}) == {"error_class": "OOMKilled"}
        assert runbook_filter_for_alert({}) is None


class TestMetadataFilteredRetrieval:
    @pytest.mark.asyncio
    async def test_filter_passed_to_runbook_query(self):
        http_client = mock_ollama_embedding_client()
        chroma, runbooks_col, _ = mock_chroma_client(
            runbook_docs=[{"id": "rb-oom", "document": "OOM fix", "distance": 0.1,
                           "metadata": {"error_class": "OOMKilled"}}],
        )

        await retrieve_context(
            "OOMKilled pod nginx", http_client, chroma_client=chroma,
            metadata_filter={"error_class": "OOMKilled"},
        )

        _, kwargs = runbooks_col.query.call_args
        assert kwargs["where"] == {"error_class": "OOMKilled"}

    @pytest.mark.asyncio
    async def test_falls_back_to_semantic_when_filter_matches_nothing(self):
        """A filter that matches no class-tagged runbook retries unfiltered (R1)."""
        http_client = mock_ollama_embedding_client()
        chroma, runbooks_col, _ = mock_chroma_client()
        empty = {"ids": [[]], "documents": [[]], "distances": [[]], "metadatas": [[]]}
        populated = {
            "ids": [["rb-fallback"]],
            "documents": [["semantic match"]],
            "distances": [[0.25]],
            "metadatas": [[{"error_class": "PodEvicted"}]],
        }
        runbooks_col.query.side_effect = [empty, populated]

        result = await retrieve_context(
            "unknown alert", http_client, chroma_client=chroma,
            metadata_filter={"error_class": "DoesNotExist"},
        )

        assert runbooks_col.query.call_count == 2
        # Second (fallback) call drops the filter.
        assert runbooks_col.query.call_args_list[1].kwargs["where"] is None
        assert result["runbooks"][0]["id"] == "rb-fallback"

    @pytest.mark.asyncio
    async def test_no_fallback_when_filter_hits(self):
        """A filter that hits does NOT trigger a second unfiltered query."""
        http_client = mock_ollama_embedding_client()
        chroma, runbooks_col, _ = mock_chroma_client(
            runbook_docs=[{"id": "rb-oom", "document": "OOM fix", "distance": 0.1,
                           "metadata": {"error_class": "OOMKilled"}}],
        )

        await retrieve_context(
            "OOMKilled pod", http_client, chroma_client=chroma,
            metadata_filter={"error_class": "OOMKilled"},
        )

        assert runbooks_col.query.call_count == 1


# ── R2·3: outcome-aware incident retrieval ────────────────────────────────────

class TestIncidentsOutcomeFilter:
    def test_filter_excludes_pending_only(self):
        # Guard the shape: pending is excluded; settled failures (rolled_back /
        # rollback_failed) stay retrievable as labeled negative knowledge.
        assert INCIDENTS_RETRIEVAL_FILTER == {"outcome": {"$ne": INCIDENT_OUTCOME_PENDING}}

    @pytest.mark.asyncio
    async def test_incident_query_carries_outcome_filter(self):
        http_client = mock_ollama_embedding_client()
        chroma, _, incidents_col = mock_chroma_client(
            incident_docs=[{"id": "inc-001", "document": "past OOM cured", "distance": 0.2,
                            "metadata": {"error_class": "OOMKilled", "outcome": "cured"}}],
        )

        await retrieve_context("OOMKilled pod nginx", http_client, chroma_client=chroma)

        _, kwargs = incidents_col.query.call_args
        assert kwargs["where"] == INCIDENTS_RETRIEVAL_FILTER

    @pytest.mark.asyncio
    async def test_all_pending_incidents_yield_empty_context(self):
        """No semantic fallback on incidents: a filter miss returns an empty list."""
        http_client = mock_ollama_embedding_client()
        chroma, _, incidents_col = mock_chroma_client(
            incident_docs=[{"id": "inc-pending", "document": "unverified fix", "distance": 0.1,
                            "metadata": {"outcome": INCIDENT_OUTCOME_PENDING}}],
        )
        # count()=1 (collection has docs) but the where filter matches none.
        incidents_col.query.return_value = {
            "ids": [[]], "documents": [[]], "distances": [[]], "metadatas": [[]],
        }

        result = await retrieve_context("test", http_client, chroma_client=chroma)

        assert result["incidents"] == []
        incidents_col.query.assert_called_once()

    @pytest.mark.asyncio
    async def test_rolled_back_incident_is_still_retrieved(self):
        """Settled failures are exposed (labeled in the prompt), not hidden."""
        http_client = mock_ollama_embedding_client()
        chroma, _, _ = mock_chroma_client(
            incident_docs=[{"id": "inc-rb", "document": "fix reverted", "distance": 0.15,
                            "metadata": {"error_class": "OOMKilled",
                                         "outcome": INCIDENT_OUTCOME_ROLLED_BACK}}],
        )

        result = await retrieve_context("OOMKilled pod", http_client, chroma_client=chroma)

        assert result["incidents"][0]["id"] == "inc-rb"
        assert result["incidents"][0]["metadata"]["outcome"] == INCIDENT_OUTCOME_ROLLED_BACK


# ── ingest_runbook tests ──────────────────────────────────────────────────────

class TestIngestRunbook:
    @pytest.mark.asyncio
    async def test_upserts_with_embedding(self):
        http_client = mock_ollama_embedding_client()
        chroma, runbooks_col, _ = mock_chroma_client()

        await ingest_runbook(
            doc_id="rb-test-001",
            text="Fix CrashLoopBackOff by checking image tag",
            metadata={"error_class": "CrashLoopBackOff", "service": "kubernetes"},
            http_client=http_client,
            chroma_client=chroma,
        )

        runbooks_col.upsert.assert_called_once()
        call_kwargs = runbooks_col.upsert.call_args[1]
        assert call_kwargs["ids"] == ["rb-test-001"]
        assert call_kwargs["embeddings"] == [SAMPLE_EMBEDDING]
        assert "CrashLoopBackOff" in call_kwargs["documents"][0]


# ── load_runbooks_from_dir tests ─────────────────────────────────────────────

class TestLoadRunbooksFromDir:
    def test_loads_valid_yaml_files(self, tmp_path):
        """Valid YAML files are loaded and returned as dicts."""
        (tmp_path / "oom.yaml").write_text(
            "id: rb-001\n"
            "error_class: OOMKilled\n"
            "service: kubernetes\n"
            "severity: critical\n"
            "commands:\n  - kubectl describe pod\n"
            "text: Fix OOM by increasing limits\n"
        )
        result = load_runbooks_from_dir(tmp_path)
        assert len(result) == 1
        assert result[0]["id"] == "rb-001"
        assert result[0]["error_class"] == "OOMKilled"
        assert result[0]["text"] == "Fix OOM by increasing limits"

    def test_skips_file_with_missing_fields(self, tmp_path):
        """Files missing required fields are skipped with a warning."""
        (tmp_path / "bad.yaml").write_text("id: rb-bad\ntext: something\n")
        (tmp_path / "good.yaml").write_text(
            "id: rb-good\nerror_class: HighCPU\nservice: kubernetes\n"
            "severity: warning\ncommands:\n  - kubectl top pod\ntext: Check CPU\n"
        )
        result = load_runbooks_from_dir(tmp_path)
        assert len(result) == 1
        assert result[0]["id"] == "rb-good"

    def test_skips_non_yaml_files(self, tmp_path):
        """Only .yaml files are processed."""
        (tmp_path / "readme.md").write_text("# Not a runbook")
        (tmp_path / "good.yaml").write_text(
            "id: rb-001\nerror_class: X\nservice: k8s\n"
            "severity: warning\ncommands:\n  - cmd\ntext: t\n"
        )
        result = load_runbooks_from_dir(tmp_path)
        assert len(result) == 1

    def test_skips_invalid_yaml_content(self, tmp_path):
        """Files that parse to non-dict (e.g. a list) are skipped."""
        (tmp_path / "list.yaml").write_text("- item1\n- item2\n")
        result = load_runbooks_from_dir(tmp_path)
        assert len(result) == 0

    def test_empty_directory(self, tmp_path):
        """Empty directory returns empty list."""
        result = load_runbooks_from_dir(tmp_path)
        assert result == []

    def test_loads_real_runbooks(self):
        """Smoke test: load the actual runbooks from agent/runbooks/."""
        runbooks_dir = Path(__file__).parent.parent / "runbooks"
        if not runbooks_dir.exists():
            pytest.skip("runbooks/ directory not found")
        result = load_runbooks_from_dir(runbooks_dir)
        assert len(result) == 16
        # Every runbook must have all required fields
        for rb in result:
            assert "id" in rb
            assert "error_class" in rb
            assert "text" in rb
            assert isinstance(rb["commands"], list)
            assert len(rb["commands"]) > 0

    def test_commands_are_lists(self, tmp_path):
        """Commands field must be a list in the YAML."""
        (tmp_path / "rb.yaml").write_text(
            "id: rb-001\nerror_class: X\nservice: k8s\n"
            "severity: warning\ncommands:\n  - kubectl get pods\n  - kubectl logs\ntext: t\n"
        )
        result = load_runbooks_from_dir(tmp_path)
        assert isinstance(result[0]["commands"], list)
        assert len(result[0]["commands"]) == 2


# ── ingest_all_runbooks tests ────────────────────────────────────────────────

class TestIngestAllRunbooks:
    @pytest.mark.asyncio
    async def test_ingests_all_valid_runbooks(self, tmp_path):
        """All valid runbooks in a directory are ingested."""
        for i in range(3):
            (tmp_path / f"rb{i}.yaml").write_text(
                f"id: rb-{i:03d}\nerror_class: Error{i}\nservice: k8s\n"
                f"severity: warning\ncommands:\n  - cmd{i}\ntext: runbook {i}\n"
            )

        http_client = mock_ollama_embedding_client()
        chroma, runbooks_col, _ = mock_chroma_client()

        stats = await ingest_all_runbooks(tmp_path, http_client, chroma_client=chroma)

        assert stats["ingested"] == 3
        assert stats["errors"] == 0
        assert stats["total"] == 3
        assert runbooks_col.upsert.call_count == 3

    @pytest.mark.asyncio
    async def test_metadata_commands_joined_as_string(self, tmp_path):
        """Commands list is joined into a comma-separated string for metadata."""
        (tmp_path / "rb.yaml").write_text(
            "id: rb-001\nerror_class: OOM\nservice: k8s\n"
            "severity: critical\ncommands:\n  - kubectl describe pod\n  - kubectl top pod\n"
            "text: Fix OOM\n"
        )

        http_client = mock_ollama_embedding_client()
        chroma, runbooks_col, _ = mock_chroma_client()

        await ingest_all_runbooks(tmp_path, http_client, chroma_client=chroma)

        call_kwargs = runbooks_col.upsert.call_args[1]
        metadata = call_kwargs["metadatas"][0]
        assert "kubectl describe pod" in metadata["commands"]
        assert "kubectl top pod" in metadata["commands"]

    @pytest.mark.asyncio
    async def test_continues_on_single_failure(self, tmp_path):
        """If one runbook fails to ingest, others still proceed."""
        for i in range(3):
            (tmp_path / f"rb{i}.yaml").write_text(
                f"id: rb-{i:03d}\nerror_class: E{i}\nservice: k8s\n"
                f"severity: warning\ncommands:\n  - cmd\ntext: text {i}\n"
            )

        http_client = mock_ollama_embedding_client()
        chroma, runbooks_col, _ = mock_chroma_client()

        # Make the second call to upsert fail
        call_count = 0
        original_upsert = runbooks_col.upsert

        def failing_upsert(**kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 2:
                raise ConnectionError("ChromaDB down")
            return original_upsert(**kwargs)

        runbooks_col.upsert = MagicMock(side_effect=failing_upsert)

        stats = await ingest_all_runbooks(tmp_path, http_client, chroma_client=chroma)

        assert stats["ingested"] == 2
        assert stats["errors"] == 1
        assert stats["total"] == 3

    @pytest.mark.asyncio
    async def test_empty_directory_returns_zero_stats(self, tmp_path):
        """Empty directory produces zero stats, no errors."""
        http_client = mock_ollama_embedding_client()
        chroma, _, _ = mock_chroma_client()

        stats = await ingest_all_runbooks(tmp_path, http_client, chroma_client=chroma)

        assert stats == {"ingested": 0, "errors": 0, "total": 0}


# ── build_incident_document tests ────────────────────────────────────────────

SAMPLE_ALERT = {
    "labels": {
        "alertname": "KubePodOOMKilled",
        "pod": "nginx-7d4f8b-x2k",
        "namespace": "arturo-llm-test",
        "severity": "critical",
    },
    "annotations": {"description": "Container nginx was OOM killed."},
}

SAMPLE_DIAGNOSIS = {
    "diagnosis": "Pod killed due to OOM. Memory limit 256Mi insufficient.",
    "commands": [
        "kubectl describe pod nginx-7d4f8b-x2k -n arturo-llm-test",
        "kubectl patch deployment nginx -n arturo-llm-test --type=json -p='[...]'",
    ],
    "confidence": 0.85,
    "risk": "low",
    "explanation": "Memory limit exceeded during peak load.",
}

SAMPLE_REMEDIATION = {
    "action": RemediationAction.AUTO_REMEDIATE,
    "command_validations": [],
    "safe_commands": [
        "kubectl describe pod nginx-7d4f8b-x2k -n arturo-llm-test",
        "kubectl patch deployment nginx -n arturo-llm-test --type=json -p='[...]'",
    ],
    "blocked_commands": [],
    "execution_attempted": True,
    "execution_log": "[DRY-RUN] kubectl patch deployment nginx ...",
}


class TestBuildIncidentDocument:
    def test_doc_id_format(self):
        doc_id, _, _ = build_incident_document(SAMPLE_ALERT, SAMPLE_DIAGNOSIS, SAMPLE_REMEDIATION)
        assert doc_id.startswith("incident-KubePodOOMKilled-")
        # suffix must be numeric epoch
        suffix = doc_id.split("incident-KubePodOOMKilled-")[1]
        assert suffix.isdigit()

    def test_text_contains_alert_and_diagnosis(self):
        _, text, _ = build_incident_document(SAMPLE_ALERT, SAMPLE_DIAGNOSIS, SAMPLE_REMEDIATION)
        assert "KubePodOOMKilled" in text
        assert "nginx-7d4f8b-x2k" in text
        assert "arturo-llm-test" in text
        assert "Pod killed due to OOM" in text
        assert "auto_remediate" in text

    def test_metadata_has_required_fields(self):
        _, _, metadata = build_incident_document(SAMPLE_ALERT, SAMPLE_DIAGNOSIS, SAMPLE_REMEDIATION)
        # error_class stores the MAPPED class (runbook vocabulary), not the raw alertname
        assert metadata["error_class"] == "OOMKilled"
        assert metadata["outcome"] == "auto_remediate"
        assert metadata["confidence"] == 0.85
        assert metadata["risk"] == "low"
        assert metadata["timestamp"].isdigit()

    def test_no_commands_gracefully(self):
        diagnosis_no_cmds = {**SAMPLE_DIAGNOSIS, "commands": []}
        _, text, metadata = build_incident_document(SAMPLE_ALERT, diagnosis_no_cmds, SAMPLE_REMEDIATION)
        assert "(none)" in text
        # fix_applied should reflect safe_commands from remediation
        assert metadata["fix_applied"] != ""

    def test_none_remediation(self):
        _, text, metadata = build_incident_document(SAMPLE_ALERT, SAMPLE_DIAGNOSIS, None)
        assert metadata["outcome"] == "no_remediation"
        assert metadata["fix_applied"] == "none"
        assert "none" in text

    # ── R2 (real learning loop): doc_id reuse + outcome override ──────────────
    def test_doc_id_override_is_reused(self):
        # The rollback verdict re-upsert passes the original doc_id so it updates
        # the same ChromaDB document instead of creating a new one.
        fixed = "incident-KubePodOOMKilled-1700000000"
        doc_id, _, _ = build_incident_document(
            SAMPLE_ALERT, SAMPLE_DIAGNOSIS, SAMPLE_REMEDIATION, doc_id=fixed,
        )
        assert doc_id == fixed

    def test_outcome_override_supersedes_action(self):
        # A verdict outcome (cured/rolled_back) overrides the provisional action
        # in both the metadata and the text.
        _, text, metadata = build_incident_document(
            SAMPLE_ALERT, SAMPLE_DIAGNOSIS, SAMPLE_REMEDIATION,
            outcome=INCIDENT_OUTCOME_ROLLED_BACK,
        )
        assert metadata["outcome"] == INCIDENT_OUTCOME_ROLLED_BACK
        assert f"Action: {INCIDENT_OUTCOME_ROLLED_BACK}" in text

    def test_outcome_override_works_without_action_key(self):
        # At verdict time the stored remediation summary has no "action" key
        # (only execution_log + safe_commands); the outcome override must still work.
        summary = {"execution_log": "patched to 512Mi", "safe_commands": ["kubectl patch ..."]}
        _, text, metadata = build_incident_document(
            SAMPLE_ALERT, SAMPLE_DIAGNOSIS, summary, outcome=INCIDENT_OUTCOME_CURED,
        )
        assert metadata["outcome"] == INCIDENT_OUTCOME_CURED
        assert metadata["fix_applied"] == "kubectl patch ..."
        assert "patched to 512Mi" in text

    def test_no_override_preserves_provisional_outcome(self):
        # Default (initial ingest) behaviour is unchanged: outcome = action value.
        _, _, metadata = build_incident_document(SAMPLE_ALERT, SAMPLE_DIAGNOSIS, SAMPLE_REMEDIATION)
        assert metadata["outcome"] == "auto_remediate"

    def test_error_class_identity_for_unknown_alertname(self):
        # An alertname outside the KubePod mapping is stored as-is (identity).
        alert = {**SAMPLE_ALERT, "labels": {**SAMPLE_ALERT["labels"], "alertname": "PodEvicted"}}
        _, _, metadata = build_incident_document(alert, SAMPLE_DIAGNOSIS, SAMPLE_REMEDIATION)
        assert metadata["error_class"] == "PodEvicted"


class TestIncidentWorthIngesting:
    """E4 quality gate: zero/absent confidence (LLM parse failure) is not a precedent."""

    def test_parsed_diagnosis_is_worth_ingesting(self):
        assert incident_worth_ingesting(SAMPLE_DIAGNOSIS) is True

    def test_low_but_nonzero_confidence_is_kept(self):
        assert incident_worth_ingesting({**SAMPLE_DIAGNOSIS, "confidence": 0.1}) is True

    def test_zero_confidence_is_rejected(self):
        # generate_diagnosis sets confidence=0.0 when the LLM output is unparseable.
        assert incident_worth_ingesting({**SAMPLE_DIAGNOSIS, "confidence": 0.0}) is False

    def test_missing_confidence_is_rejected(self):
        assert incident_worth_ingesting({"diagnosis": "x"}) is False

    def test_non_numeric_confidence_is_rejected(self):
        assert incident_worth_ingesting({**SAMPLE_DIAGNOSIS, "confidence": "high"}) is False
        assert incident_worth_ingesting({**SAMPLE_DIAGNOSIS, "confidence": None}) is False


class TestMakeIncidentDocId:
    def test_format_and_prefix(self):
        doc_id = make_incident_doc_id("KubePodOOMKilled")
        assert doc_id.startswith("incident-KubePodOOMKilled-")
        assert doc_id.rsplit("-", 1)[1].isdigit()

    def test_pending_constant_value(self):
        # Guard the wire value the retrieval filter (R2·3) will key on.
        assert INCIDENT_OUTCOME_PENDING == "auto_pending"


# ── ingest_incident tests ─────────────────────────────────────────────────────

class TestIngestIncident:
    @pytest.mark.asyncio
    async def test_upserts_incident_with_embedding(self):
        http_client = mock_ollama_embedding_client()
        chroma, _, incidents_col = mock_chroma_client()

        await ingest_incident(
            doc_id="incident-KubePodOOMKilled-1234567890",
            text="Alert: OOMKilled on nginx\nDiagnosis: Memory limit too low",
            metadata={"error_class": "KubePodOOMKilled", "outcome": "auto_remediate",
                      "fix_applied": "kubectl patch ...", "confidence": 0.85,
                      "risk": "low", "timestamp": "1234567890"},
            http_client=http_client,
            chroma_client=chroma,
        )

        incidents_col.upsert.assert_called_once()
        call_kwargs = incidents_col.upsert.call_args[1]
        assert call_kwargs["ids"] == ["incident-KubePodOOMKilled-1234567890"]
        assert call_kwargs["embeddings"] == [SAMPLE_EMBEDDING]

    @pytest.mark.asyncio
    async def test_uses_incidents_collection(self):
        http_client = mock_ollama_embedding_client()
        chroma, runbooks_col, incidents_col = mock_chroma_client()

        await ingest_incident(
            doc_id="incident-test-001",
            text="test incident",
            metadata={"error_class": "X", "outcome": "escalated", "fix_applied": "none",
                      "confidence": 0.5, "risk": "high", "timestamp": "0"},
            http_client=http_client,
            chroma_client=chroma,
        )

        incidents_col.upsert.assert_called_once()
        runbooks_col.upsert.assert_not_called()

    @pytest.mark.asyncio
    async def test_logs_outcome(self, caplog):
        import logging
        http_client = mock_ollama_embedding_client()
        chroma, _, _ = mock_chroma_client()

        with caplog.at_level(logging.INFO):
            await ingest_incident(
                doc_id="incident-test-002",
                text="some incident text",
                metadata={"error_class": "Y", "outcome": "suggested", "fix_applied": "none",
                          "confidence": 0.6, "risk": "medium", "timestamp": "0"},
                http_client=http_client,
                chroma_client=chroma,
            )

        assert any("incident-test-002" in r.message for r in caplog.records)
