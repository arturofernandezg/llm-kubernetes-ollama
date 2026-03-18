"""
Extracción de JSON desde respuestas de LLM.

Contiene el prompt template y la función extract_json() con 3 estrategias
de parseo progresivas: JSON directo, bloque markdown y regex fallback.
"""

import json
import re


# ── Prompt ────────────────────────────────────────────────────────────────────
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
Output: {{"project_name": "payments", "region": "europe-west1", "instance_type": "e2-standard-4", "purpose": "web traffic"}}

<user_message>
{user_request}
</user_message>
Output:"""


# ── Extracción ────────────────────────────────────────────────────────────────
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

    # 3. Bracket-counting fallback — soporta JSON anidado
    start = text.find("{")
    while start != -1:
        depth = 0
        for i, ch in enumerate(text[start:], start):
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
            if depth == 0:
                try:
                    return json.loads(text[start:i + 1]), "regex_search"
                except json.JSONDecodeError:
                    break
        start = text.find("{", start + 1)

    return None, None
