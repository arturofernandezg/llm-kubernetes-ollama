"""
Fixtures de pytest compartidos para todos los tests del agente.
"""

import pytest
from fastapi.testclient import TestClient

from main import app
from tests.helpers import mock_http_client


# ── Inicializar app.state.http_client para tests ─────────────────────────────
# El TestClient no ejecuta el lifespan, así que creamos un mock por defecto.
app.state.http_client = mock_http_client("")


@pytest.fixture
def api_client():
    return TestClient(app)
