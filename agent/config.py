"""
Configuración centralizada del agente AIOps.

Todas las variables de entorno se validan al arrancar via pydantic-settings.
Si un valor no se encuentra en el entorno, se usa el default definido aquí.
"""

import logging

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """Settings del agente, cargados desde variables de entorno."""

    # Ollama
    ollama_url: str = "http://ollama-svc:11434/api/generate"
    ollama_tags: str = "http://ollama-svc:11434/api/tags"
    ollama_model: str = "tinyllama"

    # Timeouts (segundos)
    http_timeout: float = 120.0
    health_timeout: float = 5.0

    model_config = {"env_prefix": "", "case_sensitive": False}


settings = Settings()

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
)
logger = logging.getLogger("aiops_agent")
