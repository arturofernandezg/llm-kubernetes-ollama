# Sesion 2026-03-30-S3 — Integrar RAG en el Webhook

## Objetivo

Cerrar el pipeline completo: cuando llega una alerta firing, el webhook llama a RAG + LLM para generar un diagnostico y lo envia formateado a Mattermost.

## Plan

### 1. ChromaDB client en lifespan
- Crear client en startup, guardarlo en `app.state.chroma_client`
- Fail-open: si ChromaDB no esta disponible, `chroma_client = None`

### 2. Background task `_process_alert_with_diagnosis()`
- Pipeline: `build_rag_query()` → `retrieve_context()` → `generate_diagnosis()` → format → Mattermost
- Triple fail-open: ChromaDB down → empty context. Ollama down → no diagnosis. Both → raw alert.

### 3. Formatter `_format_diagnosis_message()`
- Alert header + diagnosis + commands + confidence + sources
- Fallback si diagnosis es None

### 4. Actualizar webhook handler
- `firing` → diagnosis pipeline (background)
- `resolved` → mensaje simple (background)

### 5. Prometheus counter para diagnosticos

### 6. Tests (~8 nuevos)
- Webhook con diagnosis mock
- Resolved skips diagnosis
- RAG failure → still works
- Ollama failure → fallback
- Full pipeline failure → raw alert
- Format function tests

---

## Progreso

### Completado

1. **ChromaDB client en lifespan** — `app.state.chroma_client` con fail-open (None si no disponible).
2. **`_process_alert_with_diagnosis()`** — Background task con triple fail-open:
   - ChromaDB down → contexto vacío, sigue con diagnosis.
   - Ollama down → diagnosis=None, envía fallback.
   - Ambos down → envía alerta raw a Mattermost.
3. **`_format_diagnosis_message()`** — Markdown formateado: header con icono+severity, diagnosis, risk/confidence%, comandos en code block, explanation, RAG sources.
4. **Webhook handler actualizado** — `firing` → pipeline RAG+diagnosis (background). `resolved` → mensaje simple.
5. **Prometheus counter** — `aiops_diagnosis_total` con labels: `success`, `rag_ok`, `rag_failed`, `llm_failed`, `pipeline_failed`.
6. **Mock helpers** en `tests/helpers.py`: `mock_chroma_client()`, `mock_rag_context()`, `mock_diagnosis_result()`.
7. **9 tests nuevos** en `test_endpoints.py`:
   - `TestWebhookWithDiagnosis` (5 tests): firing triggers diagnosis, resolved skips diagnosis, RAG failure fallback, Ollama failure fallback, full pipeline failure.
   - `TestFormatDiagnosisMessage` (4 tests): full diagnosis, no diagnosis, empty commands, resolved alert.
8. **Fix conftest.py** — `app.state.chroma_client = None` (TestClient no ejecuta lifespan).

### Resultado

- **124/124 tests passing** (era 114 antes de esta sesión, +10 nuevos incluyendo el fix de conftest).
- Pipeline completo wired: alerta → RAG → LLM → Mattermost (con mocks).

