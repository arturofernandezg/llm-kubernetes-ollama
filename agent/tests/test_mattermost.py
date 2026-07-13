"""
Tests del cliente de integración con Mattermost.
"""

import pytest
from unittest.mock import AsyncMock, patch, MagicMock
import httpx

from mattermost import (
    send_mattermost_alert,
    send_escalation_with_buttons,
    make_hmac_token,
    sanitize_action_id,
    MATTERMOST_MAX_RETRIES,
    _MAX_TEXT_LENGTH,
)
from config import settings

FAKE_URL = "http://mattermost/hooks/fake-url"


def make_ok_response():
    mock_response = MagicMock()
    mock_response.raise_for_status = MagicMock()
    return mock_response


def make_error_response(status_code: int):
    mock_response = MagicMock()
    mock_response.status_code = status_code
    mock_response.text = f"HTTP {status_code}"
    mock_response.raise_for_status.side_effect = httpx.HTTPStatusError(
        message=f"HTTP {status_code}", request=MagicMock(), response=mock_response
    )
    return mock_response


def make_mock_client(post_side_effect):
    mock_client = AsyncMock()
    mock_client.post = AsyncMock(side_effect=post_side_effect
                                  if isinstance(post_side_effect, list)
                                  else [post_side_effect])
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)
    return mock_client


class TestMattermostClient:

    @pytest.mark.asyncio
    async def test_fails_silently_if_missing_webhook_url(self):
        """Sin URL configurada → False sin lanzar excepción (fail-open)."""
        settings.mattermost_webhook_url = None
        result = await send_mattermost_alert("Test message")
        assert result is False

    @pytest.mark.asyncio
    @patch("mattermost.MATTERMOST_BASE_DELAY", 0.0)
    async def test_success_sends_to_configured_url(self):
        """Envío exitoso → True, payload correcto."""
        settings.mattermost_webhook_url = FAKE_URL
        mock_client = make_mock_client([make_ok_response()])

        with patch("httpx.AsyncClient", return_value=mock_client):
            result = await send_mattermost_alert("Mensaje crítico", channel="admin-channel")

        assert result is True
        mock_client.post.assert_called_once()
        args, kwargs = mock_client.post.call_args
        assert args[0] == FAKE_URL
        assert kwargs["json"]["text"] == "Mensaje crítico"
        assert kwargs["json"]["channel"] == "admin-channel"

    @pytest.mark.asyncio
    async def test_uses_dedicated_mattermost_timeout(self):
        """F-04: el cliente usa mattermost_timeout (~10s), no el http_timeout del LLM
        (300s) — con MM caído, 3 retries heredando el gordo bloqueaban minutos."""
        settings.mattermost_webhook_url = FAKE_URL
        mock_client = make_mock_client([make_ok_response()])

        with patch("httpx.AsyncClient", return_value=mock_client) as client_cls:
            await send_mattermost_alert("Test")

        assert client_cls.call_args.kwargs["timeout"] == settings.mattermost_timeout
        assert client_cls.call_args.kwargs["timeout"] != settings.http_timeout

    @pytest.mark.asyncio
    @patch("mattermost.MATTERMOST_BASE_DELAY", 0.0)
    async def test_retry_on_timeout(self):
        """Timeout en el primer intento → reintenta → True."""
        settings.mattermost_webhook_url = FAKE_URL
        mock_client = make_mock_client([
            httpx.TimeoutException("timeout"),
            make_ok_response(),
        ])

        with patch("httpx.AsyncClient", return_value=mock_client):
            result = await send_mattermost_alert("Reintento")

        assert result is True
        assert mock_client.post.call_count == 2

    @pytest.mark.asyncio
    @patch("mattermost.MATTERMOST_BASE_DELAY", 0.0)
    async def test_no_retry_on_4xx(self):
        """Error 4xx (e.g. token inválido) → no reintenta → False."""
        settings.mattermost_webhook_url = FAKE_URL
        mock_client = make_mock_client([make_error_response(401)])

        with patch("httpx.AsyncClient", return_value=mock_client):
            result = await send_mattermost_alert("Bad token")

        assert result is False
        assert mock_client.post.call_count == 1

    @pytest.mark.asyncio
    @patch("mattermost.MATTERMOST_BASE_DELAY", 0.0)
    async def test_retry_on_5xx_then_gives_up(self):
        """5xx en todos los intentos → False tras MAX_RETRIES intentos."""
        settings.mattermost_webhook_url = FAKE_URL
        mock_client = make_mock_client(
            [make_error_response(503)] * MATTERMOST_MAX_RETRIES
        )

        with patch("httpx.AsyncClient", return_value=mock_client):
            result = await send_mattermost_alert("Server error")

        assert result is False
        assert mock_client.post.call_count == MATTERMOST_MAX_RETRIES

    @pytest.mark.asyncio
    @patch("mattermost.MATTERMOST_BASE_DELAY", 0.0)
    async def test_retry_on_connect_error_then_gives_up(self):
        """ConnectError en todos los intentos → False."""
        settings.mattermost_webhook_url = FAKE_URL
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(
            side_effect=httpx.ConnectError("Connection refused")
        )
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)

        with patch("httpx.AsyncClient", return_value=mock_client):
            result = await send_mattermost_alert("No connection")

        assert result is False
        assert mock_client.post.call_count == MATTERMOST_MAX_RETRIES

    @pytest.mark.asyncio
    async def test_returns_false_on_unexpected_exception(self):
        """Excepción inesperada → False sin propagar el error."""
        settings.mattermost_webhook_url = FAKE_URL
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(side_effect=RuntimeError("Unexpected"))
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)

        with patch("httpx.AsyncClient", return_value=mock_client):
            result = await send_mattermost_alert("Test")

        assert result is False

    @pytest.mark.asyncio
    @patch("mattermost.MATTERMOST_BASE_DELAY", 0.0)
    async def test_no_channel_in_payload_when_not_provided(self):
        """Sin channel → payload solo tiene 'text', sin 'channel'."""
        settings.mattermost_webhook_url = FAKE_URL
        mock_client = make_mock_client([make_ok_response()])

        with patch("httpx.AsyncClient", return_value=mock_client):
            await send_mattermost_alert("Alert without channel")

        _, kwargs = mock_client.post.call_args
        assert "channel" not in kwargs["json"]

    @pytest.mark.asyncio
    @patch("mattermost.MATTERMOST_BASE_DELAY", 0.0)
    async def test_long_message_is_truncated(self):
        """Mensaje mayor a _MAX_TEXT_LENGTH → truncado con ellipsis antes del envío."""
        settings.mattermost_webhook_url = FAKE_URL
        mock_client = make_mock_client([make_ok_response()])
        long_message = "x" * (_MAX_TEXT_LENGTH + 500)

        with patch("httpx.AsyncClient", return_value=mock_client):
            result = await send_mattermost_alert(long_message)

        assert result is True
        _, kwargs = mock_client.post.call_args
        sent_text = kwargs["json"]["text"]
        assert len(sent_text) == _MAX_TEXT_LENGTH + 1  # +1 for the ellipsis char
        assert sent_text.endswith("…")


class TestSendEscalationWithButtons:

    @pytest.mark.asyncio
    @patch("mattermost.MATTERMOST_BASE_DELAY", 0.0)
    async def test_sends_attachments_with_two_actions(self):
        """Payload tiene attachments[0].actions con Aprobar y Rechazar, URLs correctas."""
        settings.mattermost_webhook_url = FAKE_URL
        mock_client = make_mock_client([make_ok_response()])

        with patch("httpx.AsyncClient", return_value=mock_client):
            result = await send_escalation_with_buttons(
                header="escalation header",
                attachment_text="Diagnosis text",
                incident_id="test-uuid-123",
                callback_base_url="http://agent-svc:8000",
            )

        assert result is True
        _, kwargs = mock_client.post.call_args
        payload = kwargs["json"]
        assert payload["text"] == "escalation header"
        attachments = payload["attachments"]
        assert len(attachments) == 1
        actions = attachments[0]["actions"]
        assert len(actions) == 2
        assert actions[0]["id"] == "approve"
        assert actions[1]["id"] == "reject"
        assert actions[0]["integration"]["url"] == "http://agent-svc:8000/webhook/action"
        assert actions[0]["integration"]["context"]["incident_id"] == "test-uuid-123"
        assert actions[0]["integration"]["context"]["action"] == "approve"

    @pytest.mark.asyncio
    async def test_fails_silently_if_missing_webhook_url(self):
        """Sin URL configurada → False sin lanzar excepción (fail-open)."""
        settings.mattermost_webhook_url = None
        result = await send_escalation_with_buttons(
            header="header",
            attachment_text="body",
            incident_id="uuid",
            callback_base_url="http://agent:8000",
        )
        assert result is False

    @pytest.mark.asyncio
    @patch("mattermost.MATTERMOST_BASE_DELAY", 0.0)
    async def test_retry_on_5xx(self):
        """5xx en todos los intentos → False tras MAX_RETRIES intentos."""
        settings.mattermost_webhook_url = FAKE_URL
        mock_client = make_mock_client(
            [make_error_response(503)] * MATTERMOST_MAX_RETRIES
        )

        with patch("httpx.AsyncClient", return_value=mock_client):
            result = await send_escalation_with_buttons(
                header="header",
                attachment_text="body",
                incident_id="uuid",
                callback_base_url="http://agent:8000",
            )

        assert result is False
        assert mock_client.post.call_count == MATTERMOST_MAX_RETRIES

    @pytest.mark.asyncio
    @patch("mattermost.MATTERMOST_BASE_DELAY", 0.0)
    async def test_long_attachment_text_is_truncated(self):
        """attachment_text mayor a _MAX_TEXT_LENGTH → truncado con ellipsis antes del envío."""
        settings.mattermost_webhook_url = FAKE_URL
        mock_client = make_mock_client([make_ok_response()])
        long_text = "y" * (_MAX_TEXT_LENGTH + 1000)

        with patch("httpx.AsyncClient", return_value=mock_client):
            result = await send_escalation_with_buttons(
                header="header",
                attachment_text=long_text,
                incident_id="uuid",
                callback_base_url="http://agent:8000",
            )

        assert result is True
        _, kwargs = mock_client.post.call_args
        sent_text = kwargs["json"]["attachments"][0]["text"]
        assert len(sent_text) == _MAX_TEXT_LENGTH + 1  # +1 for the ellipsis char
        assert sent_text.endswith("…")

    @pytest.mark.asyncio
    @patch("mattermost.MATTERMOST_BASE_DELAY", 0.0)
    async def test_c08_variant_button_ids_are_alphanumeric(self):
        """C-08 regression (2026-07-13): the underscore in action ids 'approve_engine'/
        'approve_model' made Mattermost 404 the click (POST /posts/{id}/actions/{action_id}
        requires an alphanumeric action_id) — the button never reached the agent. The button
        `id` must be alphanumeric while integration.context.action keeps the real (underscored)
        action that the callback handler reads and the HMAC covers."""
        settings.mattermost_webhook_url = FAKE_URL
        mock_client = make_mock_client([make_ok_response()])

        with patch("httpx.AsyncClient", return_value=mock_client):
            result = await send_escalation_with_buttons(
                header="header",
                attachment_text="body",
                incident_id="inc-1",
                callback_base_url="http://agent:8000",
                webhook_secret="s3cr3t",
                approve_variants=[
                    {"action": "approve_engine", "label": "✅ ×2 motor"},
                    {"action": "approve_model", "label": "⚠️ Valor modelo"},
                ],
            )

        assert result is True
        actions = mock_client.post.call_args.kwargs["json"]["attachments"][0]["actions"]
        # 2 approve variants + reject
        assert [a["id"] for a in actions] == ["approveengine", "approvemodel", "reject"]
        # every routing id must be alphanumeric (else Mattermost 404s the click)
        for a in actions:
            assert a["id"].isalnum(), f"non-alphanumeric button id {a['id']!r} will 404"
        # the real underscored action survives in context + is what the HMAC signs
        engine = actions[0]["integration"]["context"]
        assert engine["action"] == "approve_engine"
        assert engine["hmac_token"] == make_hmac_token("inc-1", "approve_engine", "s3cr3t")

    def test_sanitize_action_id_strips_non_alphanumeric(self):
        """Underscores/symbols removed; plain actions untouched; never returns empty."""
        assert sanitize_action_id("approve_engine") == "approveengine"
        assert sanitize_action_id("approve_model") == "approvemodel"
        assert sanitize_action_id("approve") == "approve"
        assert sanitize_action_id("reject") == "reject"
        assert sanitize_action_id("___") == "action"
