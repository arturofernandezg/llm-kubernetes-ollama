"""
Cliente HTTP Asíncrono para Mattermost.
Envía notificaciones no bloqueantes usando httpx y reintentos (Exponential Backoff).
Diseñado para ser invocado como FastAPI BackgroundTask.
"""

import httpx
import asyncio
from typing import Any

from config import settings, logger

# Constants for retries
MATTERMOST_MAX_RETRIES = 3
MATTERMOST_BASE_DELAY = 1.0


async def send_mattermost_alert(message: str, channel: str | None = None) -> bool:
    """
    Envía un mensaje formateado a Mattermost.
    Implementa un patrón Retry suave / Exponential Backoff
    para tolerar fallos transitorios de red hacia el servidor de chat.
    """
    
    # Tolerancia a fallo por mala configuración (Fail-open)
    if not hasattr(settings, 'mattermost_webhook_url') or not settings.mattermost_webhook_url:
        logger.warning("Mattermost Webhook URL not configured. Skipping alert sending.")
        return False
        
    payload: dict[str, Any] = {"text": message}
    if channel:
        payload["channel"] = channel

    # Instanciamos el cliente en la task (BackgroundTasks no heredan el lifespan context)
    async with httpx.AsyncClient(timeout=settings.http_timeout) as client:
        last_exc: Exception | None = None
        
        for attempt in range(MATTERMOST_MAX_RETRIES):
            try:
                response = await client.post(
                    settings.mattermost_webhook_url,
                    json=payload
                )
                response.raise_for_status()
                logger.info(f"Successfully sent alert to Mattermost (Attempt {attempt+1})")
                return True
                
            except httpx.HTTPStatusError as exc:
                logger.error(f"Mattermost HTTP error: {exc.response.status_code} - {exc.response.text}")
                # Errores del cliente (4xx) como mal payload no se reintentan
                if 400 <= exc.response.status_code < 500:
                    break
                last_exc = exc
            except (httpx.ConnectError, httpx.TimeoutException) as exc:
                last_exc = exc
            except Exception as exc:
                logger.error(f"Unexpected error communicating with Mattermost: {exc}")
                return False
                
            # Backoff exponencial si quedan intentos
            if attempt < MATTERMOST_MAX_RETRIES - 1:
                delay = MATTERMOST_BASE_DELAY * (2 ** attempt)
                logger.warning(
                    f"Mattermost attempt {attempt+1}/{MATTERMOST_MAX_RETRIES} failed ({type(last_exc).__name__}). Retrying in {delay}s..."
                )
                await asyncio.sleep(delay)
                
        logger.error(f"Failed to communicate with Mattermost after {MATTERMOST_MAX_RETRIES} attempts. Last error: {last_exc}")
        return False
