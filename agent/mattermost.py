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


async def _post_with_retry(payload: dict[str, Any]) -> bool:
    """Envía payload JSON a Mattermost con retry/exponential backoff."""
    if not hasattr(settings, 'mattermost_webhook_url') or not settings.mattermost_webhook_url:
        logger.warning("Mattermost Webhook URL not configured. Skipping.")
        return False

    async with httpx.AsyncClient(timeout=settings.http_timeout) as client:
        last_exc: Exception | None = None

        for attempt in range(MATTERMOST_MAX_RETRIES):
            try:
                response = await client.post(settings.mattermost_webhook_url, json=payload)
                response.raise_for_status()
                logger.info(f"Successfully sent to Mattermost (attempt {attempt+1})")
                return True

            except httpx.HTTPStatusError as exc:
                logger.error(f"Mattermost HTTP error: {exc.response.status_code} - {exc.response.text}")
                if 400 <= exc.response.status_code < 500:
                    break
                last_exc = exc
            except (httpx.ConnectError, httpx.TimeoutException) as exc:
                last_exc = exc
            except Exception as exc:
                logger.error(f"Unexpected error communicating with Mattermost: {exc}")
                return False

            if attempt < MATTERMOST_MAX_RETRIES - 1:
                delay = MATTERMOST_BASE_DELAY * (2 ** attempt)
                logger.warning(
                    f"Mattermost attempt {attempt+1}/{MATTERMOST_MAX_RETRIES} failed "
                    f"({type(last_exc).__name__}). Retrying in {delay}s..."
                )
                await asyncio.sleep(delay)

        logger.error(
            f"Failed to communicate with Mattermost after {MATTERMOST_MAX_RETRIES} attempts. "
            f"Last error: {last_exc}"
        )
        return False


async def send_mattermost_alert(message: str, channel: str | None = None) -> bool:
    """
    Envía un mensaje formateado a Mattermost.
    Implementa un patrón Retry suave / Exponential Backoff
    para tolerar fallos transitorios de red hacia el servidor de chat.
    """
    payload: dict[str, Any] = {"text": message}
    if channel:
        payload["channel"] = channel
    return await _post_with_retry(payload)


async def send_escalation_with_buttons(
    header: str,
    attachment_text: str,
    safe_commands: list[str],
    incident_id: str,
    callback_base_url: str,
    channel: str | None = None,
) -> bool:
    """
    Envía mensaje Mattermost con botones interactivos Aprobar/Rechazar.
    Mattermost llamará a {callback_base_url}/webhook/action cuando el usuario
    haga clic, con el incident_id en el contexto para identificar la escalación.
    """
    action_url = f"{callback_base_url}/webhook/action"
    payload: dict[str, Any] = {
        "text": header,
        "attachments": [
            {
                "color": "#FF6600",
                "text": attachment_text,
                "actions": [
                    {
                        "id": "approve",
                        "name": "✅ Ejecutar remediación",
                        "integration": {
                            "url": action_url,
                            "context": {"action": "approve", "incident_id": incident_id},
                        },
                    },
                    {
                        "id": "reject",
                        "name": "❌ Rechazar",
                        "integration": {
                            "url": action_url,
                            "context": {"action": "reject", "incident_id": incident_id},
                        },
                    },
                ],
            }
        ],
    }
    if channel:
        payload["channel"] = channel
    return await _post_with_retry(payload)
