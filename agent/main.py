"""
AIOps Infrastructure Agent — Fase 1

Extrae parámetros de infraestructura GCP a partir de mensajes en lenguaje
natural, usando un LLM local (Ollama) como motor de inferencia.
"""

from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel, field_validator
import httpx
import json
import logging
import os
import re
import time
import uuid

# ── Logging ────────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
)
logger = logging.getLogger(__name__)

# ── Configuración via variables de entorno ─────────────────────────────────────
OLLAMA_URL  = os.getenv("OLLAMA_URL",   "http://ollama-svc:11434/api/generate")
OLLAMA_TAGS = os.getenv("OLLAMA_TAGS",  "http://ollama-svc:11434/api/tags")
MODEL       = os.getenv("OLLAMA_MODEL", "tinyllama")

# Valores válidos de GCP — ampliar según las regiones permitidas en MasOrange
VALID_REGIONS: frozenset[str] = frozenset({
    "europe-west1", "europe-west2", "europe-west3", "europe-west4",
    "europe-southwest1", "us-central1", "us-east1", "us-west1",
    "asia-east1", "asia-northeast1",
})
VALID_INSTANCE_PREFIXES: tuple[str, ...] = (
    "e2-", "n1-", "n2-", "n2d-", "c2-", "m1-", "t2d-"
)

# ── Prompt ─────────────────────────────────────────────────────────────────────
PROMPT_TEMPLATE = """\
You are an infrastructure parameter extractor. Given a message, extract exactly these 4 fields and return ONLY a valid JSON object, no extra text:
- project_name: project name (string)
- region: GCP region, e.g. europe-west1 (string)
- instance_type: machine type, e.g. e2-standard-4 (string)
- purpose: short description of the resource purpose, max 5 words (string)

If a parameter is not mentioned, use null.
Do not copy the full message into purpose. Summarize it in 2-5 words.

Examples:
Message: "I need a server for the payments project in europe-west1 with e2-standard-4 for web traffic"
Output: {"project_name": "payments", "region": "europe-west1", "instance_type": "e2-standard-4", "purpose": "web traffic"}

Message: {user_request}
Output:"""


# ── Lifespan ───────────────────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    """Verifica conectividad con Ollama y disponibilidad del modelo al arrancar."""
    logger.info(f"Agent starting — model: {MODEL}, ollama: {OLLAMA_URL}")
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(OLLAMA_TAGS)
            r.raise_for_status()
            available = [m["name"] for m in r.json().get("models", [])]
            if any(MODEL in m for m in available):
                logger.info(f"Model '{MODEL}' confirmed available")
            else:
                logger.warning(f"Model '{MODEL}' NOT found. Available: {available}")
    except Exception as exc:
        logger.warning(f"Could not reach Ollama at startup: {exc}")
    yield
    logger.info("Agent shutting down")


app = FastAPI(
    title="AIOps Infrastructure Agent",
    description="Extrae parámetros de infraestructura GCP desde lenguaje natural.",
    version="0.1.0",
    lifespan=lifespan,
)


# ── Schemas ────────────────────────────────────────────────────────────────────
class InfraRequest(BaseModel):
    message: str

    @field_validator("message")
    @classmethod
    def message_not_empty(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("message cannot be empty")
        if len(v) > 2000:
            raise ValueError("message too long (max 2000 chars)")
        return v


class ExtractedParams(BaseModel):
    project_name:  str | None = None
    region:        str | None = None
    instance_type: str | None = None
    purpose:       str | None = None


class ExtractResponse(BaseModel):
    request_id:           str
    input_message:        str
    extracted_parameters: ExtractedParams | None
    validation_warnings:  list[str]
    raw_response:         str
    model_used:           str
    extraction_method:    str | None
    duration_ms:          int


# ── Helpers ────────────────────────────────────────────────────────────────────
def extract_json(text: str) -> tuple[dict | None, str | None]:
    """
    Extrae un objeto JSON del texto devuelto por el modelo.

    Estrategias (en orden de fiabilidad):
      1. Parseo directo — el modelo devolvió JSON puro.
      2. Bloque markdown — JSON dentro de ```json ... ``` o ``` ... ```.
      3. Regex fallback — primer { ... } encontrado en el texto.

    Returns:
        Tupla (dict, method_name). Ambos son None si no se encuentra JSON.
    """
    stripped = text.strip()

    # 1. JSON puro
    try:
        return json.loads(stripped), "direct"
    except json.JSONDecodeError:
        pass

    # 2. Bloque markdown
    match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(1)), "markdown_block"
        except json.JSONDecodeError:
            pass

    # 3. Primer objeto JSON en texto libre
    match = re.search(r"\{[^{}]*\}", text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group()), "regex_search"
        except json.JSONDecodeError:
            pass

    return None, None


def validate_params(params: dict) -> list[str]:
    """
    Valida los parámetros extraídos contra valores conocidos de GCP.

    No bloquea la respuesta — genera warnings informativos que el llamante
    puede usar para decidir si escalar a revisión humana.
    """
    warnings: list[str] = []

    region = params.get("region")
    if region and region not in VALID_REGIONS:
        warnings.append(
            f"Unknown region '{region}' — verify it is a valid GCP region"
        )

    instance_type = params.get("instance_type")
    if instance_type and not any(
        instance_type.startswith(p) for p in VALID_INSTANCE_PREFIXES
    ):
        warnings.append(
            f"Unusual instance type '{instance_type}' — verify GCP machine type format"
        )

    for field in ("project_name", "region", "instance_type", "purpose"):
        if not params.get(field):
            warnings.append(f"Missing parameter: '{field}'")

    return warnings


# ── Endpoints ──────────────────────────────────────────────────────────────────
@app.get("/health", summary="Health check — agent + Ollama + modelo")
async def health():
    """
    Verifica que:
    - El agente responde.
    - Ollama es alcanzable.
    - El modelo configurado está cargado en Ollama.
    """
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            r = await client.get(OLLAMA_TAGS)
            r.raise_for_status()
            available = [m["name"] for m in r.json().get("models", [])]
            model_loaded = any(MODEL in m for m in available)
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"Ollama unreachable: {exc}")

    return {
        "status": "ok",
        "model": MODEL,
        "model_loaded": model_loaded,
        "available_models": available,
        "ollama_url": OLLAMA_URL,
    }


@app.post(
    "/extract",
    response_model=ExtractResponse,
    summary="Extrae parámetros de infraestructura desde lenguaje natural",
)
async def extract_parameters(request: InfraRequest, http_request: Request):
    """
    Recibe un mensaje en lenguaje natural, lo envía al LLM y devuelve
    los parámetros de infraestructura extraídos en formato estructurado.

    Si el modelo no devuelve JSON puro, se intentan estrategias de
    extracción progresivas (markdown, regex). Los parámetros extraídos
    se validan contra valores conocidos de GCP y se devuelven warnings
    cuando algo es sospechoso, sin bloquear la respuesta.
    """
    request_id = str(uuid.uuid4())[:8]
    start = time.time()
    logger.info(f"[{request_id}] Processing: {request.message[:100]}")

    prompt = PROMPT_TEMPLATE.format(user_request=request.message)

    try:
        async with httpx.AsyncClient(timeout=120) as client:
            response = await client.post(
                OLLAMA_URL,
                json={"model": MODEL, "prompt": prompt, "stream": False},
            )
            response.raise_for_status()
    except httpx.TimeoutException:
        logger.error(f"[{request_id}] Ollama timeout after 120s")
        raise HTTPException(status_code=504, detail="LLM timeout — model took too long")
    except httpx.HTTPStatusError as exc:
        logger.error(f"[{request_id}] Ollama HTTP error: {exc.response.status_code}")
        raise HTTPException(
            status_code=502, detail=f"LLM returned error: {exc.response.status_code}"
        )
    except httpx.HTTPError as exc:
        logger.error(f"[{request_id}] Ollama connection error: {exc}")
        raise HTTPException(status_code=502, detail=f"LLM unavailable: {exc}")

    raw = response.json().get("response", "")
    parsed_dict, method = extract_json(raw)
    warnings = (
        validate_params(parsed_dict)
        if parsed_dict
        else ["Could not extract JSON from model response"]
    )

    duration_ms = int((time.time() - start) * 1000)

    if parsed_dict:
        logger.info(f"[{request_id}] OK via '{method}' in {duration_ms}ms — {parsed_dict}")
    else:
        logger.warning(f"[{request_id}] Failed in {duration_ms}ms. Raw: {raw[:150]}")

    return ExtractResponse(
        request_id=request_id,
        input_message=request.message,
        extracted_parameters=ExtractedParams(**parsed_dict) if parsed_dict else None,
        validation_warnings=warnings,
        raw_response=raw,
        model_used=MODEL,
        extraction_method=method,
        duration_ms=duration_ms,
    )