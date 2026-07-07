---
fecha: 2026-07-07
slug: skills-operativas
promoted: true
---

## Objetivo
Analizar el repo entero para identificar trabajo que se re-deriva de memoria en cada sesión y
convertirlo en skills invocables (`.claude/skills/`). Sesión sin código de producto: solo análisis +
runbooks. Añadir el catálogo al libro de estudio.

## Hecho
- **Análisis del repo con criterio "¿qué se reconstruye a mano cada vez?"**: scripts (`chaos_arc.sh`
  nuevo con teardown por `trap EXIT`, `smoke.sh`, `build_demo.py`), docs de proceso (08 playbook,
  10 evaluation, 12 chaos), `agent/evaluation/`, las 3 skills existentes (estilo) y ~10 bitácoras.
- **6 skills nuevas creadas** (locales, gitignored, registradas e invocables):
  - `/ship` — build+deploy con la disciplina de tags como Paso 0 (short SHA del commit que buildeó,
    nunca build ID, bump de manifiesto sin rebuild) + verificación post-deploy + smoke.
  - `/chaos-run` — capa humana sobre `chaos_arc.sh`: pre-vuelo (imagen viva, Redis residual con
    comandos concretos), port-forwards completos, secuencia esperada en logs (snapshot → seal →
    grounded=1.0 → veredicto), screenshots Gate 8 en el momento, teardown VERIFICADO como último gate.
  - `/eval` — mapa de los 3 scripts (cluster sí/no, `--use-cache` tras preemption Spot), modo R4
    (incidents poblados vs vacío + aviso de los ~30 docs HighCPU contaminantes), cierre en docs/10.
  - `/ensayo` — entrevistador hostil calibrado a chapter (no tribunal): 1 pregunta/ronda, feedback
    en 3 partes, regla anti-humo, 8 categorías (incl. curveballs de runs reales y F-06 durabilidad).
  - `/quality` — docs/08 hecho invocable sin duplicarlo (si discrepan, manda docs/08); scan con
    prompt prehecho → triage cruzado con docs/11 → N fixes con OK previo → cierre.
  - `/review-senior` — persona arquitecto externo, continúa serie F-xx desde F-18, listón
    "producción real", categoría propia de finding "claims vs realidad".
- **`libro_estudio.html` → v1.1**: capítulo nuevo "15 · El taller: método de sesión y skills"
  (tablas de las 3 de ciclo + 6 operativas con la lección de la que nace cada una, kbox "por qué
  esto es presentable"), nav + TOC + footer actualizados.
- Candidatas descartadas conscientemente: `/deck` (build_demo.py ya es 1 comando), `/preflight`
  standalone (absorbido en el pre-vuelo de `/chaos-run`), `/triage` de incidentes live (solapa con
  la sección "observar" de `/chaos-run`).

## Encontrado / gotchas
- `scripts/chaos_arc.sh` ya existía (untracked, con la lección del horno como `trap EXIT`) → la
  skill `/chaos-run` se reencuadró: no genera comandos de arco, es el runbook HUMANO alrededor del
  script (pre-vuelo, observación, screenshots, verificación del teardown).
- `docs/08-code-quality-playbook.md` ya es una skill escrita en prosa (workflow 5 pasos + prompt
  prehecho) — `/quality` solo lo hace invocable. Patrón: cuando un doc de proceso está maduro, la
  skill es un puntero con pasos, no una copia.

## Decisiones + por qué
- **Skills en español (cuerpo) + TRIGGERS bilingües (frontmatter) + comandos en inglés**: a este
  nivel de modelo la diferencia de obediencia ES/EN es despreciable; lo que pesa es que las skills
  producen artefactos en español (bitácoras, docs/10, docs/11) y el español evita deriva de idioma
  en la salida. Coherente con "docs en español, código en inglés" y con las 3 skills existentes.
- **Donde no hay certeza de un comando, la skill apunta a la fuente autoritativa** (leer
  `agent/evaluation/README.md`, `k8s/deployment-agent.yaml`, `kubectl get svc`) en vez de fijar el
  comando: mismo principio anti-alucinación que el agente, y las skills no se quedan stale si
  cambia un puerto o un nombre de servicio.
- **Regla de diseño transversal: las lecciones se convierten en gates, no en párrafos** — el horno
  nocturno es el Paso 5 de `/chaos-run`; el doble mordisco del tag es el Paso 0 de `/ship`. Una
  lección narrada solo en bitácora se vuelve a pagar.
- **Priorización por ventana de uso** (chapter en 1-7 días): `/ship`, `/eval`, `/chaos-run`,
  `/ensayo` tienen uso esta semana; `/quality` y `/review-senior` son post-chapter pero se crearon
  igual porque su coste es un markdown y su valor no caduca.
- **Capítulo 15 en el libro de estudio**: el método humano+IA es material presentable para el
  chapter ("las skills son al flujo de trabajo lo que el motor de reglas es al LLM"), no solo
  tooling interno.

## Siguiente
1. **Probar `/ensayo` en real** (la de ventana más corta — chapter 8 o 14 julio) y ajustar el
   formato de ronda si hace falta.
2. **S4 con `/eval`**: medición R4 (feedback-loop gain) — decidir antes qué hacer con los ~30 docs
   HighCPU nocturnos de ChromaDB (filtrar/limpiar y anotarlo como condición del run).
3. **Commit pendiente de Jay** (de la sesión anterior): bump `k8s/deployment-agent.yaml` a
   `0914611` + bitácoras. Las skills no entran (gitignored).
4. **Pendiente arrastrado**: Gate 8 resto de screenshots Grafana; `/promote` masivo (~11 bitácoras
   `promoted: false`, incluida esta); F-11/F-17; matriz E1–E6 (docs/14); S5 deck + ensayo; S6 F-06.
