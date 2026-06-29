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
    # 300s alineado con el manifiesto desplegado: el LLM qwen2.5:1.5b tarda ~205-252s;
    # un default de 120 rompe la reproducibilidad local (timeouts del diagnóstico).
    http_timeout: float = 300.0
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

    # Cola Redis Streams (F2) — camino único de ingesta (validada en cluster 2026-06-29)
    # El webhook encola en O(1) (XADD) y un consumidor in-process drena de a una
    # (Ollama serializa la generación). Si Redis cae, el webhook devuelve 503
    # fail-closed (Alertmanager reintenta) — no se pierden alertas.
    queue_stream_key: str = "aiops:alerts"
    queue_group: str = "aiops-workers"
    dedup_window_seconds: int = 300       # TTL de la dedup-key por fingerprint
    queue_maxlen: int = 1000              # XADD MAXLEN ~ <n> (acota memoria Redis)
    queue_max_deliveries: int = 3         # dead-letter por delivery-count (Slice 2)
    queue_reclaim_interval_seconds: int = 60   # cada cuánto corre el reclaim periódico (Slice 2)
    # Solo se reclaman entradas con idle > esto. DEBE superar el peor caso de
    # diagnóstico (~252s) para no robar trabajo en curso del consumer → 600s con margen.
    queue_min_idle_seconds: int = 600
    queue_dead_letter_key: str = "aiops:alerts:dead"   # stream de cuarentena para poison messages

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
