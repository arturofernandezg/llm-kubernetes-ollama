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
| `test_endpoints.py` | 108 | Integración | Health probes, /extract end-to-end, retry con backoff, /webhook/alert, /metrics, formatter, feedback loop, `/webhook/action` (FakeRedis, approve/reject/HMAC), fallback isolation, `TestInFlightDedup` (×4), `TestDiagnosisTimeout` (×4), `TestEscalationStoreMetric` (×2, PR-06), `TestRagReconnect` (×2, PR-05) |
| `test_extraction.py` | 11 | Unitario | extract_json: direct, markdown_block, regex con bracket counting, nested JSON, edge cases |
| `test_tf_generator.py` | 16 | Unitario | safe_name, generate_terraform (template, defaults, labels) |
| `test_validation.py` | 6 | Unitario | validate_params: regiones, instance types, campos null |
| `test_mattermost.py` | 13 | Unitario | send_mattermost_alert: envío, retry 5xx, no-retry 4xx, ConnectError, excepción inesperada, fail-open; make_hmac_token |
| `test_rag.py` | 32 | Unitario | build_rag_query, generate_embedding (incl. guard ValueError), retrieve_context, ingest_runbook, load_runbooks_from_dir, ingest_all_runbooks, build_incident_document, ingest_incident (ChromaDB + Ollama mockeados) |
| `test_diagnosis.py` | 16 | Unitario | build_alert_text, format_context_docs, generate_diagnosis, _clamp (LLM mockeado) |
| `test_remediation.py` | 137 | Unitario | classify_command (SAFE/MUTATING/BLOCKED/UNKNOWN), validate_commands, decide_action (9 reglas incl. 4.5/4.6 + 7.5 `rag_degraded`→escalate, PR-04), execute_commands (dry-run + real mode mock), process_remediation, _get_safe_commands, zero_current_memory |
| `test_ingest_runbooks.py` | 3 | Unitario | CLI `ingest_runbooks.run()`: exit 0 sin errores, exit 1 con errores, runbooks_dir correcto (mocks `ingest_all_runbooks`) |
| `test_utils.py` | 5 | Unitario | `backoff_delay()`: primer intento = base, crecimiento exponencial, cap en max, base custom, edge case max == computed |
| `test_escalation_store.py` | 15 | Unitario | `store_escalation`, `get_escalation`, `delete_escalation`, `count_escalations` con FakeRedis; fail-open con Redis caído; flujo completo store→get→delete |
| `test_evaluation.py` | 21 | Unitario | Evaluación del retrieval RAG (p@1/p@3, safety vs zero-shot); soporte de `docs/10` |
| `test_rollback.py` | 11 | Unitario | Rollback post-patch: scheduling, `_evaluate_rollback` (healthy/revert), wiring en `_process_alert_with_diagnosis` |
| **Total** | **394** | | 394 funciones `def test_` en 13 ficheros. Verificado ✅ 2026-06-26 (F1 quick-wins): suite verde, +7 tests sobre los 387 de 2026-05-27 |

**Nota**: los tests estaban originalmente en un único `test_main.py` (40 tests).
Se refactorizaron y ampliaron progresivamente al añadir módulos:
- Fase 0 refactor (commit 7ec4a3a): 4 ficheros, 64 tests. Verificado con Cloud Build 2026-03-18.
- Fase 1 (2026-03-20): +8 tests para `mattermost.py`.
- Fase 2 (2026-03-20): +12 tests para `rag.py`, +14 tests para `diagnosis.py`.
- Fase 3 S4 (2026-03-31): +47 tests para `remediation.py` (classify, validate, decide, dry-run, pipeline).
- Fase 3 S5 (2026-04-06): +5 tests en `test_endpoints.py` (formatter + pipeline integration), +7 tests en `test_remediation.py` (real execution mode con subprocess mock).
- Fase 3 S6 (2026-04-07): +8 tests en `test_rag.py` (build_incident_document + ingest_incident), +2 tests en `test_endpoints.py` (feedback loop: persist + fail-open).
- Fase 2 cierre (2026-04-23): +3 tests en `test_ingest_runbooks.py` para el nuevo CLI de ingesta idempotente usado por el Job `k8s/job-ingest-runbooks.yaml`.
- Calidad sesión #1 (2026-05-11): +tests para locks PENDING_ESCALATIONS, proc guard, TTL reorder, periodic cleanup. Fixes H1-H5.
- Calidad sesiones #2-#3 (2026-05-11): +42 tests en `test_remediation.py` (classify non-string input, validate_commands coerce, zero_current_memory, _get_safe_commands, R1-R8). Fixes M1-M9 no añaden tests nuevos (comportamiento existente).
- Calidad sesión #4 (2026-05-11): +5 tests en `test_mattermost.py` (truncation guard, structured logging, 4xx no-retry). Fixes MM1-MM9.
- Calidad sesión #6 (2026-05-11): +3 tests en `test_endpoints.py::TestActionCallbackEndpoint` (HMAC missing/invalid/valid). Fix X5.
- Calidad sesión #7 (2026-05-12): +2 tests en `test_rag.py` (ValueError guard en `generate_embedding`). +2 tests en `test_endpoints.py` (fallback isolation M10). +5 tests en `test_utils.py` (nuevo fichero, `backoff_delay`). Fixes S2, M10, X2.
- F1 quick-wins (2026-06-25/26): +3 tests en `test_remediation.py` (PR-04, regla 7.5 `rag_degraded`→escalate). +2 tests en `test_endpoints.py::TestEscalationStoreMetric` (PR-06, `aiops_escalation_store_total`) + 2 extendidos (split `llm_timeout`/`llm_error`). +2 tests en `test_endpoints.py::TestRagReconnect` (PR-05, reconexión lazy ChromaDB). Total +7 → 394.

### Ficheros de soporte

- `helpers.py` — Constantes compartidas (`VALID_PARAMS`, `VALID_JSON_STR`), factorías de mocks y `FakeRedis` (stub in-memory de `redis.asyncio` para tests de `escalation_store` y `test_endpoints`):
  `mock_http_client()`, `mock_ollama_unreachable()`, `mock_ollama_model_not_loaded()`,
  `mock_http_client_with_retries(fail_count)`, `mock_diagnosis_auto_remediate()`,
  `mock_diagnosis_escalate()`, `FakeRedis`
- `conftest.py` — Fixture `api_client` (TestClient con `asyncio.sleep` parcheado); `app.state.redis = None` (tests individuales configuran FakeRedis cuando necesitan)

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

### Cloud Build falla con `TypeError: unsupported operand type(s) for |: 'function' and 'NoneType'`
**Descubierto**: 2026-03-20 durante el primer build con módulos RAG.
**Causa**: `rag.py` usaba `chromadb.HttpClient | None = None` como type hint en parámetros
de función. `chromadb.HttpClient` es una función factory, no una clase, así que no tiene
`__or__` definido y la sintaxis `X | None` (PEP 604) falla en runtime en Python 3.11.
Tipos built-in (`str`, `dict`, `int`) y clases reales sí soportan `|` en 3.10+.
**Solución**: añadir `from __future__ import annotations` al inicio de `rag.py`.
Esto hace que todos los type hints se evalúen como strings (lazy), evitando la ejecución
del operador `|` en runtime. Corregido en commit `5f64b61`.

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
