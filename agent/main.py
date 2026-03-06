from fastapi import FastAPI
from pydantic import BaseModel
import httpx
import json

app = FastAPI()

OLLAMA_URL = "http://ollama-svc:11434/api/generate"

PROMPT_TEMPLATE = """Eres un extractor de parámetros de infraestructura cloud.
Dado un mensaje en lenguaje natural, extrae los siguientes parámetros y devuelve ÚNICAMENTE un objeto JSON válido, sin texto adicional, sin markdown, sin explicaciones:
- project_name: nombre del proyecto
- region: región de GCP (ej. europe-west1)
- instance_type: tipo de máquina (ej. e2-standard-2)
- purpose: descripción breve del uso del recurso

Si algún parámetro no se menciona explícitamente, dedúcelo del contexto o usa null.

Mensaje: {user_request}

Devuelve ÚNICAMENTE el objeto JSON."""

class InfraRequest(BaseModel):
    message: str

@app.get("/health")
def health():
    return {"status": "ok"}

@app.post("/extract")
async def extract_parameters(request: InfraRequest):
    prompt = PROMPT_TEMPLATE.format(user_request=request.message)
    
    async with httpx.AsyncClient(timeout=120) as client:
        response = await client.post(OLLAMA_URL, json={
            "model": "tinyllama",
            "prompt": prompt,
            "stream": False
        })
    
    result = response.json()
    raw = result.get("response", "")
    
    try:
        parsed = json.loads(raw)
    except Exception:
        parsed = None
    
    return {
        "input_message": request.message,
        "extracted_parameters": parsed,
        "raw_response": raw
    }