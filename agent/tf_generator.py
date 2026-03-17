"""
Generador de Terraform — módulo del agente.

Contiene las funciones puras para generar archivos .tf a partir de
los parámetros extraídos por el agente. Usado internamente por el
agente en Phase 2 (Slack → extract → genera .tf → PR en GitHub).
"""

import re
from datetime import datetime, timezone

# ── Configuración del módulo Terraform ────────────────────────────────────────

TF_MODULE_SOURCE = "terraform-modules/gcp-vm"
TF_MODULE_VERSION = "~> 1.0"

# ── Template Terraform ────────────────────────────────────────────────────────

TF_TEMPLATE = """\
# =============================================================================
# Generado automáticamente por AIOps Infrastructure Agent
# Fecha:        {generated_at}
# Request ID:   {request_id}
# Modelo LLM:   {model_used}
# Mensaje original: "{input_message}"
# =============================================================================

terraform {{
  required_version = ">= 1.3"
  required_providers {{
    google = {{
      source  = "hashicorp/google"
      version = "~> 5.0"
    }}
  }}
}}

provider "google" {{
  project = var.project_id
  region  = "{region}"
}}

# ── Variables ──────────────────────────────────────────────────────────────────

variable "project_id" {{
  description = "GCP project ID where resources will be deployed"
  type        = string
}}

variable "ssh_public_key" {{
  description = "SSH public key for instance access"
  type        = string
  sensitive   = true
}}

# ── Módulo de VM ───────────────────────────────────────────────────────────────

module "vm_{safe_project_name}" {{
  source  = "{module_source}"
  version = "{module_version}"

  project_name  = "{project_name}"
  region        = "{region}"
  instance_type = "{instance_type}"
  purpose       = "{purpose}"

  project_id     = var.project_id
  ssh_public_key = var.ssh_public_key

  labels = {{
    managed-by  = "aiops-agent"
    project     = "{safe_project_name}"
    environment = "production"
    created-by  = "terraform"
  }}
}}

# ── Outputs ────────────────────────────────────────────────────────────────────

output "instance_name" {{
  description = "Name of the created VM instance"
  value       = module.vm_{safe_project_name}.instance_name
}}

output "instance_ip" {{
  description = "Internal IP of the created VM instance"
  value       = module.vm_{safe_project_name}.internal_ip
}}

output "instance_zone" {{
  description = "Zone where the instance was deployed"
  value       = module.vm_{safe_project_name}.zone
}}
"""


# ── Helpers ───────────────────────────────────────────────────────────────────

def safe_name(s: str) -> str:
    """Convierte un string a identificador válido para Terraform (solo a-z, 0-9, _)."""
    return re.sub(r"[^a-z0-9_]", "_", s.lower()).strip("_") or "project"


# ── Generador ─────────────────────────────────────────────────────────────────

def generate_terraform(agent_response: dict) -> str:
    """
    Toma la respuesta del agente y genera el contenido del fichero .tf.
    Si faltan parámetros, usa valores por defecto razonables.
    """
    params = agent_response.get("extracted_parameters") or {}

    project_name  = params.get("project_name")  or "undefined-project"
    region        = params.get("region")         or "europe-west1"
    instance_type = params.get("instance_type")  or "e2-standard-2"
    purpose       = params.get("purpose")        or "general purpose"

    return TF_TEMPLATE.format(
        generated_at     = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
        request_id       = agent_response.get("request_id", "unknown"),
        model_used       = agent_response.get("model_used", "unknown"),
        input_message    = agent_response.get("input_message", ""),
        project_name     = project_name,
        safe_project_name= safe_name(project_name),
        region           = region,
        instance_type    = instance_type,
        purpose          = purpose,
        module_source    = TF_MODULE_SOURCE,
        module_version   = TF_MODULE_VERSION,
    )
