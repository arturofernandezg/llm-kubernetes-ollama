# Testing

## Ejecución

```bash
# En GCloud Shell (o cualquier máquina con Python 3.11+)
cd agent
pip install -r requirements.txt -r requirements-dev.txt
python -m pytest tests/ -v
```

## Estructura de tests (`agent/tests/test_main.py`)

| Clase | Tests | Tipo | Qué verifica |
|---|---|---|---|
| TestExtractJson | 9 | Unitario | Extracción JSON: direct, markdown, regex, edge cases |
| TestValidateParams | 8 | Unitario | Validación de regiones, instance types, campos null |
| TestHealthzEndpoint | 2 | Integración | Liveness probe (siempre 200, incluso con Ollama down) |
| TestReadyzEndpoint | 3 | Integración | Readiness probe (200/503 según estado de Ollama) |
| TestHealthEndpoint | 2 | Integración | Health legacy (retrocompatibilidad) |
| TestExtractEndpoint | 13 | Integración | Extracción end-to-end, errores HTTP, validación input |
| **Total** | **40** | | |

## Cómo funciona el mocking

Ollama se mockea completamente. No se necesita ni K8s ni Ollama para correr tests.

```python
# Mock del cliente HTTP compartido (app.state.http_client)
app.state.http_client = _mock_http_client("")  # inicialización por defecto

# En cada test que necesite comportamiento específico:
with patch.object(app.state, "http_client", _mock_http_client(json_response)):
    r = client.post("/extract", json={"message": "..."})
```

El `TestClient` de FastAPI NO ejecuta el lifespan, por lo que `app.state.http_client`
no se crea automáticamente. Por eso se inicializa manualmente en el módulo de tests.

## Configuración pytest

`agent/pytest.ini`:
```ini
[pytest]
testpaths = tests
asyncio_mode = auto
log_cli = true
log_cli_level = INFO
```

## Dependencias de test

`agent/requirements-dev.txt`:
- pytest==8.3.4
- pytest-asyncio==0.24.0
- httpx==0.28.1 (también en requirements.txt)

## Tests pendientes (TODO)

- [ ] Tests para `generate_tf.py` (generate_terraform, safe_name, call_extract_endpoint)
- [ ] Tests de integración real (con Ollama corriendo, no mock)
- [ ] Test de carga/performance (múltiples requests concurrentes)
- [ ] Tests para integración Slack (Fase 2)
- [ ] Tests para GitHub client (Fase 2)

---

## Errores conocidos y soluciones

### "ModuleNotFoundError: No module named 'fastapi'"
**Causa**: solo instalaste `requirements-dev.txt` sin `requirements.txt`.
**Solución**: `pip install -r requirements.txt -r requirements-dev.txt`

### "bash: pytest: command not found"
**Causa**: pytest se instaló en `~/.local/bin` que no está en el PATH de Cloud Shell.
**Solución**: usar `python -m pytest tests/ -v`

### "'State' object has no attribute 'http_client'"
**Causa**: los tests usaban `patch("main.httpx.AsyncClient", ...)` que parcheaba
el constructor. Con el cliente compartido, hay que parchear `app.state.http_client`.
**Solución**: usar `patch.object(app.state, "http_client", mock)`.
Esto se corrigió en el commit fd3ca0f.

### "assert any('instance_type' in w for w in warnings)" falla
**Causa**: el warning dice "instance type" (con espacio), no "instance_type" (underscore).
**Solución**: buscar con `.lower()`: `any("instance type" in w.lower() for w in warnings)`.
Corregido en el mismo commit.

### PytestDeprecationWarning sobre asyncio_default_fixture_loop_scope
**Causa**: pytest-asyncio 0.24 advierte sobre un cambio futuro en el scope por defecto.
**Impacto**: solo es un warning, los tests funcionan correctamente.
**Solución futura**: añadir `asyncio_default_fixture_loop_scope = function` al pytest.ini.
