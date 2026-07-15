---
fecha: 2026-07-03
slug: fixes-altas-f01-f04-ci
promoted: true
---

## Objetivo
Sesión larga (varias microtasks encadenadas, plan aprobado en /start): cerrar los
hallazgos Alta pendientes de la review senior que son testeables con mocks —
test F-05 (deuda de ayer), F-01 cooldown por workload (diseño ya cerrado), F-04
timeout Mattermost — y, a petición de Jay a mitad de sesión, dejar implementado
CI antes de la demo. Cierre con /promote (~8 bitácoras `promoted: true`).

## Hecho
- **Test F-05** (`tests/test_streams.py`, +2): la compensación de la dedup-key
  quedó cubierta — `xadd` que lanza → assert `delete("aiops:seen:<fp>")` awaited
  + excepción propagada; variante con el delete fallando también → warning
  ("compensation failed") + propaga igual. Docstring del módulo actualizado.
- **F-01 — cooldown por workload** (implementación del diseño cerrado ayer):
  - `config.py`: `remediation_cooldown_seconds: int = 600` (> ventana rollback 300s).
  - `remediation.py`: `acquire_workload_cooldown()` (SET `aiops:cooldown:{ns}/{name}`
    NX EX) + gate en `process_remediation` — solo rama auto estructurada (la única
    que patchea), tras la síntesis del `structured_command` y antes de ejecutar.
    Bloqueado o error de Redis → downgrade a ESCALATE (`reason_code=workload_cooldown`,
    fail-closed); `redis_client=None` → sin gate (tests/local).
  - `main.py`: `_process_alert_with_diagnosis` pasa su `redis_client` a
    `process_remediation` (firma nueva: tercer kwarg).
  - Tests (`test_remediation.py`, +6, clase `TestWorkloadCooldown`): adquiere→AUTO
    (key/nx/ex correctos), bloqueado→ESCALATE (sin ejecución, `structured_command`
    presente para el humano), error Redis→ESCALATE, None→pasa, rama no-estructurada
    no consulta el cooldown, helper key+TTL.
- **F-04 — timeout dedicado Mattermost**: `mattermost_timeout: float = 10.0` en
  config; `_post_with_retry` lo usa en vez de heredar `http_timeout` (300s, tamaño
  LLM). +1 test que fija el contrato (el cliente se construye con el timeout chico).
- **CI (petición nueva de Jay)**: `.github/workflows/ci.yml` — job `tests` (python
  3.11 + pip cache + mismo install/pytest que el paso 1 de cloudbuild.yaml) + job
  `docker-build` (valida `docker build ./agent`, sin push). Se activa al pushear.
- Verificación estática: `py_compile` OK en todos los ficheros tocados; YAML del
  workflow validado. Suite estimada ~544 (~535 + 9 nuevos) — **pytest lo corre Jay**.

## Encontrado / gotchas
- La regla 2 de `decide_action` (`sin comandos → SUGGEST_ONLY`) pilla ANTES que el
  camino estructurado: una fixture con `commands: []` nunca llega al cooldown.
  Los tests estructurados necesitan al menos un comando investigativo (mismo
  patrón que `TestStructuredAutoRemediation`). Ojo si algún día el modelo emite
  proposed_action válida con commands vacíos: hoy eso NO auto-remedia.
- El gate del cooldown corre también en dry-run: un dry-run "consume" la ventana
  del workload. Aceptado — es justo lo que el dry-run ensaya; no se añadió carve-out.
- `cloudbuild.yaml` ya era un CI de facto (tests como gate) pero solo corre cuando
  Jay lanza `gcloud builds submit` a mano; no había nada en push/PR. El repo tiene
  remote GitHub (`arturofernandezg/llm-kubernetes-ollama` vía `github-work`), sin
  `.github/` previo.

## Decisiones + por qué
- **F-01 tal cual el diseño de ayer, sin ampliaciones**: no se siembra cooldown en
  el approve humano (pendiente explícito, "puede esperar") y no hay marker en el
  result dict — el downgrade se observa por `action=ESCALATE` + log con
  `reason_code=workload_cooldown`. Menos superficie, mismo contrato testeable.
- **Gate DESPUÉS de `decide_action` y de la síntesis, no dentro de `decide_action`**:
  `decide_action` es puro/síncrono (9 reglas sin I/O) y se testea así; el cooldown
  es I/O contra Redis. Meterlo en el entry-point async mantiene esa pureza y solo
  toca la rama que de verdad patchea.
- **Fail-closed en error de Redis** (vs fail-open del resto del sistema): si el
  cooldown no es verificable, un segundo patch sobre un workload que quizá no curó
  el primero es exactamente el patch-storm que F-01 corta. Además, con Redis caído
  la cola tampoco entrega — el caso real es un hiccup transitorio.
- **CI = GitHub Actions espejo del gate de Cloud Build, no reemplazo**: sin
  credenciales GCP en GitHub, el push de imagen sigue en Cloud Build (manual, Jay).
  El workflow da la señal barata en cada push/PR (tests + Dockerfile buildea) —
  "un check rojo aquí = el gate de Cloud Build habría fallado". Mismo python-slim
  3.11, mismo comando pytest, para que la equivalencia sea literal.
- **Orden de sesión alterado al final**: el análisis RAG pedido a mitad de sesión
  se hace ANTES de /promote para que esta bitácora lo capture y se promueva todo
  en un ciclo.

## Siguiente
- **Jay**: `python3 -m pytest agent/tests/ -q` y, si verde, commit único de
  hardening (guard secrets + F-05 + F-02 de ayer, ya en working tree, + F-01 +
  F-04 + CI de hoy). Sugerencia:
  `git add -A && git commit -m "feat(hardening): F-01 cooldown por workload + F-04 mattermost timeout + F-05/F-02 fixes review + guard secrets-setup + CI GitHub Actions"`
  — el primer push a main activa el workflow de CI (verificar el check verde en GitHub).
- **Sigue pendiente de ayer (cluster, manual)**: recuperación de secrets (printenv
  del pod viejo ANTES de reiniciar), `kubectl apply` prometheus.yaml + reload,
  `set image` al commit nuevo, chaos OOM del arco completo, Gate 8 screenshots.
- **H1 restante de la review**: F-03 (chroma vía `asyncio.to_thread`), F-06
  (AOF+PVC Redis o degradar el claim), F-11 (incident_id desde la ingesta),
  F-17 (logs+events en el prompt del LLM).
- Decidir si el approve humano siembra el cooldown (coherencia humano/auto).
- Mejoras RAG: implementar R1 (filtro por metadata con fallback) como primera
  slice — ver análisis abajo.

## Análisis — formas de mejorar el RAG (pedido de Jay, 2026-07-03)

Base: `rag.py` + `docs/10` (p@1 73.3%, p@3 86.7% N=15; distancias comprimidas
0.22–0.30; misses = confusiones entre clases próximas: imagepull↔podnotready,
oom↔highmemory). Harness de medición ya existe (`eval_retrieval.py` + ground
truth) → cada mejora es medible en minutos, sin cluster para el análisis.

Priorizado (postura, no menú):

- **R1 — Retrieval guiado por metadata (barato, el que más mueve p@1)**:
  `retrieve_context` YA acepta `metadata_filter` pero producción nunca lo pasa
  (main.py:793). Los 4 misses son confusiones de clase y el alertname de la
  alerta ya nombra la clase → mapear alertname→`error_class` y hacer two-stage:
  query filtrada por clase + fallback/merge sin filtro (para alertas de clase
  desconocida). Coherente con v2: determinista donde se pueda, semántico como
  fallback. Esperable: p@1 ≈ 100% en clases conocidas, medible con el eval.
- **R2 — Cerrar el bucle de aprendizaje (el F4 real del roadmap)**: hoy
  `ingest_incident` escribe el incidente EN el momento del diagnóstico, con
  `outcome = action` — antes de saber si el fix curó. Un auto_remediate que
  luego hizo rollback queda archivado como precedente "bueno" y el retrieval
  lo citará. Fix: re-upsert del mismo doc_id tras `evaluate_rollback` con el
  outcome final (cured / rolled_back / approved) y exponer ese outcome en el
  contexto del prompt (o filtrar rolled_back del retrieval). Convierte la
  colección incidents de pasivo a activo — y es LA feature narrable en el
  chapter ("el sistema aprende de sus propios incidentes, incluidos los fallidos").
- **R3 — Higiene de query (micro)**: `build_rag_query` mete nombres de pod con
  hash (`frontend-8d7c4b-xnq2p`) y namespace — ruido para el embedding que
  empuja hacia vecinos equivocados con las descripciones pobres (los 2 misses
  duros son descripciones mínimas). Strip del hash + medir antes/después.
- **R4 — Métrica feedback-loop gain**: re-correr eval_retrieval con incidentes
  reales poblados vs colección vacía (ya listado como trabajo futuro en docs/10)
  — gráfica para el chapter si R2 entra.
- **Descartado/aplazado**: fine-tuning de embeddings, modelo embed mayor,
  reranker cross-encoder — coste >> valor con 16 runbooks; R1 llega antes al
  mismo sitio por vía determinista. Threshold de similarity: inútil con las
  distancias comprimidas (medido en docs/10).

Orden recomendado: R1 (slice de ~1 sesión, cierra los misses) → R3 (micro,
mismo fichero) → R2 (slice mayor, toca main+rollback; la feature de demo) →
R4 (medición para el deck).
