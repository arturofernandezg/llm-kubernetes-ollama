"""
Evaluation: Model comparison for remediation-action generation (F3).

Pregunta que responde: ¿un modelo distinto produce la ACCIÓN de remediación
estructurada correcta (p.ej. `proposed_action` con `field=resources.limits.cpu`
ante HighCPU) que el motor F3 necesita para auto-remediar?

El hallazgo de cluster (2026-06-30) fue que qwen2.5:1.5b NO propone `set resources cpu`
ante HighCPU — emite comandos de investigación y `risk=high`. Este harness compara
N modelos OFFLINE, sin tocar GKE, para decidir si vale la pena desplegar uno mejor.

Diseño:
- Inyecta el runbook correspondiente como contexto (lee runbooks/<expected_runbook>.yaml),
  SIN ChromaDB ni embeddings → aísla el razonamiento del modelo del ruido del retrieval.
- Pasa cada diagnóstico por el motor REAL (validate_commands + decide_action) con el flag
  de auto-CPU OFF y ON → reporta el veredicto de negocio (escalate vs auto_remediate),
  no una proxy.

Generación a temp=0 (settings.ollama_temperature, default 0.0) → comparación reproducible:
la varianza de sampling deja de contaminar el contraste entre modelos.

Uso (Mac local, solo necesita Ollama + los modelos; sin port-forward, el runbook se
inyecta de disco, no toca ChromaDB ni embeddings):
    cd agent
    OLLAMA_URL=http://localhost:11434/api/generate \
    python -m evaluation.eval_model_compare \
        --models qwen2.5:1.5b,qwen3.5:9b,mistral-small:24b --alerts highcpu,highmemory,oom

    # qwen3.5:2b como control de "misma talla, nueva gen"; mistral-nemo:12b si 24b pesa.
    # Por defecto solo las alertas HighCPU. Para incluir más: --alerts highcpu,oom
"""

import argparse
import asyncio
import json
import re
from datetime import date
from pathlib import Path

import httpx
import yaml

from config import settings, logger
from diagnosis import generate_diagnosis
from remediation import RemediationAction, decide_action, validate_commands

# ── Paths ──────────────────────────────────────────────────────────────────────
_EVAL_DIR = Path(__file__).parent
DATASETS_DIR = _EVAL_DIR / "datasets"
RUNBOOKS_DIR = _EVAL_DIR.parent / "runbooks"
RESULTS_DIR = _EVAL_DIR.parent / "evaluation_results"

# alertname → recurso esperado en proposed_action.field (None = no se espera acción de límite)
_EXPECTED_RESOURCE = {
    "HighCPU": "cpu",
    "KubePodOOMKilled": "memory",
    "HighMemory": "memory",
}

_PARSE_FAIL_SENTINEL = "LLM did not return valid structured output"
_SET_RESOURCES_RE = re.compile(r"set\s+resources.*--limits=(cpu|memory)=", re.IGNORECASE)


def load_alerts(prefixes: list[str]) -> list[dict]:
    """Load dataset alerts whose id starts with any of the given prefixes."""
    alerts: list[dict] = []
    for dataset_file in sorted(DATASETS_DIR.glob("alerts_*.json")):
        with open(dataset_file) as f:
            for entry in json.load(f):
                if any(entry["id"].startswith(p) for p in prefixes):
                    alerts.append(entry)
    return alerts


def inject_runbook_context(expected_runbook: str) -> dict:
    """Build a rag_context that mirrors retrieve_context() output, with the matching
    runbook injected directly from disk (no ChromaDB)."""
    path = RUNBOOKS_DIR / f"{expected_runbook}.yaml"
    with open(path) as f:
        rb = yaml.safe_load(f)
    doc = {
        "id": rb["id"],
        "document": rb["text"],
        "distance": 0.0,
        "metadata": {
            "error_class": rb["error_class"],
            "service": rb["service"],
            "severity": rb["severity"],
        },
    }
    return {"query": rb["error_class"], "runbooks": [doc], "incidents": []}


def _set_resources_resource(commands: list[str]) -> str | None:
    """Return 'cpu'|'memory' if any command is a `set resources --limits=<r>=`, else None."""
    for c in commands:
        if isinstance(c, str):
            m = _SET_RESOURCES_RE.search(c)
            if m:
                return m.group(1).lower()
    return None


def _decide_with_flag(diagnosis: dict, validations: list[dict], cpu_flag: bool) -> str:
    """Run the real decision engine with the auto-CPU flag set to cpu_flag."""
    settings.remediation_auto_cpu_enabled = cpu_flag
    return decide_action(diagnosis, validations).value


async def eval_one(alert: dict, model: str, client: httpx.AsyncClient) -> dict:
    """Run one alert through one model; return the F3-relevant metrics."""
    payload = alert["payload"]
    first = payload["alerts"][0]
    labels = first["labels"]
    annotations = first.get("annotations", {})
    status = first["status"]
    alertname = labels.get("alertname", "")
    expected_resource = _EXPECTED_RESOURCE.get(alertname)

    rag_context = inject_runbook_context(alert["expected_runbook"])

    settings.ollama_model = model
    diagnosis = await generate_diagnosis(labels, annotations, status, rag_context, client)

    commands = [c for c in diagnosis.get("commands", []) if isinstance(c, str)]
    pa = diagnosis.get("proposed_action")
    proposed_field = pa.get("field") if isinstance(pa, dict) else None
    proposed_resource = proposed_field.rsplit(".", 1)[-1] if proposed_field else None
    set_res = _set_resources_resource(commands)

    validations = validate_commands(commands)
    verdict_off = _decide_with_flag(diagnosis, validations, cpu_flag=False)
    verdict_on = _decide_with_flag(diagnosis, validations, cpu_flag=True)

    return {
        "alert_id": alert["id"],
        "model": model,
        "alertname": alertname,
        "expected_resource": expected_resource,
        "parsed_ok": diagnosis.get("diagnosis") != _PARSE_FAIL_SENTINEL,
        "proposed_field": proposed_field,
        "field_correct": (expected_resource is not None and proposed_resource == expected_resource),
        "has_set_resources": set_res,
        "n_commands": len(commands),
        "risk": diagnosis.get("risk"),
        "confidence": diagnosis.get("confidence"),
        "verdict_flag_off": verdict_off,
        "verdict_flag_on": verdict_on,
        "duration_ms": diagnosis.get("duration_ms", 0),
        "diagnosis_text": diagnosis.get("diagnosis", "")[:160],
    }


def aggregate(rows: list[dict], model: str) -> dict:
    entries = [r for r in rows if r["model"] == model]
    n = len(entries)
    actionable = [r for r in entries if r["expected_resource"] is not None]
    field_hits = sum(1 for r in actionable if r["field_correct"])
    auto_on = sum(1 for r in entries if r["verdict_flag_on"] == RemediationAction.AUTO_REMEDIATE.value)
    parsed = sum(1 for r in entries if r["parsed_ok"])
    avg_conf = round(sum((r["confidence"] or 0.0) for r in entries) / n, 3) if n else 0.0
    avg_ms = int(sum(r["duration_ms"] for r in entries) / n) if n else 0
    return {
        "model": model,
        "alerts": n,
        "parsed_ok": f"{parsed}/{n}",
        "field_correct": f"{field_hits}/{len(actionable)}" if actionable else "n/a",
        "auto_remediate_flag_on": f"{auto_on}/{n}",
        "avg_confidence": avg_conf,
        "avg_duration_ms": avg_ms,
    }


async def main(models: list[str], prefixes: list[str]) -> dict:
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    alerts = load_alerts(prefixes)
    if not alerts:
        print(f"No alerts matched prefixes {prefixes}")
        return {}

    # Config del motor como en cluster (remediation activo, riesgo bajo, conf 0.8).
    settings.remediation_enabled = True
    settings.remediation_auto_max_risk = "low"
    settings.remediation_auto_confidence = 0.8

    rows: list[dict] = []
    async with httpx.AsyncClient(timeout=600.0) as client:
        for model in models:
            for alert in alerts:
                print(f"  {model} × {alert['id']} (LLM, puede tardar)...")
                try:
                    rows.append(await eval_one(alert, model, client))
                except Exception as exc:
                    logger.error("eval failed for %s × %s: %s", model, alert["id"], exc)
                    rows.append({
                        "alert_id": alert["id"], "model": model, "error": str(exc),
                        "expected_resource": None, "parsed_ok": False, "field_correct": False,
                        "verdict_flag_on": "error", "verdict_flag_off": "error",
                        "confidence": 0.0, "duration_ms": 0,
                    })

    aggregates = [aggregate(rows, m) for m in models]
    output = {"date": str(date.today()), "models": models, "alerts": prefixes,
              "aggregates": aggregates, "details": rows}

    out_file = RESULTS_DIR / f"model_compare_{date.today()}.json"
    with open(out_file, "w") as f:
        json.dump(output, f, indent=2, default=str)

    print("\n=== Comparativa de modelos (acción de remediación F3) ===")
    print(f"{'model':<20} {'parsed':>8} {'field_ok':>9} {'auto(on)':>9} {'avg_conf':>9} {'avg_ms':>9}")
    for a in aggregates:
        print(f"{a['model']:<20} {a['parsed_ok']:>8} {a['field_correct']:>9} "
              f"{a['auto_remediate_flag_on']:>9} {a['avg_confidence']:>9} {a['avg_duration_ms']:>9}")
    print(f"\nDetalle por alerta en {out_file}")
    print("Lectura: field_ok = ¿proposed_action.field coincide con el recurso esperado "
          "(cpu/memory)? · auto(on) = ¿decide_action daría AUTO_REMEDIATE con el flag de CPU on?")
    return output


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--models", required=True,
                        help="lista separada por comas, p.ej. qwen2.5:1.5b,qwen3.5")
    parser.add_argument("--alerts", default="highcpu",
                        help="prefijos de id separados por comas (default: highcpu). Ej: highcpu,oom")
    args = parser.parse_args()
    asyncio.run(main(
        models=[m.strip() for m in args.models.split(",") if m.strip()],
        prefixes=[p.strip() for p in args.alerts.split(",") if p.strip()],
    ))
