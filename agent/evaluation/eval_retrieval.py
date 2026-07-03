"""
Evaluation: Retrieval Precision (Paso 2)

Mide si ChromaDB devuelve el runbook correcto en top-k para cada alerta del dataset.
Requiere ChromaDB y Ollama accesibles (via port-forward o en cluster).

Uso:
    cd agent
    CHROMADB_HOST=localhost CHROMADB_PORT=8001 \
    OLLAMA_EMBED_URL=http://localhost:11434/api/embeddings \
    python -m evaluation.eval_retrieval
"""

import asyncio
import json
import os
from datetime import date
from pathlib import Path

import httpx

from config import logger
from rag import build_rag_query, retrieve_context, runbook_filter_for_alert

# ── Paths ──────────────────────────────────────────────────────────────────────
_EVAL_DIR = Path(__file__).parent
DATASETS_DIR = _EVAL_DIR / "datasets"
GROUND_TRUTH_FILE = _EVAL_DIR / "ground_truth" / "expected_runbooks.json"
RESULTS_DIR = _EVAL_DIR.parent / "evaluation_results"

# ── Mapping: filename stem → ChromaDB metadata.error_class ───────────────────
RUNBOOK_ERROR_CLASSES: dict[str, str] = {
    "oomkilled": "OOMKilled",
    "crashloopbackoff": "CrashLoopBackOff",
    "imagepullbackoff": "ImagePullBackOff",
    "highmemory": "HighMemory",
    "highcpu": "HighCPU",
    "targetdown": "TargetDown",
    "podnotready": "PodNotReady",
    "nodenotready": "NodeNotReady",
    "podevicted": "PodEvicted",
    "diskpressure": "DiskPressure",
    "jobfailed": "JobFailed",
    "persistentvolumefillingup": "PersistentVolumeFillingUp",
}


def load_datasets() -> list[dict]:
    alerts = []
    for dataset_file in sorted(DATASETS_DIR.glob("alerts_*.json")):
        with open(dataset_file) as f:
            alerts.extend(json.load(f))
    return alerts


def load_ground_truth() -> dict[str, str]:
    with open(GROUND_TRUTH_FILE) as f:
        return json.load(f)


def _first_alert_fields(payload: dict) -> tuple[dict, dict, str]:
    """Extract labels, annotations, status from the first alert in a payload."""
    first = payload["alerts"][0]
    return first["labels"], first.get("annotations", {}), first["status"]


async def evaluate_alert(
    alert_entry: dict,
    expected_error_class: str,
    http_client: httpx.AsyncClient,
    top_k: int = 3,
    use_filter: bool = True,
) -> dict:
    payload = alert_entry["payload"]
    labels, annotations, _ = _first_alert_fields(payload)
    description = annotations.get("description", "")

    query_text = build_rag_query(labels, description)
    # R1: metadata-guided retrieval. Set EVAL_NO_FILTER=1 to reproduce the semantic-only
    # baseline (docs/10) and A/B the gain.
    metadata_filter = runbook_filter_for_alert(labels) if use_filter else None
    context = await retrieve_context(
        query_text=query_text,
        http_client=http_client,
        top_k_runbooks=top_k,
        metadata_filter=metadata_filter,
    )
    retrieved = context.get("runbooks", [])

    # Hits: retrieved error_class matches expected (case-insensitive)
    hit_at_1 = (
        len(retrieved) >= 1
        and retrieved[0]["metadata"].get("error_class", "").lower()
        == expected_error_class.lower()
    )
    hit_at_k = any(
        r["metadata"].get("error_class", "").lower() == expected_error_class.lower()
        for r in retrieved
    )

    return {
        "alert_id": alert_entry["id"],
        "expected_runbook": alert_entry["expected_runbook"],
        "expected_error_class": expected_error_class,
        "query": query_text,
        "retrieved": [
            {
                "rank": i + 1,
                "id": r["id"],
                "error_class": r["metadata"].get("error_class"),
                "distance": round(r["distance"], 4),
            }
            for i, r in enumerate(retrieved)
        ],
        "hit_at_1": hit_at_1,
        f"hit_at_{top_k}": hit_at_k,
    }


async def main(top_k: int = 3, use_filter: bool = True) -> dict:
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)

    alerts = load_datasets()
    ground_truth = load_ground_truth()

    print(f"Metadata filter (R1): {'ON' if use_filter else 'OFF (semantic baseline)'}")
    results = []
    async with httpx.AsyncClient(timeout=60.0) as client:
        for alert in alerts:
            alert_id = alert["id"]
            expected_runbook = ground_truth.get(alert_id, alert.get("expected_runbook", ""))
            expected_error_class = RUNBOOK_ERROR_CLASSES.get(expected_runbook, expected_runbook)

            print(f"  Evaluating {alert_id} (expected: {expected_error_class})...")
            try:
                result = await evaluate_alert(alert, expected_error_class, client, top_k, use_filter)
            except Exception as exc:
                logger.error("Failed to evaluate alert %s: %s", alert_id, exc)
                result = {
                    "alert_id": alert_id,
                    "expected_runbook": expected_runbook,
                    "expected_error_class": expected_error_class,
                    "error": str(exc),
                    "hit_at_1": False,
                    f"hit_at_{top_k}": False,
                }
            results.append(result)

    total = len(results)
    hits_at_1 = sum(1 for r in results if r.get("hit_at_1"))
    hits_at_k = sum(1 for r in results if r.get(f"hit_at_{top_k}"))

    output = {
        "date": str(date.today()),
        "total": total,
        "top_k": top_k,
        "metadata_filter": use_filter,
        "precision_at_1": round(hits_at_1 / total, 3) if total else 0,
        f"precision_at_{top_k}": round(hits_at_k / total, 3) if total else 0,
        "hits_at_1": hits_at_1,
        f"hits_at_{top_k}": hits_at_k,
        "details": results,
    }

    out_file = RESULTS_DIR / f"retrieval_{date.today()}.json"
    with open(out_file, "w") as f:
        json.dump(output, f, indent=2)

    print(f"\n=== Retrieval Precision ===")
    print(f"precision@1 : {output['precision_at_1']:.1%}  ({hits_at_1}/{total})")
    print(f"precision@{top_k} : {output[f'precision_at_{top_k}']:.1%}  ({hits_at_k}/{total})")
    print(f"Results saved to {out_file}")
    return output


if __name__ == "__main__":
    use_filter = os.getenv("EVAL_NO_FILTER", "").lower() not in ("1", "true", "yes")
    asyncio.run(main(use_filter=use_filter))
