"""
Evaluation: Feedback-loop gain (R4).

Measures what the RAG learning loop (R2/R2·3) actually delivers — which is NOT
runbook retrieval precision (that lives in eval_retrieval.py and is unaffected by
the incidents collection). Two independent layers:

  Layer A — incident-retrieval precision (embedding-only, fast, N = dataset).
      Does the incidents collection surface a SAME error_class past incident in
      top-k for a new alert? Empty collection (0%) vs seeded fixture (measured %).
      Claim: "past incidents are surfaced to the right future alert." Nothing more.

  Layer B — negative-knowledge behavioral ablation (LLM in the loop, 3 arms).
      Pre-registered causal test: does a `rolled_back` precedent suppress re-proposal
      of the failed fix, while an identical `cured` precedent does not? The cured-vs-
      rolled_back ablation isolates the outcome LABEL as the causal variable.
      Outcome is measured at the DIAGNOSIS level (proposed_action / confidence /
      mention of the prior failure), NOT the motor verdict: the eval has no real pod,
      so seal/decide_action would ESCALATE uniformly (target_unresolved) and wash out
      the signal. In prod, a dropped proposed_action or a sub-threshold confidence is
      exactly what routes to ESCALATE.

Isolation: uses a DISPOSABLE ChromaDB collection (incidents_eval_<epoch>); the prod
`incidents` collection is never touched. Teardown runs in `finally` (horno-nocturno
lesson: teardown is the last gate of a run, not a later chore).

Pre-registered protocol + hypothesis (including the possible null result — that the
1.5b ignores negative knowledge and a larger model would be needed) live in docs/10.

Requires ChromaDB + Ollama reachable (port-forward or in-cluster). Run by Jay.

Uso (desde agent/):
    CHROMADB_HOST=localhost CHROMADB_PORT=8001 \
    OLLAMA_URL=http://localhost:11434/api/generate \
    OLLAMA_EMBED_URL=http://localhost:11434/api/embeddings \
    OLLAMA_MODEL=qwen2.5:1.5b \
    python -m evaluation.eval_feedback --reps 3
"""

import argparse
import asyncio
import json
import time
from datetime import date
from pathlib import Path

import httpx

from config import logger, settings
from diagnosis import generate_diagnosis
from rag import (
    COLLECTION_RUNBOOKS,
    DEFAULT_TOP_K,
    INCIDENTS_RETRIEVAL_FILTER,
    build_rag_query,
    generate_embedding,
    get_chroma_client,
    runbook_filter_for_alert,
)
from evaluation.eval_retrieval import (
    RUNBOOK_ERROR_CLASSES,
    _first_alert_fields,
    load_datasets,
    load_ground_truth,
)

_EVAL_DIR = Path(__file__).parent
FIXTURE_FILE = _EVAL_DIR / "fixtures" / "incidents_seed.json"
RESULTS_DIR = _EVAL_DIR.parent / "evaluation_results"

# Keywords that signal the model is reacting to a prior failed fix (Layer B secondary).
_FAILURE_KEYWORDS = (
    "rolled back",
    "rollback",
    "reverted",
    "failed fix",
    "did not cure",
    "didn't cure",
    "previous fix",
    "prior fix",
    "already tried",
    "do not repeat",
    "was reverted",
)


def load_fixture() -> dict:
    with open(FIXTURE_FILE) as f:
        return json.load(f)


# ── ChromaDB disposable-collection helpers (offline tooling: blocking calls are OK) ──

def _reset_collection(client, name: str):
    """Return a fresh, empty collection under `name`, dropping any prior contents."""
    try:
        client.delete_collection(name=name)
    except Exception:
        pass  # did not exist yet
    return client.get_or_create_collection(name=name, metadata={"hnsw:space": "cosine"})


async def _seed_collection(collection, incidents: list[dict], http_client: httpx.AsyncClient) -> None:
    for inc in incidents:
        embedding = await generate_embedding(inc["text"], http_client)
        collection.upsert(
            ids=[inc["id"]],
            embeddings=[embedding],
            documents=[inc["text"]],
            metadatas=[{
                "error_class": inc["error_class"],
                "outcome": inc["outcome"],
                "fix_applied": inc.get("fix_applied", ""),
                "synthetic": True,
            }],
        )


def _query_incident_classes(collection, query_embedding: list[float], top_k: int) -> list[str]:
    """Layer A: return the error_class of each incident retrieved in top-k (settled only)."""
    if collection.count() == 0:
        return []
    res = collection.query(
        query_embeddings=[query_embedding],
        n_results=min(top_k, collection.count()),
        where=INCIDENTS_RETRIEVAL_FILTER,
        include=["metadatas", "distances"],
    )
    return [m.get("error_class", "") for m in res["metadatas"][0]]


def _query_incidents_full(collection, query_embedding: list[float], top_k: int) -> list[dict]:
    """Layer B: return incident docs in the shape generate_diagnosis/format_context_docs expects."""
    if collection.count() == 0:
        return []
    res = collection.query(
        query_embeddings=[query_embedding],
        n_results=min(top_k, collection.count()),
        where=INCIDENTS_RETRIEVAL_FILTER,
        include=["documents", "metadatas", "distances"],
    )
    return [
        {
            "id": res["ids"][0][i],
            "document": res["documents"][0][i],
            "distance": res["distances"][0][i],
            "metadata": res["metadatas"][0][i],
        }
        for i in range(len(res["ids"][0]))
    ]


def _query_runbooks_full(client, query_embedding: list[float], metadata_filter, top_k: int) -> list[dict]:
    """Two-stage R1 runbook retrieval against the REAL runbooks collection (prod parity)."""
    collection = client.get_or_create_collection(
        name=COLLECTION_RUNBOOKS, metadata={"hnsw:space": "cosine"}
    )

    def _q(where):
        return collection.query(
            query_embeddings=[query_embedding],
            n_results=top_k,
            where=where,
            include=["documents", "metadatas", "distances"],
        )

    res = _q(metadata_filter)
    if metadata_filter is not None and not res["ids"][0]:
        res = _q(None)
    return [
        {
            "id": res["ids"][0][i],
            "document": res["documents"][0][i],
            "distance": res["distances"][0][i],
            "metadata": res["metadatas"][0][i],
        }
        for i in range(len(res["ids"][0]))
    ]


def extract_outcome(diagnosis: dict) -> dict:
    """Reduce a diagnosis to the pre-registered Layer-B outcome fields."""
    pa = diagnosis.get("proposed_action")
    field = (pa or {}).get("field", "") or ""
    text = f"{diagnosis.get('diagnosis', '')} {diagnosis.get('explanation', '')}".lower()
    return {
        "proposes_action": pa is not None,
        "field": field,
        "proposes_memory_bump": pa is not None and "memory" in field.lower(),
        "current_value": (pa or {}).get("current_value"),
        "new_value": (pa or {}).get("new_value"),
        "confidence": diagnosis.get("confidence"),
        "risk": diagnosis.get("risk"),
        "mentions_prior_failure": any(k in text for k in _FAILURE_KEYWORDS),
        "commands": diagnosis.get("commands", []),
        "rag_sources": diagnosis.get("rag_sources", []),
    }


# ── Layer A — incident-retrieval precision ────────────────────────────────────

async def run_retrieval_layer(client, http_client, coll, seed: list[dict], top_k: int = 2) -> dict:
    alerts = load_datasets()
    ground_truth = load_ground_truth()

    async def _measure(label: str) -> dict:
        details, hits_at_1, hits_at_k = [], 0, 0
        for alert in alerts:
            labels, annotations, _ = _first_alert_fields(alert["payload"])
            expected_runbook = ground_truth.get(alert["id"], alert.get("expected_runbook", ""))
            expected_class = RUNBOOK_ERROR_CLASSES.get(expected_runbook, expected_runbook)
            query_text = build_rag_query(labels, annotations.get("description", ""))
            embedding = await generate_embedding(query_text, http_client)
            classes = _query_incident_classes(coll, embedding, top_k)
            hit1 = bool(classes) and classes[0].lower() == expected_class.lower()
            hitk = any(c.lower() == expected_class.lower() for c in classes)
            hits_at_1 += hit1
            hits_at_k += hitk
            details.append({
                "alert_id": alert["id"],
                "expected_class": expected_class,
                "retrieved_classes": classes,
                "hit_at_1": hit1,
                f"hit_at_{top_k}": hitk,
            })
        total = len(alerts)
        return {
            "label": label,
            "total": total,
            "precision_at_1": round(hits_at_1 / total, 3) if total else 0,
            f"precision_at_{top_k}": round(hits_at_k / total, 3) if total else 0,
            "hits_at_1": hits_at_1,
            f"hits_at_{top_k}": hits_at_k,
            "details": details,
        }

    # Baseline: the collection is empty at this point (measured, not asserted).
    empty = await _measure("empty")
    await _seed_collection(coll, seed, http_client)
    seeded = await _measure("seeded")
    return {"top_k": top_k, "empty": empty, "seeded": seeded}


# ── Layer B — negative-knowledge behavioral ablation ──────────────────────────

async def run_behavioral_layer(
    client, http_client, coll, behavioral: dict, reps: int,
    top_k_runbooks: int = DEFAULT_TOP_K, top_k_incidents: int = 2,
) -> dict:
    alerts = {a["id"]: a for a in load_datasets()}
    stimulus = alerts[behavioral["stimulus_alert_id"]]
    labels, annotations, status = _first_alert_fields(stimulus["payload"])
    query_text = build_rag_query(labels, annotations.get("description", ""))
    runbook_filter = runbook_filter_for_alert(labels)
    base = behavioral["base_incident"]

    arms_out = []
    for arm in behavioral["arms"]:
        coll_arm = _reset_collection(client, coll.name)
        if arm["outcome"] is not None:
            incident = {
                "id": base["id"],
                "error_class": base["error_class"],
                "outcome": arm["outcome"],
                "fix_applied": base["fix_applied"],
                "text": base["text_template"].format(outcome=arm["outcome"]),
            }
            await _seed_collection(coll_arm, [incident], http_client)

        reps_out = []
        for rep in range(reps):
            embedding = await generate_embedding(query_text, http_client)
            runbooks = _query_runbooks_full(client, embedding, runbook_filter, top_k_runbooks)
            incidents = _query_incidents_full(coll_arm, embedding, top_k_incidents)
            rag_context = {"query": query_text, "runbooks": runbooks, "incidents": incidents}
            diagnosis = await generate_diagnosis(labels, annotations, status, rag_context, http_client)
            reps_out.append(extract_outcome(diagnosis))

        arms_out.append({
            "name": arm["name"],
            "seeded_outcome": arm["outcome"],
            "prediction": arm.get("prediction", ""),
            "reps": reps_out,
        })

    return {
        "stimulus_alert_id": behavioral["stimulus_alert_id"],
        "reps_per_arm": reps,
        "arms": arms_out,
    }


# ── Orchestration ─────────────────────────────────────────────────────────────

async def main(reps: int = 3, layers: str = "both") -> dict:
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    fixture = load_fixture()
    client = get_chroma_client()
    coll_name = f"incidents_eval_{int(time.time())}"
    output = {
        "date": str(date.today()),
        "model": settings.ollama_model,
        "eval_collection": coll_name,
        "fixture_disclosure": fixture["_meta"]["disclosure"],
    }

    try:
        coll = _reset_collection(client, coll_name)
        async with httpx.AsyncClient(timeout=300.0) as http_client:
            if layers in ("a", "both"):
                print("Layer A — incident-retrieval precision (empty vs seeded)...")
                output["layer_a_retrieval"] = await run_retrieval_layer(
                    client, http_client, coll, fixture["retrieval_seed"]
                )
            if layers in ("b", "both"):
                print(f"Layer B — negative-knowledge ablation ({reps} reps/arm)...")
                output["layer_b_behavioral"] = await run_behavioral_layer(
                    client, http_client, coll, fixture["behavioral"], reps
                )
    finally:
        # Teardown is the last gate of the run (horno-nocturno lesson).
        try:
            client.delete_collection(name=coll_name)
            logger.info("Torn down disposable eval collection %s", coll_name)
        except Exception as exc:
            logger.warning("Could not delete eval collection %s: %s", coll_name, exc)

    out_file = RESULTS_DIR / f"feedback_{date.today()}.json"
    with open(out_file, "w") as f:
        json.dump(output, f, indent=2)

    _print_summary(output)
    print(f"\nResults saved to {out_file}")
    return output


def _print_summary(output: dict) -> None:
    a = output.get("layer_a_retrieval")
    if a:
        print("\n=== Layer A — incident-retrieval precision ===")
        tk = a["top_k"]
        print(f"empty  : p@1 {a['empty']['precision_at_1']:.1%}  p@{tk} {a['empty'][f'precision_at_{tk}']:.1%}")
        print(f"seeded : p@1 {a['seeded']['precision_at_1']:.1%}  p@{tk} {a['seeded'][f'precision_at_{tk}']:.1%}")
    b = output.get("layer_b_behavioral")
    if b:
        print("\n=== Layer B — negative-knowledge ablation ===")
        print(f"stimulus alert: {b['stimulus_alert_id']}  ({b['reps_per_arm']} reps/arm)")
        for arm in b["arms"]:
            bumps = sum(r["proposes_memory_bump"] for r in arm["reps"])
            confs = [r["confidence"] for r in arm["reps"] if r["confidence"] is not None]
            avg_conf = round(sum(confs) / len(confs), 2) if confs else None
            mentions = sum(r["mentions_prior_failure"] for r in arm["reps"])
            print(
                f"  {arm['name']:<18} outcome={str(arm['seeded_outcome']):<12} "
                f"memory_bump={bumps}/{len(arm['reps'])}  avg_conf={avg_conf}  "
                f"mentions_failure={mentions}/{len(arm['reps'])}"
            )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="R4 feedback-loop gain evaluation")
    parser.add_argument("--reps", type=int, default=3, help="Layer B repetitions per arm (determinism check)")
    parser.add_argument("--layers", choices=["a", "b", "both"], default="both")
    args = parser.parse_args()
    asyncio.run(main(reps=args.reps, layers=args.layers))
