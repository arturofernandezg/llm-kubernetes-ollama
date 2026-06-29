---
fecha: 2026-06-25
slug: production-readiness-f1
promoted: true
---

## Objetivo
Arrancar la Fase F1 (validación en cluster) **sin tocar cluster**: diseñar el protocolo de validación, y de paso analizar el código del pipeline para sacar hallazgos de production-readiness honestos antes de provocar nada. Luego atacar el primer quick-win de código (PR-04). "Despacio y bien."

## Hecho
- **Análisis del pipeline real** (no de la doc): leídos `main.py` (webhook, dedup, `_process_alert_with_diagnosis`, lifespan), `diagnosis.py`, `escalation_store.py`, `config.py`, `rag.py`. Mapeados los 4 caminos de fail-open (ChromaDB / Ollama / Redis / pipeline) y el dedup in-flight de FASE 2.
- **`docs/14-production-readiness.md`** (nuevo, entregable de F1):
  - §1 Protocolo: matriz E1–E6 (inyección one-liner · fail-open esperado · señal · veredicto pendiente).
  - §2 Hallazgos PR-01..PR-07, marcados como **hipótesis a verificar** (no hechos), con evidencia + línea de código.
  - §3 Plan de soluciones (finding→fix→esfuerzo→cuándo + orden sugerido).
- **`docs/11-quality-backlog.md`**: sección nueva "Production-readiness F1" con los 4 accionables en código (PR-01, PR-04, PR-05, PR-06) en estado `TODO`.
- **`CLAUDE.md`**: línea de `docs/14` añadida al índice de docs.
- **PR-04 implementado y verificado** (quick-win de seguridad):
  - `remediation.py`: nueva **regla 7.5** en `decide_action(diagnosis, validations, rag_degraded=False)` — intercepta el `AUTO_REMEDIATE` final y lo baja a `ESCALATE` si `rag_degraded`. `process_remediation(diagnosis, rag_degraded=False)` propaga el flag. Ambos params con default → cero rotura de llamadores.
  - `main.py` `_process_alert_with_diagnosis`: marca `rag_degraded=True` en el `except` del retrieval (ChromaDB inalcanzable) y lo pasa a `process_remediation`.
  - `tests/test_remediation.py`: 3 tests (`rag_degraded` baja AUTO→ESCALATE; no toca SUGGEST_ONLY; integración escala con `safe_commands` poblados para botones).
  - **137 passed** (Jay corrió pytest). Backlog PR-04 → DONE.

## Encontrado / gotchas
- **`docs/12` ya estaba ocupado** (chaos-engineering) — el índice de `CLAUDE.md` no lo listaba (tabla algo stale: faltan 09 retirado, 10, 12). El informe F1 fue a `docs/14`.
- **PR-01 ya estaba medio resuelto**: la sesión E2E (backlog finding E1, 2026-05-26, imagen `5aaf9f9`) **ya subió `HTTP_TIMEOUT` a 300** en el manifiesto. El residuo es solo que el default de `config.py` sigue en 120 → reproducibilidad local rota. Bajado de 🔴 a 🟡.
- **PR-04 tiene refuerzo empírico en el propio backlog**: finding E5 ya observó **sobreconfianza 95–98% con razonamiento incorrecto** en qwen2.5:1.5b. → confiar en `confidence` sin grounding RAG es doblemente arriesgado.
- **PR-02 (readiness gatea Ollama)**: si Ollama cae del todo, el pod queda `NotReady` y Alertmanager no entrega → el "fail-open de LLM" solo se ve en una ventana estrecha (caída mid-flight). Hay que narrarlo con precisión y diseñar E2 con esto en cuenta.
- **PR-03 (dedup per-pod en memoria)**: `IN_FLIGHT_ALERTS` es un `set` del proceso → F3 (HPA) lo rompería al escalar el agente. F1 descubre un constraint que condiciona F3.
- **Venv del proyecto está en `agent/.venv`** (Python 3.11, con pytest). Gotcha: Jay corrió `python3 -m pytest` con el python del sistema (3.14, sin pytest) → `No module named pytest`. Solución: `cd agent && source .venv/bin/activate && pytest ...` o `.venv/bin/pytest ...`.
- Al implementar PR-04 confirmé que el callback de aprobación (`/webhook/action`) usa `execute_commands` directo, **no** `process_remediation` → una escalación degradada que el operador aprueba sí ejecuta (correcto: decisión humana explícita).
- Warning preexistente y ajeno en `TestExecuteCommandsRealMode` (coroutine never awaited) — no introducido por PR-04, no bloquea.

## Decisiones + por qué
- **Empezar F1 por el código, no por el cluster**: leer el pipeline produjo 7 hallazgos honestos antes de provocar nada — el informe de production-readiness empieza aquí. Encaja con "construir despacio y bien" y "docs reflect reality, not ambition".
- **Hallazgos como hipótesis, no hechos**: los veredictos quedan pendientes de cluster; se marca explícitamente para no confundir ambición con realidad.
- **Crear `docs/14` directamente** (no esperar a `/promote`): es un entregable nuevo que el roadmap ya nombra como salida de F1, no una promoción de hechos dispersos. Se mantiene honesto marcando lo pendiente.
- **Separar accionables-de-código (al backlog) de diseño/fase (solo en docs/14)**: PR-01/04/05/06 son quick-wins testeables con mocks → `11`. PR-02 (decisión readiness) y PR-03/PR-07 (→F2) no son filas de código.
- **Orden de ataque**: quick-wins de código sin cluster primero (PR-04 prioritario por seguridad + valor de demo), luego verificación en cluster de la matriz, luego F2 absorbe PR-03/PR-07 y la decisión de PR-02.
- **PR-04 — trigger conservador**: solo el `except` del retrieval (ChromaDB caído), **no** el retrieval vacío-pero-exitoso. El finding es sobre el *outage*; un retrieval vacío legítimo (alerta novel sin runbook) es otro caso (posible extensión futura). Evita cambiar el comportamiento de alertas sin match.
- **PR-04 — downgrade a ESCALATE, no SUGGEST_ONLY**: preserva `safe_commands` → human-in-the-loop con botones en vez de perder la acción. Narrativa de demo: "sin grounding no auto-actúo, te paso la decisión". El guard solo intercepta AUTO_REMEDIATE (SUGGEST_ONLY y ESCALATE existentes intactos).

## Siguiente
- **PR-04 ✅ hecho.** Siguiente quick-win: decidido empezar por **PR-06** (observabilidad: separar `outcome="llm_timeout"` vs `"llm_error"` + métrica `aiops_redis_up`) por valor de demo (Grafana muestra fallos diferenciados, no caja negra) — o **PR-05** (reconnect lazy de `chroma_client`) si se prefiere robustez. Pendiente de confirmar con Jay al retomar.
- Cuando Jay tenga sesión `kubectl`: ejecutar la matriz E1–E6 de `docs/14`, rellenar veredictos, confirmar/descartar hipótesis, capturar PR-01 (verificar HTTP_TIMEOUT desplegado) y PR-03 (nº de réplicas del agente).
- Pendiente real de siempre: **Gate 8** — screenshots Grafana.
