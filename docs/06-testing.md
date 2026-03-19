# Testing

## Ejecución

```bash
# En GCloud Shell (o cualquier máquina con Python 3.11+)
cd agent
pip install -r requirements.txt -r requirements-dev.txt
python -m pytest tests/ -v
```

## Estructura de tests (`agent/tests/`)

| Archivo | Tests | Tipo | Qué verifica |
|---|---|---|---|
| `test_endpoints.py` | 26 | Integración | Health probes (/healthz, /readyz, /health), /extract end-to-end, retry con backoff, /metrics |
| `test_extraction.py` | 11 | Unitario | extract_json: direct, markdown_block, regex con bracket counting, nested JSON, edge cases |
| `test_tf_generator.py` | 16 | Unitario | safe_name (caracteres especiales, vacíos, trailing underscore), generate_terraform (template, defaults, labels) |
| `test_validation.py` | 11 | Unitario | validate_params: regiones válidas/inválidas, instance types con parametrize (6 prefijos), campos null |
| **Total** | **64** | | |

**Nota**: los tests estaban originalmente en un único `test_main.py` (40 tests).
Se refactorizaron en 4 archivos al modularizar el código del agente (commit 7ec4a3a)
y se añadieron tests nuevos para cubrir `tf_generator.py`, validación parametrizada
y casos adicionales. Verificado con Cloud Build el 2026-03-18: **64 passed in 0.39s**.

### Ficheros de soporte

- `helpers.py` — Constantes compartidas (`VALID_PARAMS`, `VALID_JSON_STR`) y factorías de mocks:
  `mock_http_client()`, `mock_ollama_unreachable()`, `mock_ollama_model_not_loaded()`,
  `mock_http_client_with_retries(fail_count)`
- `conftest.py` — Fixture `api_client` (TestClient con `asyncio.sleep` parcheado para evitar delays reales en tests de retry)

## Cómo funciona el mocking

Ollama se mockea completamente. No se necesita ni K8s ni Ollama para correr tests.

```python
# helpers.py proporciona mock factories reutilizables
from tests.helpers import mock_http_client, VALID_PARAMS

# conftest.py crea el fixture api_client que parchea asyncio.sleep
@pytest.fixture
def api_client():
    app.state.http_client = mock_http_client("")  # mock por defecto
    with patch("asyncio.sleep", new_callable=AsyncMock):
        with TestClient(app) as client:
            yield client

# En cada test que necesite comportamiento específico:
with patch.object(app.state, "http_client", mock_http_client(json_response)):
    r = client.post("/extract", json={"message": "..."})
```

El `TestClient` de FastAPI NO ejecuta el lifespan, por lo que `app.state.http_client`
no se crea automáticamente. Por eso se inicializa manualmente en conftest.py.

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

- [ ] Tests de integración real (con Ollama o Vertex AI direct, sin mock)
- [ ] Test de carga/performance (múltiples webhooks concurrentes desde Alertmanager)
- [ ] Tests unitarios para el parser de JSON de alertas de Prometheus (`/webhook/alert`)
- [ ] Tests para el formateador e integración con ChatOps (Mattermost API)

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

### safe_name produce trailing underscore
**Causa**: input terminado en caracteres especiales (ej: "test-") generaba identificadores
con underscore final ("test_"). Las assertions de los tests esperaban el underscore.
**Solución**: se añadió `.strip("_")` al final de `safe_name()` y se actualizaron las
assertions. Corregido en commit e36ceab.

### Extracción de JSON nested falla con regex
**Causa**: el fallback regex usaba un patrón simple que no manejaba objetos JSON anidados
(ej: `{"params": {"nested": "value"}}`). El `}` interno cerraba prematuramente el match.
**Solución**: se reemplazó el regex por un algoritmo de conteo de llaves (bracket counting)
que rastrea la profundidad de anidamiento. Corregido en commit 5ec78f5.

### Import de helpers desde conftest.py causa errores
**Causa**: usar `from conftest import ...` genera problemas de import en pytest.
**Solución**: los helpers compartidos (mocks, constantes) van en `tests/helpers.py`.
`conftest.py` solo contiene fixtures de pytest.

### PytestDeprecationWarning sobre asyncio_default_fixture_loop_scope
**Causa**: pytest-asyncio 0.24 advierte sobre un cambio futuro en el scope por defecto.
**Impacto**: solo es un warning, los tests funcionan correctamente.
**Solución futura**: añadir `asyncio_default_fixture_loop_scope = function` al pytest.ini.

### Tests de validación: `us-east1` no genera warning en /extract real
**Descubierto**: 2026-03-18 durante pruebas end-to-end en el cluster.
**Contexto**: al enviar `"Quiero un servidor en us-east1"` al endpoint `/extract`,
la respuesta incluye `validation_warnings` para campos faltantes (instance_type, purpose)
pero **no avisa de que `us-east1` no está entre las regiones europe-\* del proyecto**.
**Causa**: `validation.py` sí valida regiones con `VALID_REGIONS`, pero esa lista incluye
regiones US y Asia además de las europeas. La convención del proyecto es solo europe-\*,
pero la validación acepta un conjunto más amplio.
**Nota**: los tests unitarios (`test_invalid_region_generates_warning`) usan `"zona-inventada-1"`
(región que no existe), no una región GCP válida fuera de Europa. Gap entre test y regla de negocio.
