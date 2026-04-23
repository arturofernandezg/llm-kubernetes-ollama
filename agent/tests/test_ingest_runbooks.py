"""
Tests for ingest_runbooks.py CLI.

All tests mock ingest_all_runbooks — no ChromaDB or Ollama required.
"""

import pytest
from unittest.mock import AsyncMock, patch

import ingest_runbooks


class TestIngestRunbooksCLI:
    @pytest.mark.asyncio
    async def test_run_returns_0_when_no_errors(self):
        stats = {"ingested": 16, "errors": 0, "total": 16}
        with patch("ingest_runbooks.ingest_all_runbooks", new=AsyncMock(return_value=stats)):
            result = await ingest_runbooks.run()
        assert result == 0

    @pytest.mark.asyncio
    async def test_run_returns_1_when_errors_present(self):
        stats = {"ingested": 14, "errors": 2, "total": 16}
        with patch("ingest_runbooks.ingest_all_runbooks", new=AsyncMock(return_value=stats)):
            result = await ingest_runbooks.run()
        assert result == 1

    @pytest.mark.asyncio
    async def test_run_passes_runbooks_dir_to_ingest(self):
        stats = {"ingested": 16, "errors": 0, "total": 16}
        mock_ingest = AsyncMock(return_value=stats)
        with patch("ingest_runbooks.ingest_all_runbooks", new=mock_ingest), \
             patch("ingest_runbooks.RUNBOOKS_DIR", "/test/runbooks"):
            await ingest_runbooks.run()
        called_dir = mock_ingest.call_args[0][0]
        assert called_dir == "/test/runbooks"
