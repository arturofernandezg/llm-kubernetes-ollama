"""
Tests for the RAG module (rag.py).

All tests mock both ChromaDB and Ollama — no external dependencies needed.
"""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from rag import (
    build_rag_query,
    generate_embedding,
    retrieve_context,
    ingest_runbook,
    COLLECTION_RUNBOOKS,
    COLLECTION_INCIDENTS,
)


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
