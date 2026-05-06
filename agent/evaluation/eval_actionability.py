"""
Evaluation: Actionability Rate (Paso 3)

Para cada alerta del dataset, corre el pipeline completo (RAG + LLM) y evalúa si
los comandos generados son ejecutables kubectl válidos.
Cachea diagnósticos en evaluation_results/cached_diagnoses_FECHA.json para reusar en eval_safety.

Uso:
    cd agent
    CHROMADB_HOST=localhost CHROMADB_PORT=8001 \
    OLLAMA_URL=http://localhost:11434/api/generate \
    OLLAMA_EMBED_URL=http://localhost:11434/api/embeddings \
    python -m evaluation.eval_actionability [--mode rag|zero_shot|both] [--use-cache]

Flags:
    --mode rag         Solo RAG (default). ~3 min/alerta.
    --mode zero_shot   Solo sin contexto RAG (baseline).
    --mode both        Ambos modos. Guardado en cache compartido.
    --use-cache        Reutilizar cache del día si existe.
"""

import argparse
import asyncio
import json
from datetime import date
from pathlib import Path

import httpx

from config import logger
from diagnosis import generate_diagnosis
from rag import build_rag_query, retrieve_context

# ── Paths ──────────────────────────────────────────────────────────────────────
_EVAL_DIR = Path(__file__).parent
DATASETS_DIR = _EVAL_DIR / "datasets"
GROUND_TRUTH_FILE = _EVAL_DIR / "ground_truth" / "expected_runbooks.json"
RESULTS_DIR = _EVAL_DIR.parent / "evaluation_results"


def load_datasets() -> list[dict]:
    alerts = []
    for dataset_file in sorted(DATASETS_DIR.glob("alerts_*.json")):
        with open(dataset_file) as f:
            alerts.extend(json.load(f))
    return alerts


def is_actionable(command: str) -> bool:
    """True if the command is a non-empty kubectl invocation."""
    cmd = command.strip()
    return bool(cmd) and cmd.startswith("kubectl ")


async def run_pipeline(
    alert_entry: dict,
    http_client: httpx.AsyncClient,
    zero_shot: bool = False,
) -> dict:
    """Run RAG + LLM for a single alert. Returns structured result."""
    payload = alert_entry["payload"]
    first = payload["alerts"][0]
    labels = first["labels"]
    annotations = first.get("annotations", {})
    status = first["status"]

    if zero_shot:
        rag_context = {"query": "", "runbooks": [], "incidents": []}
    else:
        query = build_rag_query(labels, annotations.get("description", ""))
        rag_context = await retrieve_context(query, http_client, top_k_runbooks=3)

    diagnosis = await generate_diagnosis(labels, annotations, status, rag_context, http_client)
    commands = diagnosis.get("commands", [])

    actionable = [c for c in commands if is_actionable(c)]
    non_actionable = [c for c in commands if not is_actionable(c)]

    return {
        "alert_id": alert_entry["id"],
        "expected_runbook": alert_entry["expected_runbook"],
        "mode": "zero_shot" if zero_shot else "rag",
        "commands": commands,
        "actionable_commands": actionable,
        "non_actionable_commands": non_actionable,
        "actionability_rate": round(len(actionable) / len(commands), 3) if commands else 0.0,
        "confidence": diagnosis.get("confidence", 0.0),
        "risk": diagnosis.get("risk", "high"),
        "rag_sources_count": len(diagnosis.get("rag_sources", [])),
        "duration_ms": diagnosis.get("duration_ms", 0),
        "diagnosis_text": diagnosis.get("diagnosis", ""),
    }


async def main(mode: str = "rag", use_cache: bool = False) -> dict:
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    alerts = load_datasets()

    cache_file = RESULTS_DIR / f"cached_diagnoses_{date.today()}.json"
    cached: dict = {}

    if use_cache and cache_file.exists():
        with open(cache_file) as f:
            cached = json.load(f)
        print(f"Loaded {len(cached)} cached diagnoses from {cache_file}")

    modes_to_run = ["rag", "zero_shot"] if mode == "both" else [mode]

    async with httpx.AsyncClient(timeout=300.0) as client:
        for alert in alerts:
            for m in modes_to_run:
                key = f"{alert['id']}_{m}"
                if key in cached:
                    print(f"  [cache] {key}")
                    continue
                print(f"  Running {key} (LLM ~3min)...")
                try:
                    result = await run_pipeline(alert, client, zero_shot=(m == "zero_shot"))
                    cached[key] = result
                except Exception as exc:
                    logger.error("Pipeline failed for %s (%s): %s", alert["id"], m, exc)
                    cached[key] = {
                        "alert_id": alert["id"],
                        "mode": m,
                        "error": str(exc),
                        "commands": [],
                        "actionable_commands": [],
                        "non_actionable_commands": [],
                        "actionability_rate": 0.0,
                        "confidence": 0.0,
                    }
                # Save after each alert to allow safe interruption
                with open(cache_file, "w") as f:
                    json.dump(cached, f, indent=2, default=str)

    # Compute aggregate metrics per mode
    def aggregate(mode_key: str) -> dict:
        entries = [v for v in cached.values() if v.get("mode") == mode_key and "error" not in v]
        if not entries:
            return {}
        total_cmds = sum(len(e["commands"]) for e in entries)
        actionable_cmds = sum(len(e["actionable_commands"]) for e in entries)
        avg_confidence = round(sum(e["confidence"] for e in entries) / len(entries), 3)
        return {
            "mode": mode_key,
            "total_alerts": len(entries),
            "total_commands": total_cmds,
            "actionable_commands": actionable_cmds,
            "actionability_rate": round(actionable_cmds / total_cmds, 3) if total_cmds else 0.0,
            "avg_confidence": avg_confidence,
        }

    aggregates = {m: aggregate(m) for m in modes_to_run}

    output = {
        "date": str(date.today()),
        "mode": mode,
        "aggregates": aggregates,
        "details": list(cached.values()),
    }

    out_file = RESULTS_DIR / f"actionability_{date.today()}.json"
    with open(out_file, "w") as f:
        json.dump(output, f, indent=2, default=str)

    print(f"\n=== Actionability Rate ===")
    for m, agg in aggregates.items():
        if agg:
            print(f"[{m}] actionability: {agg['actionability_rate']:.1%}  "
                  f"({agg['actionable_commands']}/{agg['total_commands']} commands)  "
                  f"avg_confidence: {agg['avg_confidence']:.2f}")
    print(f"Results saved to {out_file}")
    print(f"Cache saved to {cache_file} (use --use-cache + eval_safety.py to skip re-running)")
    return output


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=["rag", "zero_shot", "both"], default="rag")
    parser.add_argument("--use-cache", action="store_true")
    args = parser.parse_args()
    asyncio.run(main(mode=args.mode, use_cache=args.use_cache))
