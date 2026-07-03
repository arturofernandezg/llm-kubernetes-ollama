from __future__ import annotations

"""
RAG (Retrieval-Augmented Generation) module for AIOps agent.

Manages two ChromaDB collections:
  - "runbooks": static operational knowledge (K8s procedures, docs)
  - "incidents": semantic memory of past alerts + fixes (feedback loop)

Embeddings are generated in-cluster via Ollama (nomic-embed-text).
"""

import re
import time
from pathlib import Path
from typing import Optional

import chromadb
import httpx
import yaml

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
    data = response.json()
    embedding = data.get("embedding")
    if not embedding:
        raise ValueError(
            f"Ollama embedding response missing 'embedding' key "
            f"(model={settings.ollama_embed_model!r}, keys={list(data.keys())})"
        )
    return embedding


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
    chroma_client: Optional[chromadb.HttpClient] = None,
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


# ── Batch ingestion from YAML files ─────────────────────────────────────────

RUNBOOK_REQUIRED_FIELDS = {"id", "error_class", "service", "severity", "commands", "text"}


def load_runbooks_from_dir(runbooks_dir: str | Path) -> list[dict]:
    """
    Load and validate all YAML runbook files from a directory.

    Each YAML file must contain: id, error_class, service, severity, commands, text.
    Returns a list of parsed dicts. Skips files with missing fields (logs a warning).
    """
    runbooks_dir = Path(runbooks_dir)
    loaded = []

    for yaml_file in sorted(runbooks_dir.glob("*.yaml")):
        with open(yaml_file, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)

        if not isinstance(data, dict):
            logger.warning("Skipping %s: not a valid YAML mapping", yaml_file.name)
            continue

        missing = RUNBOOK_REQUIRED_FIELDS - set(data.keys())
        if missing:
            logger.warning("Skipping %s: missing fields %s", yaml_file.name, missing)
            continue

        loaded.append(data)

    logger.info("Loaded %d runbooks from %s", len(loaded), runbooks_dir)
    return loaded


async def ingest_all_runbooks(
    runbooks_dir: str | Path,
    http_client: httpx.AsyncClient,
    chroma_client: Optional[chromadb.HttpClient] = None,
) -> dict:
    """
    Load all YAML runbooks from a directory and ingest them into ChromaDB.

    Returns:
        {"ingested": int, "errors": int, "total": int}
    """
    runbooks = load_runbooks_from_dir(runbooks_dir)
    stats = {"ingested": 0, "errors": 0, "total": len(runbooks)}

    for rb in runbooks:
        try:
            metadata = {
                "error_class": rb["error_class"],
                "service": rb["service"],
                "severity": rb["severity"],
                "commands": ", ".join(rb["commands"]),
            }
            await ingest_runbook(
                doc_id=rb["id"],
                text=rb["text"],
                metadata=metadata,
                http_client=http_client,
                chroma_client=chroma_client,
            )
            stats["ingested"] += 1
        except Exception as exc:
            logger.error("Failed to ingest runbook %s: %s", rb.get("id"), exc)
            stats["errors"] += 1

    logger.info(
        "Batch ingestion complete: %d ingested, %d errors, %d total",
        stats["ingested"], stats["errors"], stats["total"],
    )
    return stats


async def ingest_incident(
    doc_id: str,
    text: str,
    metadata: dict,
    http_client: httpx.AsyncClient,
    chroma_client: Optional[chromadb.HttpClient] = None,
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


# ── Incident document builder (feedback loop) ─────────────────────────────────

# Final incident outcomes recorded once the rollback verdict is known (R2 —
# real learning loop). At diagnosis time the incident is provisional
# (INCIDENT_OUTCOME_PENDING); the verdict re-upserts the same doc with the true
# result so retrieval never cites a rolled-back fix as a good precedent.
INCIDENT_OUTCOME_PENDING = "auto_pending"       # patch applied, verdict not yet known
INCIDENT_OUTCOME_CURED = "cured"                # pod healthy after the wait window
INCIDENT_OUTCOME_ROLLED_BACK = "rolled_back"    # still failing → reverted
INCIDENT_OUTCOME_ROLLBACK_FAILED = "rollback_failed"  # unhealthy AND revert failed


def make_incident_doc_id(alertname: str) -> str:
    """Build an incident document id. Shared between the initial ingest and the
    rollback verdict re-upsert (R2) so both write the same ChromaDB document."""
    return f"incident-{alertname}-{int(time.time())}"


def incident_worth_ingesting(diagnosis: dict) -> bool:
    """Quality gate for the feedback-loop ingest (E4 — RAG contamination).

    A diagnosis the LLM failed to produce (unparseable output → confidence 0.0)
    or that the model itself scored at zero confidence carries no precedent
    value — archiving it would feed garbage back into future retrievals.
    Deterministic, no tunable threshold: only zero/absent/invalid confidence is
    rejected; a low-but-parsed confidence is still legitimate history.
    """
    try:
        return float(diagnosis.get("confidence", 0.0)) > 0.0
    except (TypeError, ValueError):
        return False


def build_incident_document(
    alert: dict,
    diagnosis: dict,
    remediation: dict | None,
    *,
    doc_id: str | None = None,
    outcome: str | None = None,
) -> tuple[str, str, dict]:
    """
    Build (doc_id, text, metadata) for incident persistence in ChromaDB.

    Args:
        alert:       {"labels": {...}, "annotations": {...}}
        diagnosis:   Output of generate_diagnosis() — keys: diagnosis, commands,
                     confidence, risk, explanation
        remediation: Output of process_remediation() — keys: action, safe_commands,
                     blocked_commands, execution_log. None if remediation was skipped.

    Returns:
        doc_id:   "incident-{alertname}-{epoch}"
        text:     Human-readable summary (alert + diagnosis + action + execution)
        metadata: ChromaDB filterable primitives
    """
    labels = alert.get("labels", {})
    annotations = alert.get("annotations", {})
    alertname = labels.get("alertname", "UnknownAlert")
    pod = labels.get("pod", "unknown-pod")
    namespace = labels.get("namespace", "unknown-ns")
    severity = labels.get("severity", "unknown")
    description = annotations.get("description", "")

    now = int(time.time())
    # doc_id is reused on the rollback-verdict re-upsert (R2); generate a fresh one
    # only for the initial ingest.
    doc_id = doc_id or make_incident_doc_id(alertname)

    # Determine the provisional outcome from the remediation action.
    if remediation is None:
        outcome_value = "no_remediation"
        action_str = "none"
        execution_str = ""
        fix_applied = "none"
    else:
        action = remediation.get("action")
        outcome_value = action.value if action is not None else "unknown"
        action_str = outcome_value
        execution_str = remediation.get("execution_log", "")
        safe_cmds = remediation.get("safe_commands", [])
        fix_applied = ", ".join(safe_cmds) if safe_cmds else "none"

    # R2: an explicit verdict (cured / rolled_back / ...) supersedes the provisional
    # action recorded at diagnosis time. The re-upsert passes it; the initial ingest
    # leaves it None so behaviour is unchanged.
    if outcome is not None:
        outcome_value = outcome
        action_str = outcome

    confidence_pct = int(diagnosis["confidence"] * 100)
    commands = diagnosis.get("commands", [])
    commands_str = "\n".join(f"  {c}" for c in commands) if commands else "  (none)"

    text_parts = [
        f"Alert: {alertname} on pod {pod} in namespace {namespace}",
        f"Severity: {severity}",
    ]
    if description:
        text_parts.append(f"Description: {description}")
    text_parts += [
        f"Diagnosis: {diagnosis['diagnosis']}",
        f"Risk: {diagnosis['risk']} | Confidence: {confidence_pct}%",
        f"Commands:\n{commands_str}",
        f"Action: {action_str}",
    ]
    if execution_str:
        text_parts.append(f"Execution:\n{execution_str}")

    text = "\n".join(text_parts)

    metadata = {
        # Store the mapped class (not the raw Prometheus alertname) so incidents
        # share the runbooks' error_class vocabulary (R1) — enables class-filtered
        # incident retrieval later and correct class headers in the prompt.
        "error_class": error_class_for_alertname(alertname) or alertname,
        "outcome": outcome_value,
        "fix_applied": fix_applied,
        "confidence": diagnosis["confidence"],
        "risk": diagnosis["risk"],
        "timestamp": str(now),
    }

    return doc_id, text, metadata


# ── Query construction ────────────────────────────────────────────────────────

# Kubernetes suffixes controller names to make pod names unique:
#   Deployment            → <name>-<replicaset-hash>-<rand>  (frontend-8d7c4b-xnq2p)
#   ReplicaSet/DaemonSet  → <name>-<rand>                    (frontend-xnq2p)
#   StatefulSet           → <name>-<ordinal>                 (postgres-0)
# The hash/rand/ordinal carries no semantic signal for retrieval and drags the
# embedding toward wrong near-neighbours on sparse descriptions (R3 — query
# hygiene). Reduce the pod name to its workload name for the query only; the
# incident document keeps the real pod name for forensics.
_POD_HASH_RE = re.compile(
    r"(?:"
    r"-[a-z0-9]{5,10}-[a-z0-9]{5}"      # Deployment pod: <rs-hash>-<rand>
    r"|-\d+"                            # StatefulSet ordinal: <name>-0
    r"|-(?=[a-z0-9]*\d)[a-z0-9]{5}"     # bare RS/DS rand suffix (has a digit)
    r")$"
)


def strip_pod_hash(pod: str) -> str:
    """Reduce a pod name to its workload name by stripping k8s-generated suffixes.

    Conservative: only strips the standard Deployment/StatefulSet/DaemonSet
    patterns, and never returns empty (falls back to the original pod name).
    """
    if not pod:
        return pod
    return _POD_HASH_RE.sub("", pod) or pod


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
        parts.append(f"pod {strip_pod_hash(pod)}")
    if namespace:
        parts.append(f"namespace {namespace}")
    if container:
        parts.append(f"container {container}")
    if severity:
        parts.append(f"severity {severity}")
    if description:
        parts.append(description)

    return " ".join(parts)


# ── Metadata-guided retrieval (R1) ────────────────────────────────────────────

# Prometheus alertname → runbook error_class. Prometheus prefixes the pod-level
# Kube* alerts, while the runbooks store the bare Kubernetes reason as error_class;
# HighCPU/HighMemory/TargetDown already match 1:1. This deterministic map lets us
# filter the runbook query by class (the alert already NAMES the class) instead of
# leaning on the embedding to disambiguate near-neighbour classes — the source of
# the residual p@1 misses (see docs/10). Unknown alertnames fall through to a
# semantic query (identity below + empty-result fallback in retrieve_context).
ALERTNAME_TO_ERROR_CLASS: dict[str, str] = {
    "KubePodOOMKilled": "OOMKilled",
    "KubePodCrashLoopBackOff": "CrashLoopBackOff",
    "KubePodImagePullBackOff": "ImagePullBackOff",
    "HighMemory": "HighMemory",
    "HighCPU": "HighCPU",
    "TargetDown": "TargetDown",
}


def error_class_for_alertname(alertname: str) -> Optional[str]:
    """
    Map a Prometheus alertname to a runbook error_class (deterministic).

    Returns the explicit mapping when known, else the alertname itself (identity —
    many alerts already name their class, e.g. HighCPU). Returns None only for an
    empty alertname. A class that has no matching runbook is harmless: the filtered
    query in retrieve_context yields nothing and falls back to a semantic search.
    """
    if not alertname:
        return None
    return ALERTNAME_TO_ERROR_CLASS.get(alertname, alertname)


def runbook_filter_for_alert(alert_labels: dict) -> Optional[dict]:
    """Build a ChromaDB `where` filter on error_class from the alert's alertname."""
    error_class = error_class_for_alertname(alert_labels.get("alertname", ""))
    return {"error_class": error_class} if error_class else None


# ── Retrieval ─────────────────────────────────────────────────────────────────

# R2·3: never cite an in-flight (unverified) fix as precedent — an auto_pending
# incident becomes cured/rolled_back only when the rollback verdict lands.
# Settled failures (rolled_back / rollback_failed) stay retrievable ON PURPOSE:
# they are negative knowledge ("this fix did not cure"), surfaced with their
# outcome label in the prompt (diagnosis.format_context_docs) so the model
# avoids repeating a fix that already failed.
INCIDENTS_RETRIEVAL_FILTER = {"outcome": {"$ne": INCIDENT_OUTCOME_PENDING}}

async def retrieve_context(
    query_text: str,
    http_client: httpx.AsyncClient,
    chroma_client: Optional[chromadb.HttpClient] = None,
    top_k_runbooks: int = DEFAULT_TOP_K,
    top_k_incidents: int = 2,
    metadata_filter: Optional[dict] = None,
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

    def _query_runbooks(where):
        return runbooks_col.query(
            query_embeddings=[query_embedding],
            n_results=top_k_runbooks,
            where=where,
            include=["documents", "metadatas", "distances"],
        )

    # Query runbooks. Two-stage (R1): try the metadata filter first; if it matches
    # no class-tagged runbook (unknown/mismatched class), fall back to an unfiltered
    # semantic query so we never return an empty context just because the filter missed.
    runbook_results = _query_runbooks(metadata_filter)
    if metadata_filter is not None and not runbook_results["ids"][0]:
        logger.info(
            "RAG metadata filter %s matched no runbooks; falling back to semantic query",
            metadata_filter,
        )
        runbook_results = _query_runbooks(None)
    for i in range(len(runbook_results["ids"][0])):
        results["runbooks"].append({
            "id": runbook_results["ids"][0][i],
            "document": runbook_results["documents"][0][i],
            "distance": runbook_results["distances"][0][i],
            "metadata": runbook_results["metadatas"][0][i],
        })

    # Query incidents (may be empty initially). Pending (unverified) incidents are
    # excluded — no semantic fallback here: an empty incident context is a normal
    # state, and falling back would defeat the filter (R2·3).
    if incidents_col.count() > 0:
        incident_results = incidents_col.query(
            query_embeddings=[query_embedding],
            n_results=min(top_k_incidents, incidents_col.count()),
            where=INCIDENTS_RETRIEVAL_FILTER,
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
