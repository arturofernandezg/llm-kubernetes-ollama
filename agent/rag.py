"""
RAG (Retrieval-Augmented Generation) module for AIOps agent.

Manages two ChromaDB collections:
  - "runbooks": static operational knowledge (K8s procedures, docs)
  - "incidents": semantic memory of past alerts + fixes (feedback loop)

Embeddings are generated in-cluster via Ollama (nomic-embed-text).
"""

import chromadb
import httpx

from config import settings, logger

# ── Constants ─────────────────────────────────────────────────────────────────
COLLECTION_RUNBOOKS = "runbooks"
COLLECTION_INCIDENTS = "incidents"

# How many results to return per collection
DEFAULT_TOP_K = 3


# ── Embedding generation via Ollama ───────────────────────────────────────────

async def generate_embedding(
    text: str,
    http_client: httpx.AsyncClient,
) -> list[float]:
    """
    Generate an embedding vector using Ollama's /api/embeddings endpoint.
    Uses nomic-embed-text (768 dims) loaded in the same Ollama pod.
    """
    response = await http_client.post(
        settings.ollama_embed_url,
        json={"model": settings.ollama_embed_model, "prompt": text},
        timeout=30.0,
    )
    response.raise_for_status()
    return response.json()["embedding"]


# ── ChromaDB client ───────────────────────────────────────────────────────────

def get_chroma_client() -> chromadb.HttpClient:
    """Create a ChromaDB HTTP client pointing to the in-cluster StatefulSet."""
    return chromadb.HttpClient(
        host=settings.chromadb_host,
        port=settings.chromadb_port,
    )


def ensure_collections(client: chromadb.HttpClient) -> tuple:
    """Get or create both collections. Returns (runbooks, incidents)."""
    runbooks = client.get_or_create_collection(
        name=COLLECTION_RUNBOOKS,
        metadata={"hnsw:space": "cosine"},
    )
    incidents = client.get_or_create_collection(
        name=COLLECTION_INCIDENTS,
        metadata={"hnsw:space": "cosine"},
    )
    return runbooks, incidents


# ── Ingestion ─────────────────────────────────────────────────────────────────

async def ingest_runbook(
    doc_id: str,
    text: str,
    metadata: dict,
    http_client: httpx.AsyncClient,
    chroma_client: chromadb.HttpClient | None = None,
) -> None:
    """
    Add a runbook document to the runbooks collection.

    Args:
        doc_id:   Unique ID (e.g. "runbook-oomkilled-001")
        text:     Full runbook text
        metadata: Dict with keys: error_class, service, severity, commands
        http_client: Shared httpx client for Ollama embedding calls
    """
    client = chroma_client or get_chroma_client()
    collection = client.get_or_create_collection(
        name=COLLECTION_RUNBOOKS,
        metadata={"hnsw:space": "cosine"},
    )

    embedding = await generate_embedding(text, http_client)

    collection.upsert(
        ids=[doc_id],
        embeddings=[embedding],
        documents=[text],
        metadatas=[metadata],
    )
    logger.info("Ingested runbook %s (%d chars)", doc_id, len(text))


async def ingest_incident(
    doc_id: str,
    text: str,
    metadata: dict,
    http_client: httpx.AsyncClient,
    chroma_client: chromadb.HttpClient | None = None,
) -> None:
    """
    Persist a resolved incident into the incidents collection (feedback loop).

    Args:
        doc_id:   Unique ID (e.g. "incident-2026-03-25-oom-nginx")
        text:     Full incident description (alert + diagnosis + fix + outcome)
        metadata: Dict with keys: error_class, outcome, fix_applied, confidence, timestamp
        http_client: Shared httpx client for Ollama embedding calls
    """
    client = chroma_client or get_chroma_client()
    collection = client.get_or_create_collection(
        name=COLLECTION_INCIDENTS,
        metadata={"hnsw:space": "cosine"},
    )

    embedding = await generate_embedding(text, http_client)

    collection.upsert(
        ids=[doc_id],
        embeddings=[embedding],
        documents=[text],
        metadatas=[metadata],
    )
    logger.info("Persisted incident %s (outcome=%s)", doc_id, metadata.get("outcome"))


# ── Query construction ────────────────────────────────────────────────────────

def build_rag_query(alert_labels: dict, description: str) -> str:
    """
    Construct an enriched query from alert data.
    NOT just the raw log — includes labels + features for better retrieval.
    """
    alertname = alert_labels.get("alertname", "UnknownAlert")
    pod = alert_labels.get("pod", "")
    namespace = alert_labels.get("namespace", "")
    severity = alert_labels.get("severity", "")
    container = alert_labels.get("container", "")

    parts = [alertname]
    if pod:
        parts.append(f"pod {pod}")
    if namespace:
        parts.append(f"namespace {namespace}")
    if container:
        parts.append(f"container {container}")
    if severity:
        parts.append(f"severity {severity}")
    if description:
        parts.append(description)

    return " ".join(parts)


# ── Retrieval ─────────────────────────────────────────────────────────────────

async def retrieve_context(
    query_text: str,
    http_client: httpx.AsyncClient,
    chroma_client: chromadb.HttpClient | None = None,
    top_k_runbooks: int = DEFAULT_TOP_K,
    top_k_incidents: int = 2,
    metadata_filter: dict | None = None,
) -> dict:
    """
    Query both ChromaDB collections and return relevant context.

    Returns:
        {
            "runbooks": [{"id": ..., "document": ..., "distance": ..., "metadata": ...}, ...],
            "incidents": [{"id": ..., "document": ..., "distance": ..., "metadata": ...}, ...],
            "query": str,
        }
    """
    client = chroma_client or get_chroma_client()
    runbooks_col, incidents_col = ensure_collections(client)

    query_embedding = await generate_embedding(query_text, http_client)

    results = {"query": query_text, "runbooks": [], "incidents": []}

    # Query runbooks
    runbook_results = runbooks_col.query(
        query_embeddings=[query_embedding],
        n_results=top_k_runbooks,
        where=metadata_filter,
        include=["documents", "metadatas", "distances"],
    )
    for i in range(len(runbook_results["ids"][0])):
        results["runbooks"].append({
            "id": runbook_results["ids"][0][i],
            "document": runbook_results["documents"][0][i],
            "distance": runbook_results["distances"][0][i],
            "metadata": runbook_results["metadatas"][0][i],
        })

    # Query incidents (may be empty initially)
    if incidents_col.count() > 0:
        incident_results = incidents_col.query(
            query_embeddings=[query_embedding],
            n_results=min(top_k_incidents, incidents_col.count()),
            include=["documents", "metadatas", "distances"],
        )
        for i in range(len(incident_results["ids"][0])):
            results["incidents"].append({
                "id": incident_results["ids"][0][i],
                "document": incident_results["documents"][0][i],
                "distance": incident_results["distances"][0][i],
                "metadata": incident_results["metadatas"][0][i],
            })

    logger.info(
        "RAG retrieval: %d runbooks, %d incidents for query: %.80s...",
        len(results["runbooks"]),
        len(results["incidents"]),
        query_text,
    )

    return results
