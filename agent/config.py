"""
Configuración centralizada del agente AIOps.

Todas las variables de entorno se validan al arrancar via pydantic-settings.
Si un valor no se encuentra en el entorno, se usa el default definido aquí.
"""

import logging
import sys

from pydantic_settings import BaseSettings
from pythonjsonlogger.json import JsonFormatter


class Settings(BaseSettings):
    """Settings del agente, cargados desde variables de entorno."""

    # Ollama
    ollama_url: str = "http://ollama-svc:11434/api/generate"
    ollama_tags: str = "http://ollama-svc:11434/api/tags"
    ollama_model: str = "tinyllama"

    # Timeouts (segundos)
    http_timeout: float = 120.0
    health_timeout: float = 5.0

    # Retry con exponential backoff (llamadas a Ollama)
    retry_max_attempts: int = 3
    retry_base_delay: float = 1.0
    retry_max_delay: float = 10.0

    # Logging
    log_level: str = "INFO"

    # Mattermost (Fase 1 - Observabilidad Activa)
    # URL completa del incoming webhook de Mattermost.
    # En K8s se usa el FQDN cross-namespace:
    #   http://mattermost-svc.arturo-mattermost.svc.cluster.local:8065/hooks/<token>
    mattermost_webhook_url: str | None = None

    # ChromaDB (Fase 2 - RAG)
    chromadb_host: str = "chromadb-svc"
    chromadb_port: int = 8000

    # Ollama Embeddings (Fase 2 - RAG)
    ollama_embed_url: str = "http://ollama-svc:11434/api/embeddings"
    ollama_embed_model: str = "nomic-embed-text"

    # Remediation (Fase 3)
    remediation_enabled: bool = False
    remediation_auto_confidence: float = 0.8
    remediation_auto_max_risk: str = "low"
    remediation_dry_run: bool = True          # False = ejecución real via kubectl
    remediation_command_timeout: int = 30     # segundos por comando
    remediation_rollback_enabled: bool = True
    remediation_rollback_timeout: int = 300  # seconds to wait before evaluating rollback
    remediation_rollback_grace: int = 30     # seconds for pod rollout before health check

    # Human escalation callback URL (Fase 3 — botones interactivos Mattermost)
    # URL base del agente accesible desde Mattermost (intra-cluster FQDN)
    agent_callback_url: str = "http://agent-svc.arturo-llm-test.svc.cluster.local:8000"

    # HMAC signing secret for Mattermost button callbacks (Fase 3 — X5)
    # Empty = HMAC disabled (dev/test). Set via K8s Secret in production.
    webhook_secret: str = ""

    # Mattermost slash command token (Mini-Fase 4 — ChatOps consulta)
    # Token que MM genera al crear el slash command. Empty = auth disabled (dev).
    mm_command_token: str = ""

    # Redis — persistencia de escalaciones pendientes (Mini-Fase 4 — mejora)
    # Sin Redis, las escalaciones viven en memoria y se pierden si el pod reinicia.
    redis_host: str = "redis-svc"
    redis_port: int = 6379

    model_config = {"env_prefix": "", "case_sensitive": False}


settings = Settings()

# ── Logging JSON estructurado ─────────────────────────────────────────────────
# Cada línea de log es un JSON parseable por Cloud Logging / ELK / Loki.
handler = logging.StreamHandler(sys.stdout)
handler.setFormatter(JsonFormatter(
    fmt="%(asctime)s %(levelname)s %(name)s %(message)s",
    rename_fields={"asctime": "timestamp", "levelname": "severity"},
))
logging.root.handlers = [handler]
logging.root.setLevel(getattr(logging, settings.log_level.upper(), logging.INFO))
logger = logging.getLogger("aiops_agent")
