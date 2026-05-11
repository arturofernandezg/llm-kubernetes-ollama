"""
Cliente HTTP Asíncrono para Mattermost.
Envía notificaciones no bloqueantes usando httpx y reintentos (Exponential Backoff).
Diseñado para ser invocado como FastAPI BackgroundTask.
"""

import httpx
import asyncio
from typing import Any

from config import settings, logger

MATTERMOST_MAX_RETRIES = 3
MATTERMOST_BASE_DELAY = 1.0

_MAX_TEXT_LENGTH = 14_000
_ATTACHMENT_COLOUR = "#FF6600"
_BTN_APPROVE_LABEL = "✅ Ejecutar remediación"
_BTN_REJECT_LABEL = "❌ Rechazar"


async def _post_with_retry(payload: dict[str, Any]) -> bool:
    """Envía payload JSON a Mattermost con retry/exponential backoff."""
    if not settings.mattermost_webhook_url:
        logger.warning("Mattermost webhook URL not configured")
        return False

    async with httpx.AsyncClient(timeout=settings.http_timeout) as client:
        last_exc: Exception | None = None

        for attempt in range(MATTERMOST_MAX_RETRIES):
            try:
                response = await client.post(settings.mattermost_webhook_url, json=payload)
                response.raise_for_status()
                logger.info("Mattermost send OK", extra={"attempt": attempt + 1})
                return True

            except httpx.HTTPStatusError as exc:
                logger.error(
                    "Mattermost HTTP error",
                    extra={"status": exc.response.status_code, "body": exc.response.text},
                )
                if 400 <= exc.response.status_code < 500:
                    return False  # client error, no retry
                last_exc = exc
            except (httpx.ConnectError, httpx.TimeoutException) as exc:
                last_exc = exc
            except Exception as exc:
                logger.error(
                    "Unexpected Mattermost error",
                    extra={"error": str(exc), "error_type": type(exc).__name__},
                )
                return False

            if attempt < MATTERMOST_MAX_RETRIES - 1:
                delay = MATTERMOST_BASE_DELAY * (2 ** attempt)
                logger.warning(
                    "Mattermost retry",
                    extra={
                        "attempt": attempt + 1,
                        "max": MATTERMOST_MAX_RETRIES,
                        "error_type": type(last_exc).__name__,
                        "delay": delay,
                    },
                )
                await asyncio.sleep(delay)

        logger.error(
            "Mattermost send failed",
            extra={"attempts": MATTERMOST_MAX_RETRIES, "last_error": str(last_exc)},
        )
        return False


async def send_mattermost_alert(message: str, channel: str | None = None) -> bool:
    """
    Envía un mensaje formateado a Mattermost.
    Implementa un patrón Retry suave / Exponential Backoff
    para tolerar fallos transitorios de red hacia el servidor de chat.
    """
    if len(message) > _MAX_TEXT_LENGTH:
        message = message[:_MAX_TEXT_LENGTH] + "…"
    payload: dict[str, Any] = {"text": message}
    if channel:
        payload["channel"] = channel
    return await _post_with_retry(payload)


async def send_escalation_with_buttons(
    header: str,
    attachment_text: str,
    incident_id: str,
    callback_base_url: str,
    channel: str | None = None,
) -> bool:
    """
    Envía mensaje Mattermost con botones interactivos Aprobar/Rechazar.
    Mattermost llamará a {callback_base_url}/webhook/action cuando el usuario
    haga clic, con el incident_id en el contexto para identificar la escalación.
    """
    if len(attachment_text) > _MAX_TEXT_LENGTH:
        attachment_text = attachment_text[:_MAX_TEXT_LENGTH] + "…"
    action_url = f"{callback_base_url}/webhook/action"
    payload: dict[str, Any] = {
        "text": header,
        "attachments": [
            {
                "color": _ATTACHMENT_COLOUR,
                "text": attachment_text,
                "actions": [
                    {
                        "id": "approve",
                        "name": _BTN_APPROVE_LABEL,
                        "integration": {
                            "url": action_url,
                            "context": {"action": "approve", "incident_id": incident_id},
                        },
                    },
                    {
                        "id": "reject",
                        "name": _BTN_REJECT_LABEL,
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
