"""
Tests del cliente de integración con Mattermost.
"""

import pytest
from unittest.mock import AsyncMock, patch, MagicMock
import httpx

from mattermost import send_mattermost_alert, MATTERMOST_MAX_RETRIES
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
