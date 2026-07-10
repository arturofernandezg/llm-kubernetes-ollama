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
    # Greedy decoding (temp=0): la remediación necesita razonamiento estructurado
    # reproducible. Default 0.8 de Ollama causaba varianza run-to-run en proposed_action
    # (un OOM dio current_value="32Mi", otro la lista fabricada "256Mi, 512Mi, 1Gi").
    ollama_temperature: float = 0.0

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
    mattermost_timeout: float = 10.0  # F-04: chat webhook responds in ms; inheriting http_timeout (LLM-sized) held retries hostage for minutes

    # ChromaDB (Fase 2 - RAG)
    chromadb_host: str = "chromadb-svc"
    chromadb_port: int = 8000

    # Ollama Embeddings (Fase 2 - RAG)
    ollama_embed_url: str = "http://ollama-svc:11434/api/embeddings"
    ollama_embed_model: str = "nomic-embed-text"

    # Remediation (Fase 3)
    remediation_enabled: bool = False
    remediation_auto_confidence: float = 0.8
    remediation_auto_min_restarts: int = 1  # grounding: min cluster-observed restarts to trust an OOM/crash incident as recurring (feeds ground_confidence)
    remediation_auto_max_risk: str = "low"
    remediation_auto_cpu_enabled: bool = False  # gate: extiende la excepción 4.5 a CPU (escalate-first hasta validar en cluster)
    remediation_auto_namespace_prefix: str = "arturo-"  # guardrail: auto solo en namespaces propios (cluster compartido); "" = sin restricción
    remediation_dry_run: bool = True          # False = ejecución real via kubectl
    remediation_command_timeout: int = 30     # segundos por comando
    remediation_rollback_enabled: bool = True
    remediation_rollback_timeout: int = 300  # seconds to wait before evaluating rollback
    remediation_rollback_grace: int = 30     # seconds for pod rollout before health check
    remediation_cooldown_seconds: int = 600  # per-workload auto-remediation cooldown (> rollback window: one auto patch per workload per window, breaks patch loops)
    # R5 (observational learning loop): how long an active incident stays correlatable
    # by fingerprint so a later Alertmanager `resolved` can close it (outcome
    # resolved_observed + MTTR metric). Generous — an alert may take a while to clear;
    # on expiry we simply lose the resolution signal (fail-open).
    incident_correlation_ttl_seconds: int = 3600

    # Enrichment (v2 Eje A / P0·1 — grounding) — gather de estado K8s ANTES del LLM.
    # "El cluster informa, el modelo razona": current_value/target salen del snapshot,
    # no del modelo. Fail-soft: si falla, se degrada a diagnóstico LLM-only.
    enrichment_enabled: bool = True
    enrichment_timeout: int = 10  # segundos por query kubectl (corto; best-effort, no crítico)

    # Human escalation callback URL (Fase 3 — botones interactivos Mattermost)
    # URL base del agente accesible desde Mattermost (intra-cluster FQDN)
    agent_callback_url: str = "http://agent-svc.arturo-llm-test.svc.cluster.local:8000"

    # Ventana para aprobar/rechazar una escalación en Mattermost (C-03). Antes era una
    # constante hardcodeada (60) en main.py — un rebuild para cambiarla. 120min equilibra
    # demos/ausencias frente al riesgo F-09 (el approve ejecuta sin re-validar: una ventana
    # mayor amplía cuánto puede envejecer el estado que el humano aprueba).
    escalation_ttl_minutes: int = 120

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
