"""
generate_tf.py — AIOps Terraform Generator

Llama al endpoint /extract del agente FastAPI, toma los parámetros
extraídos y genera un fichero .tf listo para usar con terraform apply.

Uso:
    python generate_tf.py "Necesito un servidor para el proyecto web-prod en europe-west1 con maquina e2-standard-4"
    python generate_tf.py "Server for payments project, europe-southwest1, n2-standard-2"

    # Apuntar a un agente en otra URL:
    AGENT_URL=http://localhost:8000 python generate_tf.py "..."

    # Solo extraer JSON, sin generar .tf:
    python generate_tf.py --dry-run "..."
"""

import argparse
import json
import os
import sys
from pathlib import Path

import urllib.request
import urllib.error

# Importar funciones del módulo del agente (evitar duplicación)
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "agent"))
from tf_generator import safe_name, generate_terraform  # noqa: E402

# ── Configuración ──────────────────────────────────────────────────────────────

DEFAULT_AGENT_URL = os.getenv("AGENT_URL", "http://localhost:8000")
OUTPUT_DIR = Path(os.getenv("TF_OUTPUT_DIR", "./terraform_output"))


# ── Llamada al agente ──────────────────────────────────────────────────────────

def call_extract_endpoint(message: str, agent_url: str) -> dict:
    """
    Llama a POST /extract del agente FastAPI y devuelve el JSON de respuesta.
    Usa urllib de stdlib para no requerir dependencias externas.
    """
    url = f"{agent_url}/extract"
    body = json.dumps({"message": message}).encode("utf-8")

    req = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=150) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        error_body = e.read().decode("utf-8")
        print(f"\n  Error HTTP {e.code} del agente:")
        try:
            detail = json.loads(error_body).get("detail", error_body)
            print(f"    {detail}")
        except json.JSONDecodeError:
            print(f"    {error_body}")
        sys.exit(1)
    except urllib.error.URLError as e:
        print(f"\n  No se puede conectar con el agente en {agent_url}")
        print(f"    Asegúrate de que el port-forward está activo:")
        print(f"    kubectl port-forward svc/agent-svc 8000:8000 -n arturo-llm-test")
        print(f"    Error: {e.reason}")
        sys.exit(1)


def print_summary(agent_response: dict) -> None:
    """Muestra un resumen legible de lo que extrajo el agente."""
    params   = agent_response.get("extracted_parameters") or {}
    warnings = agent_response.get("validation_warnings", [])

    print("\n" + "─" * 60)
    print("  PARÁMETROS EXTRAÍDOS")
    print("─" * 60)
    print(f"  project_name  : {params.get('project_name')  or '⚠️  no detectado'}")
    print(f"  region        : {params.get('region')         or '⚠️  no detectado'}")
    print(f"  instance_type : {params.get('instance_type')  or '⚠️  no detectado'}")
    print(f"  purpose       : {params.get('purpose')        or '⚠️  no detectado'}")
    print(f"\n  request_id    : {agent_response.get('request_id')}")
    print(f"  model_used    : {agent_response.get('model_used')}")
    print(f"  extract_method: {agent_response.get('extraction_method')}")
    print(f"  duration_ms   : {agent_response.get('duration_ms')}ms")

    if warnings:
        print(f"\n  ⚠️  WARNINGS ({len(warnings)}):")
        for w in warnings:
            print(f"     · {w}")
    else:
        print("\n    Sin warnings de validación")
    print("─" * 60)


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="AIOps Terraform Generator — extrae parámetros y genera .tf"
    )
    parser.add_argument(
        "message",
        help="Mensaje en lenguaje natural describiendo la infraestructura necesaria",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Solo extrae parámetros, no genera el fichero .tf",
    )
    parser.add_argument(
        "--output",
        metavar="FILE",
        help="Nombre del fichero .tf de salida (por defecto: <project_name>.tf)",
    )
    parser.add_argument(
        "--agent-url",
        metavar="URL",
        default=DEFAULT_AGENT_URL,
        help=f"URL del agente FastAPI (por defecto: {DEFAULT_AGENT_URL})",
    )
    args = parser.parse_args()

    agent_url = args.agent_url

    # 1. Llamar al agente
    print(f"\n🔍  Enviando mensaje al agente ({agent_url})...")
    print(f"    \"{args.message}\"")

    agent_response = call_extract_endpoint(args.message, agent_url)

    # 2. Mostrar resumen
    print_summary(agent_response)

    # 3. Mostrar raw_response para debug si no hay parámetros
    if not agent_response.get("extracted_parameters"):
        print(f"\n  El modelo no devolvió JSON válido.")
        print(f"   Raw response del modelo:")
        print(f"   {agent_response.get('raw_response', '')[:300]}")
        if not args.dry_run:
            print("\n   Generando .tf con valores por defecto...")

    if args.dry_run:
        print("\n✅  Modo --dry-run: no se genera fichero .tf")
        return

    # 4. Generar contenido Terraform
    tf_content = generate_terraform(agent_response)

    # 5. Determinar ruta del fichero de salida
    params = agent_response.get("extracted_parameters") or {}
    project_name = params.get("project_name") or "infra"

    if args.output:
        output_path = Path(args.output)
        output_path.parent.mkdir(parents=True, exist_ok=True)
    else:
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        output_path = OUTPUT_DIR / f"{safe_name(project_name)}.tf"

    # 6. Guardar fichero
    output_path.write_text(tf_content, encoding="utf-8")

    print(f"\n✅  Fichero generado: {output_path}")
    print(f"    Tamaño: {len(tf_content)} caracteres")
    print()


if __name__ == "__main__":
    main()
