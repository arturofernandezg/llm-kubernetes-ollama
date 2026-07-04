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
| `test_endpoints.py` | 127 | Integración | Health probes (`TestReadyzQueueMode` ×3: readyz Redis-gated), /extract end-to-end, retry con backoff, /webhook/alert (encola, fail-closed 503 si Redis None), /metrics, formatter, feedback loop, `/webhook/action` (FakeRedis, approve/reject/HMAC + **fail-closed/fail-open gated por dry_run** ×2, v2 P0·2), fallback isolation, `TestWebhookQueuePath` (×4) + `TestHandleStreamEntry` + `TestPeriodicReclaim`, `TestDiagnosisTimeout` (×4), `TestEscalationStoreMetric` (×2, PR-06), `TestRagReconnect` (×2, PR-05), slash `/webhook/command` fail-closed (×1, v2 P0·2) |
| `test_extraction.py` | 11 | Unitario | extract_json: direct, markdown_block, regex con bracket counting, nested JSON, edge cases |
| `test_tf_generator.py` | 16 | Unitario | safe_name, generate_terraform (template, defaults, labels) |
| `test_validation.py` | 6 | Unitario | validate_params: regiones, instance types, campos null |
| `test_mattermost.py` | 14 | Unitario | send_mattermost_alert: envío, retry 5xx, no-retry 4xx, ConnectError, excepción inesperada, fail-open; make_hmac_token; timeout dedicado `mattermost_timeout` (F-04, no hereda el `http_timeout` del LLM) |
| `test_rag.py` | 69 | Unitario | build_rag_query, generate_embedding (incl. guard ValueError), retrieve_context, ingest_runbook, load_runbooks_from_dir, ingest_all_runbooks, build_incident_document, ingest_incident, **R1: error_class_for_alertname + retrieval two-stage con metadata filter** (ChromaDB + Ollama mockeados). **R3**: `TestStripPodHash` (×10, higiene de query). **R2·3**: `TestIncidentWorthIngesting` (×5, gate E4), `TestIncidentsOutcomeFilter` (×4, filtro `$ne auto_pending` sin fallback, `rolled_back` recuperable). **F-03**: `TestChromaOffloading` (×2, spy sobre `rag.asyncio.to_thread` — verifica offload + resultado intacto) |
| `test_diagnosis.py` | 29 | Unitario | build_alert_text, format_context_docs, generate_diagnosis, _clamp (LLM mockeado); `test_generation_uses_configured_temperature` (temp=0 viaja en `options.temperature`); `TestFormatClusterFacts` (×5, cluster facts fail-soft, incl. `restart_count=0` = hecho real) + 2 de integración (snapshot entra en el prompt / sin snapshot prompt limpio). **R2·3** (×5): labeling `cured`/`rolled_back`/`rollback_failed` en `format_context_docs`, runbook sin outcome intacto, guard del contrato `DIAGNOSIS_PROMPT` (label y regla `FAILED FIX` evolucionan juntos) |
| `test_remediation.py` | 222 | Unitario | classify_command (SAFE/MUTATING/BLOCKED/UNKNOWN), validate_commands, decide_action (9 reglas incl. 4.5/4.6 + 7.5 `rag_degraded`→escalate, PR-04), execute_commands (dry-run + real mode mock), process_remediation, _get_safe_commands, zero_current_memory. **F3**: `TestParseCpuToMillicores`, `TestDecideActionCpu` (escalate-first), `TestDecideActionCpuAuto` (flag on/off), `TestLimitResource`, `TestCapturePrePatchValue`/`TestRevertPatch` cpu (jsonpath/`--limits=cpu` field-aware), `TestProcessRemediationCpuAuto` (enganche E2E determinista), `TestStructuredAutoRemediation` (re-sourcing: risk=high+conf≥0.8+proposed_action → AUTO por vía estructurada; guardrail solo-subir + namespace ajeno). **v2 Eje A + hardening**: `TestSealProposedAction` (sella identidad/valor/match_labels; anula PA si workload sin confirmar o kind≠Deployment F-02; síntesis `new_value=2×current`), `TestDeriveConfidence`/`TestGroundConfidence` (confidence grounded + `model_confidence`), regla 4.7 `target_unresolved`, selector real en capture/revert. **F-01**: `TestWorkloadCooldown` (×6: adquiere→AUTO con key/nx/ex correctos, bloqueado→ESCALATE sin ejecución, Redis error→ESCALATE fail-closed, `redis_client=None`→pasa, rama no estructurada no consulta, helper key+TTL) |
| `test_ingest_runbooks.py` | 3 | Unitario | CLI `ingest_runbooks.run()`: exit 0 sin errores, exit 1 con errores, runbooks_dir correcto (mocks `ingest_all_runbooks`) |
| `test_utils.py` | 5 | Unitario | `backoff_delay()`: primer intento = base, crecimiento exponencial, cap en max, base custom, edge case max == computed |
| `test_escalation_store.py` | 15 | Unitario | `store_escalation`, `get_escalation`, `delete_escalation`, `count_escalations` con FakeRedis; fail-open con Redis caído; flujo completo store→get→delete |
| `test_evaluation.py` | 23 | Unitario | Evaluación del retrieval RAG (p@1/p@3, safety vs zero-shot); soporte de `docs/10` |
| `test_enrichment.py` | 25 | Unitario | Grounding v2 (Eje A): `_kubectl_json` (fail-soft: rc≠0/timeout/JSON inválido/excepción → None, `proc.kill()` en timeout), parseo del pod (limits/phase/restart_count/last_state_reason), selección determinista de container, `_controller_owner`, `_resolve_workload` (cadena pod→RS→Deployment, gate de existencia, STS/DS directo) |
| `test_rollback.py` | 29 | Unitario | Rollback post-patch: scheduling, `_evaluate_rollback` (healthy/revert), wiring en `_process_alert_with_diagnosis`. **v2 P0·3 (+11)**: `TestRollbackSerialization` (round-trip ↔ dict + json), `TestRollbackStore` (`rollback_store.py`: store/delete/list + fail-open con FakeRedis), `TestRollbackDurability` (persiste en Redis, borra al evaluar, `_recover_rollbacks` re-arma, idempotencia, no-op sin Redis). **R2 (+5)**: `TestRollbackVerdictReupsert` (cured/rolled_back/rollback_failed re-upsertan el mismo `doc_id` con el outcome; back-compat sin `doc_id` no re-upserta pero completa; fail-open si `ingest_incident` revienta) |
| `test_streams.py` | 21 | Unitario | Cola Redis Streams (F2, `AsyncMock`): `enqueue_alert` (dedup SETNX, XADD, fail-closed; **F-05 ×2**: XADD fallido borra la dedup-key y propaga / delete fallido → warning y propaga igual), `ensure_group` (BUSYGROUP idempotente, default `id="0"`), `consume_loop` (XREADGROUP, XACK, handler-fail-sin-ack, CancelledError corta el loop; **`TestConsumeLoop` self-heal NOGROUP**: recrea grupo con `id="$"` + backoff sin busy-spin, error genérico solo backoff, reset del contador tras éxito), `TestReclaimPending` (noop / reprocesa+ack / dead-letter poison / handler-fail / depth gauge) |
| **Total** | **620** | | 615 funciones `def test_` en **15 ficheros** (conteo `grep -c "def test_"` 2026-07-04); **pytest colecta 620** (parametrize expande ~5). **Jay-confirmado 620 verde 2026-07-04** (recuento reconciliado — los docs venían diciendo ~573/613). Incluye el bloque RAG F4 (R2/R2·3/R3 + gate E4), F-01/02/04/05 y F-03. Corridos en el gate de Cloud Build de `8a40fdc` (desplegado). |

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
- F2 cola Redis Streams (2026-06-26/29): nuevo `test_streams.py` (~16) + clases de cola en `test_endpoints.py` (`TestWebhookQueuePath`, `TestHandleStreamEntry`, `TestPeriodicReclaim`, `TestReadyzQueueMode`). Slices 1-3 llevaron la suite a **427**. El retiro del legacy (2026-06-29) borró `TestInFlightDedup` (×4) y los readyz legacy de Ollama (×3) y colapsó las ramas duplicadas del webhook → neto −8 en `test_endpoints.py` (120→112). **Total 419**. Mocking con `AsyncMock` (no `FakeRedis`: no soporta operaciones de stream).
- F2 pulido post-cierre (2026-06-29): +3 tests en `test_streams.py::TestConsumeLoop` para el self-heal ante NOGROUP (recrea grupo con `id="$"` + backoff / error genérico solo backoff / reset del contador de fallos tras éxito). **Total 422**.
- F3 remediación CPU + re-sourcing (2026-06-30/07-01): +38 en `test_remediation.py` (137→175) a lo largo del arco — slice 1 (`TestParseCpuToMillicores` +7, `TestDecideActionCpu` +7), slice 1b (`TestLimitResource` +5, capture/revert cpu +3), slice 2 (`TestDecideActionCpuAuto` +6), integtest (`TestProcessRemediationCpuAuto` +3), re-sourcing (`TestStructuredAutoRemediation` +6, +2 guardrail namespace; **4 tests de seguridad invertidos** deliberadamente al contrato "drop risk / keep conf≥0.8" — renombrados, no borrados). **Total 460** (Jay-confirmado, full suite). +1 test de temp=0 staged en `test_diagnosis.py` (pytest pendiente → 461).
- v2 Eje B — confianza (2026-07-01): **P0·2** +3 en `test_endpoints.py` (callback fail-closed/fail-open gated por `dry_run`; slash `/webhook/command` fail-closed). **P0·3** +11 en `test_rollback.py` (nuevo módulo `rollback_store.py` testeado con FakeRedis: serialización round-trip, store/delete/list fail-open, durabilidad schedule→persist / evaluate→delete / `_recover_rollbacks` re-arma / idempotencia / no-op sin Redis). Warnings "coroutine never awaited" silenciados (el mock de `create_task` cierra la coroutine). **Total 474** (Jay-confirmado: `test_endpoints -k` 10 passed, `test_rollback.py` 22 passed).
- v2 Eje A — grounding (2026-07-02): nuevo `test_enrichment.py` (25) + `TestSealProposedAction`/`TestDeriveConfidence`/`TestGroundConfidence`/`TestFormatClusterFacts` + selector real en capture/revert + temp=0. Gotcha cazado por los tests: `extra={"args":...}` revienta el logger (`args` reservado en `LogRecord`) → `cmd_args`. Suite → ~505 (Jay: remediation+enrichment 205 passed; +diagnosis 245 passed).
- v2 hardening post-review (2026-07-02, en `cb2d1db`): +30 — regla 4.7 target fantasma, síntesis `new_value` 2×, paridad humano/auto (escalación estructurada guarda el comando determinista; approve programa rollback), métrica `aiops_enrichment_total`, confidence grounded visible en MM. `test_remediation.py` +18, `test_endpoints.py` +11, `test_enrichment.py` +1. Suite → ~535 (verde en el gate de Cloud Build).
- Review senior fixes Alta (2026-07-02/03): **F-02** +1 `test_non_deployment_kind_drops_action` (STS/DS/None parametrizado) + 3 fixtures del seal actualizados a `workload_kind="Deployment"` explícito. **F-05** +2 en `test_streams.py` (compensación dedup-key). **F-01** +6 `TestWorkloadCooldown`. **F-04** +1 timeout dedicado MM. **Total 565** (pytest de los ~9 últimos pendiente por Jay). Gotcha F-01: la regla 2 de `decide_action` (sin comandos → SUGGEST_ONLY) corre ANTES del camino estructurado — fixtures estructuradas necesitan ≥1 comando investigativo.
- F4 R1 retrieval por metadata (2026-07-03): +8 en `test_rag.py` — `TestErrorClassForAlertname` (prefijo `KubePod`, identidad, desconocido, vacío, shape del filtro) + `TestMetadataFilteredRetrieval` (el filtro llega a `.query(where=...)`, fallback a semántico cuando no matchea, no-fallback cuando sí). **Total 573** (Jay-confirmado, pytest verde).

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
