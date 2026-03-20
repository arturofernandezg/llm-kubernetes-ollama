"""
AIOps Diagnosis module.

Takes a normalized alert + RAG context and generates a structured diagnosis
via the LLM. Enforces JSON output schema for downstream validation.
"""

import json
import time

import httpx

from config import settings, logger
from extraction import extract_json

# ── Prompt template ───────────────────────────────────────────────────────────

DIAGNOSIS_PROMPT = """\
You are an AIOps diagnostic agent for Kubernetes clusters. Given a firing alert and relevant context from runbooks and past incidents, produce a structured JSON diagnosis.

RULES:
- Return ONLY a valid JSON object, no extra text.
- "commands" must be real kubectl commands the operator can run immediately.
- "risk" must be one of: "low", "medium", "high".
- "confidence" must be a float between 0.0 and 1.0.
- If context is insufficient, set confidence below 0.5 and risk to "high".
- NEVER suggest destructive commands (delete namespace, delete pvc, rm -rf).

OUTPUT SCHEMA:
{{
  "diagnosis": "One-sentence root cause",
  "commands": ["kubectl ...", "kubectl ..."],
  "confidence": 0.0,
  "risk": "low|medium|high",
  "explanation": "Detailed reasoning (2-3 sentences max)"
}}

--- ALERT ---
{alert_text}

--- RELEVANT RUNBOOKS ---
{runbook_context}

--- PAST INCIDENTS ---
{incident_context}

Output:"""


# ── Diagnosis generation ──────────────────────────────────────────────────────

def format_context_docs(docs: list[dict]) -> str:
    """Format retrieved documents into a readable string for the prompt."""
    if not docs:
        return "No relevant context found."

    parts = []
    for doc in docs:
        meta = doc.get("metadata", {})
        header = meta.get("error_class", "unknown")
        distance = doc.get("distance", 0)
        parts.append(f"[{header}] (similarity: {1 - distance:.2f})\n{doc['document']}")
    return "\n\n".join(parts)


def build_alert_text(alert_labels: dict, alert_annotations: dict, status: str) -> str:
    """Build a human-readable alert summary from Alertmanager fields."""
    alertname = alert_labels.get("alertname", "UnknownAlert")
    pod = alert_labels.get("pod", "unknown-pod")
    namespace = alert_labels.get("namespace", "unknown-ns")
    severity = alert_labels.get("severity", "unknown")
    description = alert_annotations.get("description", "No description")
    summary = alert_annotations.get("summary", "")

    text = f"Alert: {alertname} (status: {status}, severity: {severity})\n"
    text += f"Pod: {pod}, Namespace: {namespace}\n"
    if summary:
        text += f"Summary: {summary}\n"
    text += f"Description: {description}"
    return text


async def generate_diagnosis(
    alert_labels: dict,
    alert_annotations: dict,
    alert_status: str,
    rag_context: dict,
    http_client: httpx.AsyncClient,
) -> dict:
    """
    Generate a structured AIOps diagnosis.

    Args:
        alert_labels:      Prometheus labels (alertname, pod, namespace, severity)
        alert_annotations: Prometheus annotations (description, summary)
        alert_status:      "firing" or "resolved"
        rag_context:       Output from rag.retrieve_context()
        http_client:       Shared httpx client for Ollama

    Returns:
        Dict with: diagnosis, commands, confidence, risk, explanation, model_used,
                   duration_ms, rag_sources
    """
    start = time.time()

    alert_text = build_alert_text(alert_labels, alert_annotations, alert_status)
    runbook_context = format_context_docs(rag_context.get("runbooks", []))
    incident_context = format_context_docs(rag_context.get("incidents", []))

    prompt = DIAGNOSIS_PROMPT.format(
        alert_text=alert_text,
        runbook_context=runbook_context,
        incident_context=incident_context,
    )

    response = await http_client.post(
        settings.ollama_url,
        json={
            "model": settings.ollama_model,
            "prompt": prompt,
            "stream": False,
        },
    )
    response.raise_for_status()

    raw = response.json().get("response", "")
    parsed, method = extract_json(raw)

    duration_ms = int((time.time() - start) * 1000)

    # Collect source IDs for traceability
    rag_sources = (
        [d["id"] for d in rag_context.get("runbooks", [])]
        + [d["id"] for d in rag_context.get("incidents", [])]
    )

    if parsed:
        # Enforce expected fields with safe defaults
        result = {
            "diagnosis": parsed.get("diagnosis", "Unable to determine root cause"),
            "commands": parsed.get("commands", []),
            "confidence": _clamp(parsed.get("confidence", 0.0), 0.0, 1.0),
            "risk": parsed.get("risk", "high") if parsed.get("risk") in ("low", "medium", "high") else "high",
            "explanation": parsed.get("explanation", ""),
            "model_used": settings.ollama_model,
            "duration_ms": duration_ms,
            "rag_sources": rag_sources,
            "raw_response": raw,
        }
        logger.info(
            "Diagnosis generated: confidence=%.2f risk=%s duration=%dms",
            result["confidence"], result["risk"], duration_ms,
        )
    else:
        result = {
            "diagnosis": "LLM did not return valid structured output",
            "commands": [],
            "confidence": 0.0,
            "risk": "high",
            "explanation": f"Raw LLM response could not be parsed: {raw[:200]}",
            "model_used": settings.ollama_model,
            "duration_ms": duration_ms,
            "rag_sources": rag_sources,
            "raw_response": raw,
        }
        logger.warning("Diagnosis parsing failed after %dms. Raw: %s", duration_ms, raw[:150])

    return result


def _clamp(value, min_val, max_val):
    """Clamp a numeric value to [min_val, max_val]."""
    try:
        return max(min_val, min(float(value), max_val))
    except (TypeError, ValueError):
        return min_val
