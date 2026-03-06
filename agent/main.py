from fastapi import FastAPI
from pydantic import BaseModel
import httpx
import json

app = FastAPI()

OLLAMA_URL = "http://ollama-svc:11434/api/generate"

PROMPT_TEMPLATE = """You are an infrastructure parameter extractor. 
Given a natural language request, extract the following parameters and return ONLY a valid JSON object, no extra text, no markdown:
- project_name: name of the project
- region: GCP region (e.g. europe-west1)
- instance_type: machine type (e.g. e2-standard-2)
- purpose: brief description of what the resource is for

Request: {user_request}

Return ONLY the JSON object."""

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