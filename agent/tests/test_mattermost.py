"""
Tests del cliente de integración con Mattermost.
"""

import pytest
from unittest.mock import AsyncMock, patch, MagicMock
import httpx

from mattermost import send_mattermost_alert
from config import settings

class TestMattermostClient:
    
    @pytest.mark.asyncio
    async def test_fails_silently_if_missing_webhook_url(self):
        settings.mattermost_webhook_url = None
        result = await send_mattermost_alert("Test message")
        assert result is False

    @pytest.mark.asyncio
    @patch("mattermost.MATTERMOST_BASE_DELAY", 0.0) # Evitar que el test duerma
    async def test_success_sends_to_configured_url(self):
        settings.mattermost_webhook_url = "http://mattermost/hooks/fake-url"
        
        mock_response = AsyncMock()
        mock_response.raise_for_status = MagicMock()
        
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_response)
        
        # Async context manager mock para httpx.AsyncClient
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        
        with patch("httpx.AsyncClient", return_value=mock_client):
            result = await send_mattermost_alert("Mensaje crítico", channel="admin-channel")
            
            assert result is True
            mock_client.post.assert_called_once()
            args, kwargs = mock_client.post.call_args
            assert args[0] == "http://mattermost/hooks/fake-url"
            assert kwargs["json"]["text"] == "Mensaje crítico"
            assert kwargs["json"]["channel"] == "admin-channel"
            
    @pytest.mark.asyncio
    @patch("mattermost.MATTERMOST_BASE_DELAY", 0.0) # Speedup tests
    async def test_retry_on_timeout(self):
        settings.mattermost_webhook_url = "http://mattermost/hooks/fake-url"
        
        mock_response = AsyncMock()
        mock_response.raise_for_status = MagicMock()
        
        mock_client = AsyncMock()
        # Fallar la 1a vez (Timeout), Exito la 2a vez
        mock_client.post = AsyncMock(side_effect=[httpx.TimeoutException("timeout"), mock_response])
        
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        
        with patch("httpx.AsyncClient", return_value=mock_client):
            result = await send_mattermost_alert("Reintento")
            
            assert result is True
            assert mock_client.post.call_count == 2
