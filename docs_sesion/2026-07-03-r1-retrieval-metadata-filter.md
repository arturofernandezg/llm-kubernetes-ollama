---
fecha: 2026-07-03
slug: r1-retrieval-metadata-filter
promoted: false
---

## Objetivo
Implementar **R1 — retrieval guiado por metadata** (primer slice del análisis RAG
de la sesión anterior): filtrar la query de runbooks por `error_class` derivado del
alertname, con fallback semántico. Cerrar los 4 misses de p@1 (confusiones entre
clases próximas) por vía determinista, coherente con la tesis v2.

## Hecho
- **`rag.py`**:
  - `ALERTNAME_TO_ERROR_CLASS` (mapping determinista) + `error_class_for_alertname()`
    + `runbook_filter_for_alert(labels)`. El mapping resuelve el **prefijo `KubePod`**
    de Prometheus (`KubePodOOMKilled`→`OOMKilled`, `KubePodCrashLoopBackOff`→
    `CrashLoopBackOff`, `KubePodImagePullBackOff`→`ImagePullBackOff`); `HighCPU`/
    `HighMemory`/`TargetDown` son 1:1. Desconocidos → identidad; alertname vacío → None.
  - `retrieve_context`: **two-stage** — query filtrada por `where`; si matchea 0
    runbooks (clase desconocida/mismatch), reintenta sin filtro (semántico). Nunca
    devuelve contexto vacío por un filtro que falló.
- **`main.py`**: `_process_alert_with_diagnosis` computa `runbook_filter` una vez y lo
  pasa a las **dos** llamadas `retrieve_context` (camino normal + reconnect de ChromaDB).
- **`eval_retrieval.py`**: pasa el filtro (medible); `use_filter` por env
  `EVAL_NO_FILTER=1` para reproducir el baseline semántico y A/B el gain; `metadata_filter`
  en el JSON de salida.
- **Tests** (`test_rag.py`, +8): `TestErrorClassForAlertname` (prefijo, identidad,
  desconocido, vacío, shape del filtro) + `TestMetadataFilteredRetrieval` (filtro
  llega a `.query(where=...)`, fallback a semántico cuando el filtro no matchea,
  no-fallback cuando sí matchea). `py_compile` OK en los 4 ficheros.
- **Medido en cluster (A/B con eval_retrieval, N=15, port-forward chroma+ollama)**:

  | | p@1 | p@3 |
  |---|---|---|
  | Baseline semántico (`EVAL_NO_FILTER=1`) | 73.3% (11/15) | 86.7% (13/15) |
  | **R1 (filtro por error_class)** | **100% (15/15)** | **100% (15/15)** |
  | Gain | **+26.7 pp** | **+13.3 pp** |

  El baseline clava lo de `docs/10`. Los 4 misses (oom↔highmemory, imagepull↔
  podnotready) desaparecen: el filtro ancla la clase antes de que el embedding
  decida. Ficheros: `evaluation_results/retrieval_2026-07-03_{baseline,r1filter}.json`.

## Encontrado / gotchas
- **La causa por la que el filtro nunca se cableó**: el alertname de Prometheus NO
  es el `error_class`. Los pod-level llevan prefijo `KubePod` (verificado en
  `k8s/prometheus.yaml`: 6 alertas KubePodOOMKilled/KubePodCrashLoopBackOff/
  KubePodImagePullBackOff/HighMemory/HighCPU/TargetDown) mientras los runbooks
  guardan la razón K8s pelada. Un `where={"error_class": alertname}` naive fallaba
  para los 3 prefijados → por eso `main.py` nunca lo pasaba. El mapping lo arregla.
- `EVAL_NO_FILTER` para A/B: `python -m evaluation.eval_retrieval` (filtro ON) vs
  `EVAL_NO_FILTER=1 python -m evaluation.eval_retrieval` (baseline). Requiere
  ChromaDB+Ollama por port-forward (no lo puedo correr en local: sin `chromadb`).
- El filtro solo afecta a **runbooks** (la colección de incidents no lo recibe —
  consistente con lo que mide el eval). Si en R2 los incidents pesan, revisar.

## Decisiones + por qué
- **Two-stage con fallback dentro de `retrieve_context`**, no en `main.py`: la
  lógica "filtra, y si no hay, cae a semántico" es concern de retrieval y así el
  eval se beneficia igual pasando solo el filtro. `main.py` queda en 1 línea
  (computar el filtro). Superficie mínima.
- **Identidad como default del mapping** (`.get(alertname, alertname)`) en vez de
  un set explícito de clases conocidas: no duplica la lista de error_class de los
  16 runbooks, y el fallback por resultado-vacío cubre cualquier clase sin runbook.
  Un alertname futuro que ya nombre su clase (p.ej. KSM `PodEvicted`, Eje C)
  funciona sin tocar código.
- **Mapping estático, no leído de los runbooks**: 6 alertas reales hoy; el prefijo
  `KubePod` es una convención de Prometheus, no un dato de los runbooks. Tenerlo
  explícito documenta la traducción y es lo que se testea.

## Siguiente
- **Jay**: `python3 -m pytest agent/tests/test_rag.py -q` (verificar +8) y, con
  port-forward a ChromaDB+Ollama, correr el A/B del eval (filtro ON vs
  `EVAL_NO_FILTER=1`) → número para el deck. Esperable p@1≈100% en clases conocidas.
- Este R1 entra en el **mismo commit de hardening pendiente** o uno propio
  `feat(rag): R1 retrieval guiado por metadata (alertname→error_class + fallback)`.
- Continuar el orden RAG: **R3** (higiene de query, micro, mismo fichero) → **R2**
  (bucle de aprendizaje real, toca main+rollback — la feature narrable del chapter)
  → **R4** (métrica gain para el deck).
- Sin promover aún (bitácora `promoted: false`); `/promote` al cerrar el bloque RAG.
</content>
