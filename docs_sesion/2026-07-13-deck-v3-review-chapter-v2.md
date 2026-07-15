---
fecha: 2026-07-13
slug: deck-v3-review-chapter-v2
promoted: true
---

## Objetivo
Review técnica hostil del deck v3 (modo "yo soy el chapter") contra ground truth (docs/10,
docs/12, docs/07, capturas PNG reales) + pasada v2 quirúrgica sobre `scripts/build_demo_v3.py`
para arreglar lo encontrado antes del ensayo S8 (14-jul) y el chapter (15-jul).

## Hecho
- **Review completa del deck** (fuente `build_demo_v3.py`, no el HTML): números de eval y chaos
  verificados contra docs/10 y docs/12 (cuadran); capturas Mattermost leídas visualmente.
- **Fixes aplicados** (ver Encontrado para el detalle de cada hallazgo):
  - B1: figcap grounding 65% → 95% (reformulado: el punto es la procedencia, no el delta).
  - B3: tabla chaos glosada — `MTTR` → `T. respuesta`, `conf.` → `conf. modelo`, línea de
    definiciones al pie (MTTD/T. respuesta/T_detect); nota de recámara en speaker notes
    (runs de mayo = sistema pre-grounding).
  - Tanda 3: fila 5ª en la tabla chaos "arco completo OOM → cured" (datos del run del replay,
    celdas sin métrica = "—"); vocabulario de confianza etiquetado en todo el deck
    (fundada vs del modelo).
  - M1: figcap cured explica que la captura dice `KubePodCrashLoopBackOff` (el OOM se
    manifiesta como crashloop de reinicios del mismo deployment).
  - M2: frase-escudo en slide 06 sobre el texto libre imperfecto del 1.5b (la captura repite
    el pod 2×) → convertido en munición de la tesis.
  - M3: cierre "100% comandos seguros" → "(N=15)".
  - M4: separador decimal unificado a coma (0,86 / 0,63).
  - QA: guion 16 → 23 preguntas (+7: prompt injection vía logs, tormenta de alertas/throughput,
    pod ya auto-curado a los 4 min, RBAC del agente comprometido, tensión runbook-sí/outcome-no,
    tests deterministas con LLM, estado de deploy de C-07/C-08/R5).
- **Regenerado** `demo/demo_v3.html` (2.7MB) + `guion_v3.html` (14KB) y verificado: grep de
  cadenas nuevas + ausencia de viejas (0 restos de "65 %", "0.86", "MTTR pipeline"), y revisión
  visual con Chrome headless (`--headless --screenshot`, hashes #7/#9/#12/#13/#14/#18/#21).
- **Overflow encontrado y corregido en la verificación visual** (el riesgo previsto): la slide
  grounding y la de chaos desbordaban el escenario 720px tras los añadidos. Fixes: claim de
  grounding acortado a "Antes de que el modelo hable, *habla el cluster*" (1 línea, más
  punchy), figcap comprimido a 1 línea, etiqueta de la fila del arco → "arco completo (replay)",
  definiciones y márgenes recortados. Re-verificado: todo dentro del papel.

- **Retoques pre-reu tutor (petición Jay)**: fila Durabilidad fuera de la slide de Límites
  (queda en R2 + QA 7; ver Siguiente), chip "~15 min + preguntas" fuera de la portada (y su CSS),
  y **logo MasOrange en la portada**: `demo/logo_empresa_light.png` derivado del JPG oficial con
  Pillow (venv de scratchpad) — fondo navy → transparente, "O" blanca → navy de marca, "+"
  naranja intacto; script en scratchpad `make_logo_light.py`. No hizo falta descargar nada.

## Encontrado
- **B1 (bloqueante, error factual visible)**: el figcap de slide 06 decía "frente al 65 % que
  declaraba el modelo" pero la captura embebida dice literalmente *"Confidence: 100% (grounded
  del cluster; el modelo dijo 95%)"*. docs/12 confirma `grounded=1.0, model=0.95` para el arco
  del 04-jul. Un chapter leyendo el screenshot lo caza en directo.
- **B2 (dependencia del ship de hoy)**: C-07/C-08/R5 se afirman en presente pero el cluster corre
  `0914611` (esas features viven solo en `588e3a9`, ship S7 = hoy). La slide 03 fija el listón
  "cada afirmación mapea a algo que el sistema hace hoy en un cluster real". Además la única
  captura de escalación muestra UN botón mientras slide 07 + QA prometen dos.
- **B3 (tabla chaos, 3 pinchazos)**: (a) "MTTR pipeline" con outcome=escalate — nada se remedió;
  (b) la columna `conf.` es la confianza DECLARADA POR EL MODELO (docs/12: "la confianza alta no
  implica razonamiento correcto"), dos slides después de predicar que esa confianza no gobierna;
  (c) `T_detect` sin definir, con 609s bajo el título "detección en segundos" (es `for:` + ramp
  de `rate[5m]`, config de alerting, no el agente). Bonus: los 4 runs son del 27-may,
  pre-grounding.
- **M1**: la captura del veredicto dice `KubePodCrashLoopBackOff` + "Remediation healthy"; el
  replay narra OOM→cured. Explicable (OOM → crashloop del mismo deployment) pero cross-ref en
  frío parece evidencia que no corresponde.
- **M2**: el diagnóstico de la captura repite el nombre del pod dos veces — verruga visible del
  1.5b, mejor exhibida como argumento que escondida.
- **M3**: "100% comandos seguros" en el cierre sin N (la nota honesta estaba solo en slide 10).
- **M4**: decimales mixtos (0.86 con punto, 0,95 con coma).
- **M5 (checklist, no deck)**: `STATS['tests']` lleva el comentario "re-confirmar verde antes de
  presentar" — pendiente de Jay antes del 15.
- **Huecos QA**: el deck *invita* a 6 preguntas que el guion no cubría (las 6 añadidas arriba);
  la más peligrosa: prompt injection vía logs/eventos (F-17), con respuesta buena que existía
  pero no estaba escrita.
- Lo que está bien y no se toca: tesis clara, notas de honestidad, replay (elimina riesgo de
  demo en vivo), capa de respaldo, timing ~15:00 clavado en las notas.

## Decisiones + por qué
- **Alcance completo incl. Tanda 3** (fila cured + vocabulario de confianza): cambios baratos en
  el mismo fichero; la tabla terminaba 4/4 sin remediar y la respuesta vivía solo en texto
  pequeño. Elegido por Jay.
- **B2 = confiar en el ship de hoy, sin hedge en slides**: el deploy S7 es hoy y las capturas
  C-08/R5 del plan cierran el gap con evidencia real; se añade solo una QA preparada en el guion
  redactada para valer en ambos escenarios (ship ok / contingencia en `0914611`). Elegido por
  Jay. Doble trabajo de hedge+revert descartado.
- **B1 se arregla cambiando 65→95, no quitando el dato**: narrativamente mejora — la separación
  de procedencia importa aunque los números casi coincidan (lo que gobierna no es cuánto dice el
  modelo sino de dónde sale el número).
- **Fila cured sin inventar métricas**: el run del arco (04/06-jul) no midió MTTD/MTTR con el
  harness de chaos → celdas "—" y outcome `cured`. Honestidad sobre estética.
- **Sin cambios estructurales**: a 2 días del chapter, ni slides nuevas ni rediseño ni tocar el
  replay (la mejor pieza del deck).

## Siguiente
- **PENDIENTE (petición Jay 13-jul): re-tratar la durabilidad de Redis** — la fila "Durabilidad"
  se QUITÓ de la slide de Límites (petición pre-reu tutor); el límite sigue declarado en el
  respaldo R2 (nota honest) y en la QA 7 del guion, así que no desaparece del discurso, pero
  la slide de límites ya no lo lista. Arreglo de fondo = AOF+PVC (backlog v2.1); cuando se haga,
  o bien re-añadir la fila como "resuelto" o dejarla fuera. Revisar también antes del chapter si
  conviene re-añadirla tal cual (un chapter que pregunte por durabilidad la encontrará en R2/QA).
- **Hoy (S7, Jay)**: `/ship` de `588e3a9` + arco chaos → capturas C-08 (doble botón en
  Mattermost) y métrica R5 en Grafana → sustituir/añadir en slides 06-07 y 13 (Tanda 2, cierra
  B2 con evidencia). Gate 8 screenshots.
- **Antes del 15 (Jay)**: re-confirmar los 696 tests en verde (M5); si el ship se cayó, revisar
  la QA de estado de deploy (está redactada para ambos escenarios).
- **14-jul (S8)**: `/ensayo` hostil con el guion ampliado (23 QA) — foco nuevo: prompt
  injection, throughput, tensión slide 10↔11.

## Vault Impact
| Tipo | Destino | Nota |
|---|---|---|
| Pattern | 03_Knowledge/AI_ML | "Review the screenshot, not the caption" — los captions que citan números de una imagen embebida hay que verificarlos CONTRA la imagen (el 65% vs 95% era visible en pantalla) |
| Pattern | 04_Systems/Patterns_I_Keep_Using | Un deck honesto se audita con su propio listón: la slide "cada afirmación mapea a algo que el sistema hace" convierte el gap de deploy en hallazgo |
