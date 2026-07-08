---
fecha: 2026-07-07
slug: promote-plan-chapter-deck-review
promoted: false
---

## Objetivo
Dos tramos: (1) **`/promote`** de las bitácoras pendientes para que la doc canónica refleje la
realidad antes de la presentación; (2) construir un **planner personal para Jay** (mentalización +
día a día + método de estudio) de cara al chapter, y hacer una **review del deck** (`demo/demo.html`)
para decidir una v2 actualizada.

## Hecho
- **`/promote` completo** (fuente: bitácoras `promoted:false`). El grep inicial dio 11 falsos
  positivos (menciones de "promoted: false" en el cuerpo); las **reales eran 3**: `2026-07-04-cured-run-r2-human-gap`,
  `2026-07-06-r2-human-fix-cured-validado`, `2026-07-07-skills-operativas`.
  - `docs/07-roadmap.md`: fecha chapter **8/14→15 julio**; imagen `8a40fdc→0914611`; tests **620→621**;
    **S3·b ✅ cerrado** (veredicto `cured` validado en cluster 07-06) + R4 desbloqueado; paridad
    R2-humano (`ca159be`) en el bloque ChatOps; entrada de changelog nueva (S3·b); **+2 modos de
    fallo** (horno nocturno, factibilidad≠seguridad) + confirmación del fix falso-rollback.
  - `CLAUDE.md`: estado (imagen/tests/fecha/S3·b + 3 lecciones del run); método → **3 de ciclo + 6
    operativas**; archivos clave (tests 621, skills, gotcha commit solo-manifiesto en "Imagen actual");
    "En curso/siguiente" reescrito (S4 R4 → matriz E1–E6 → promote+deck+ensayo → F-11/F-17/F-06).
  - `docs/06-testing.md`: Total **621**; 2 entradas de changelog (bloque RAG+F-03 reconciliado a 620,
    test paridad R2-humano `test_approve_feeds_verdict_loop_with_doc_id`).
  - `docs/11-quality-backlog.md`: **C-06** (gap R2-humano, ✅ DONE `ca159be`) y **C-07**
    (factibilidad≠seguridad → pre-flight `auth can-i`, TODO); C-01 mitigación confirmada en el run 07-06.
  - `docs/04-cicd-cloudbuild.md`: gotcha del commit solo-manifiesto (no rebuildear para cuadrar el tag).
  - `docs/12-chaos-engineering.md`: **sin cambios** — ya estaba al día (veredicto `cured` 07-06, horno
    nocturno, `chaos_arc.sh`, `--vm-hang 0`).
  - **Vault**: nodo proyecto `AIOps_Infra_Agent.md` (estado 07-06 + 3 patrones nuevos: acarrear
    contexto en la escalación por doc_id no reproducible / teardown = último gate / validation valida
    seguridad no factibilidad); `RAG_Feedback_Loops.md` (corolario "todo camino que actúa alimenta el
    bucle, incl. el humano"); `Command_Safety_Classification_for_LLM_Actions.md` (insight 10:
    feasibility ≠ safety).
  - 3 bitácoras marcadas `promoted:true`.
- **Review del deck** (`scripts/build_demo.py` + `demo/demo.html`): diagnóstico de staleness severa
  (ver gotchas). Propuesto arco de v2 (15 slides: thesis → grounding → motor dispone → learning loop
  → auditoría 5.9→7.1 → evidencia cluster). No ejecutado aún (pendiente OK de Jay).
- **Planner de presentación** entregado en texto (mentalización + día a día Mar 7→Mié 15 + método de
  estudio). Ofrecido como Artifact privado, pendiente de confirmar disponibilidad.

## Encontrado / gotchas
- **El deck está congelado en ~26 mayo** (`DATE="2026-05-26"`, `demo.html` mtime 28 may; `build_demo.py`
  tocado por última vez 29 jun en F2 pero el HTML no se regeneró). Es **pre-F2/F3/v2-grounding/auditoría/
  F4-RAG/validación-cluster/cured**. ~6 semanas del mejor trabajo sin reflejar.
- **Lo grave no es que falte, es que vende como abierto lo ya cerrado**: "escalaciones en memoria"
  (limitación) y "dedup in-flight" (próximo paso) están **resueltos por F2**; "limpiar RAG store"
  resuelto por gate E4. Presentar trabajo shipped como TODO resta credibilidad.
- **Números stale en el deck**: 369 tests (→621), p@1 60%/80% como headline (→73→100 con R1, falta la
  slide del salto). Framing "primera demo con tutor" (→presentación a chapter, TFM ya evaluado).
- **4 capturas reales sin usar** en `demo/`: `mattermost_escalation_grounded_confidence.png` (04-jul),
  `mattermost_cured.png` (06-jul), `grafana_overview_top.png`, `grafana_queue_row.png` (29-jun). El
  deck sigue apuntando a `memoria/demos/*` viejas.
- **El deck se genera de UN fichero** (`build_demo.py`, 419 líneas): la v2 es una reescritura acotada
  del contenido Python + re-run, no editar el HTML de 2.8MB (se sobreescribe). Andamiaje CSS/JS sólido
  y reutilizable.
- `demo.html` = 2.8MB en 235 líneas → todo son data-URIs base64 embebidos (autocontenido, offline,
  navegable por teclado). Bien de diseño; solo el contenido está viejo.

## Decisiones + por qué
- **Reframe mental del chapter como sala de pares, no tribunal**: el TFM ya está evaluado → el objetivo
  es credibilidad de ingeniería, no nota. La narrativa de la auditoría honesta (5.9→7.1, "encontré 3
  razones para rechazar mi propio sistema y las cerré") es el foso: madurez > features. Antídoto al
  nervio = ground truth (cada claim mapea a algo que el sistema HACE) + ensayar "no lo sé, lo comprobaría"
  sin vergüenza.
- **Planner front-load contenido / back-load ensayo**: deck v2 CONGELADO el viernes 10, fin de semana
  y lunes de ensayo cronometrado, víspera (14) en calma sin material nuevo. Regla de oro de presentar:
  no se cambia el contenido la víspera. Coherente con "construir despacio y bien".
- **La v2 del deck es reescritura de contenido, no retoque**: el gap es de 6 semanas y el andamiaje ya
  es bueno → tocar `build_demo.py` (arco + datos + imágenes), no el HTML generado.
- **Método de estudio = recuperación activa** (`/ensayo` como gimnasio, narrativa en 3 longitudes,
  tarjeta de números memorizada, 4 preguntas por componente) en vez de relectura del deck (falsa
  sensación de dominio).
- **No arrancar el deck v2 ni el Artifact sin OK de Jay** (Proposal→Validation→Execution): el arco y la
  disponibilidad son decisiones suyas; hoy solo se propone.

## Siguiente
1. **Decisión R4 (desbloquea la gráfica estrella)**: qué hacer con los ~30 docs HighCPU del horno
   nocturno en ChromaDB — filtrar en la query del eval (`INCIDENTS_RETRIEVAL_FILTER`/metadata) vs.
   limpiar la colección. Anotarlo como condición del run.
2. **Correr R4 en cluster** (Jay): `eval_retrieval` con incidents poblados (hay `cured`+`rolled_back`
   reales) vs vacío → feedback-loop gain → slide estrella del deck.
3. **Deck v2** sobre `build_demo.py`: arco nuevo (thesis "el cluster informa, el modelo razona, el motor
   dispone" → grounding Eje A → motor dispone + cap 4.6 → learning loop + `cured` → retrieval 73→100 →
   auditoría 5.9→7.1 → evidencia cluster con las 4 capturas reales → chaos reframe hardware → límites
   honestos incl. F-06 → roadmap post-chapter → cierre 621 tests). Actualizar `DATE`, `AUTHOR` framing,
   `IMAGES` a `demo/*.png`, `CHAOS_RESULTS` + añadir el arco `cured`. Regenerar + export PDF de backup.
4. **Actualizar `guion.html` + QA** del generador con las preguntas hostiles reales (review §9.3) y las
   respuestas ancladas a ground truth.
5. **Commit pendiente de Jay**: `git add -A && git commit` del `/promote` (docs + bump `deployment-agent.yaml`
   a `0914611` + bitácoras). Sin `Co-Authored-By`.
6. **(Opcional) Artifact del planner** (calendario + método + tarjeta de números + preguntas hostiles),
   tras confirmar disponibilidad real por día.
7. **Arrastrado**: matriz E1–E6 (`docs/14`); Gate 8 resto de screenshots Grafana; F-11/F-17; F-06
   (durabilidad Redis: AOF+PVC o degradar el claim en deck/docs — no reclamar durabilidad sobre memoria
   volátil delante del chapter).
