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
from rag import INCIDENT_OUTCOME_ROLLED_BACK, INCIDENT_OUTCOME_ROLLBACK_FAILED

# Outcomes that mark a past fix as FAILED (applied but reverted) — surfaced with
# an explicit warning in the prompt context so the model does not repeat them.
FAILED_FIX_OUTCOMES = {INCIDENT_OUTCOME_ROLLED_BACK, INCIDENT_OUTCOME_ROLLBACK_FAILED}

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
- PAST INCIDENTS carry their final outcome: "cured" means the fix worked; "rolled_back" or "rollback_failed" means the fix was applied and FAILED (it was reverted). NEVER propose a fix marked as failed again — propose a different approach or lower your confidence.

OUTPUT SCHEMA:
{{
  "diagnosis": "One-sentence root cause",
  "commands": ["kubectl ...", "kubectl ..."],
  "confidence": 0.0,
  "risk": "low|medium|high",
  "explanation": "Detailed reasoning (2-3 sentences max)",
  "proposed_action": {{
    "kind": "Deployment",
    "name": "<resource-name>",
    "namespace": "<namespace>",
    "container": "<container-name>",
    "field": "resources.limits.memory | resources.limits.cpu",
    "current_value": "<current limit — memory e.g. 256Mi, cpu e.g. 250m>",
    "new_value": "<proposed limit — memory e.g. 512Mi, cpu e.g. 500m>"
  }}
}}

PROPOSED ACTION RULES:
- Include "proposed_action" ONLY when a command modifies a resource LIMIT on a specific workload.
  Two fields are supported:
  - "resources.limits.memory" — for memory pressure (e.g. OOMKilled, HighMemory). Values use IEC units (256Mi, 512Mi, 1Gi).
  - "resources.limits.cpu" — for sustained CPU throttling (e.g. HighCPU). Values use millicores (250m, 500m, 1000m) or cores (1, 0.5).
- "field" must be exactly one of the two values above.
- "current_value" must reflect the limit in the alert labels or context. If the current limit is NOT present in the alert text or context, OMIT proposed_action entirely. NEVER guess, fabricate, or default the current_value — a wrong baseline causes unsafe scaling.
- "new_value" must match the value used in the corresponding kubectl command, and must NOT exceed 2× current_value.
- For a CPU change, the command is `kubectl set resources deployment <name> -n <ns> --containers=<c> --limits=cpu=<new_value>`.
- If no limit change is proposed, omit the "proposed_action" field entirely.

--- ALERT ---
{alert_text}

--- RELEVANT RUNBOOKS ---
{runbook_context}

--- PAST INCIDENTS ---
{incident_context}
{cluster_context}
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
        # Incidents carry their final outcome (R2): label it so the model can
        # tell a good precedent (cured) from a fix that failed and was reverted.
        outcome = meta.get("outcome")
        if outcome:
            header = f"{header} | outcome: {outcome}"
            if outcome in FAILED_FIX_OUTCOMES:
                header += " — FAILED FIX (reverted, do not repeat)"
        distance = doc.get("distance", 0)
        parts.append(f"[{header}] (similarity: {1 - distance:.2f})\n{doc['document']}")
    return "\n\n".join(parts)


def format_cluster_facts(snapshot) -> str:
    """Format the cluster-sourced snapshot into an authoritative prompt block.

    Grounding (v2 Eje A): feed the LLM the REAL pod state (phase, last termination
    reason, restarts, current limits) so it reasons over facts, not guesses. Returns
    an empty string when there is no usable snapshot (gather disabled/failed) — the
    prompt then degrades to the previous alert+RAG-only form (backward-compatible).
    """
    if snapshot is None or not getattr(snapshot, "gather_ok", False):
        return ""

    lines = []
    if getattr(snapshot, "phase", None):
        lines.append(f"- phase: {snapshot.phase}")
    if getattr(snapshot, "last_state_reason", None):
        lines.append(f"- last termination reason: {snapshot.last_state_reason}")
    if getattr(snapshot, "restart_count", None) is not None:
        lines.append(f"- restarts: {snapshot.restart_count}")

    container = getattr(snapshot, "container", None)
    limits = (getattr(snapshot, "limits", {}) or {}).get(container or "", {})
    if limits:
        rendered = ", ".join(f"{k}={v}" for k, v in limits.items())
        lines.append(f"- container '{container}' limits: {rendered}")

    # F-17: free-text signals — the LLM reasons over these in language (never executed).
    # Rendered as separate blocks so structured facts stay unambiguous above.
    events = getattr(snapshot, "recent_events", None) or []
    logs_tail = getattr(snapshot, "logs_tail", None)

    if not lines and not events and not logs_tail:
        return ""

    block = ""
    if lines:
        block += (
            "\n--- CLUSTER FACTS (observed, authoritative — trust over your own guesses) ---\n"
            + "\n".join(lines) + "\n"
        )
    if events:
        block += (
            "\n--- RECENT EVENTS (observed, newest last) ---\n"
            + "\n".join(f"- {e}" for e in events) + "\n"
        )
    if logs_tail:
        block += (
            "\n--- RECENT LOGS (pod log tail, observed) ---\n"
            + logs_tail + "\n"
        )
    return block


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
    snapshot=None,
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
    cluster_context = format_cluster_facts(snapshot)

    prompt = DIAGNOSIS_PROMPT.format(
        alert_text=alert_text,
        runbook_context=runbook_context,
        incident_context=incident_context,
        cluster_context=cluster_context,
    )

    response = await http_client.post(
        settings.ollama_url,
        json={
            "model": settings.ollama_model,
            "prompt": prompt,
            "stream": False,
            "options": {"temperature": settings.ollama_temperature},
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
        # Extract proposed_action if present and structurally valid
        raw_pa = parsed.get("proposed_action")
        proposed_action = None
        if isinstance(raw_pa, dict) and raw_pa.get("field") and raw_pa.get("current_value") and raw_pa.get("new_value"):
            proposed_action = raw_pa

        # Enforce expected fields with safe defaults
        result = {
            "diagnosis": parsed.get("diagnosis", "Unable to determine root cause"),
            "commands": parsed.get("commands", []),
            "confidence": _clamp(parsed.get("confidence", 0.0), 0.0, 1.0),
            "risk": parsed.get("risk", "high") if parsed.get("risk") in ("low", "medium", "high") else "high",
            "explanation": parsed.get("explanation", ""),
            "proposed_action": proposed_action,
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
            "proposed_action": None,
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
