---
fecha: 2026-06-30
slug: f4-context-enrichment
promoted: true
---

> Cuarta sesión del 2026-06-30, continuación de `eval-run-cpu-integtest`. Tras refutar slice 2 (el cuello era el contexto, no el modelo), se planea (plan mode) e implementa **F4: enriquecer el contexto de las alertas** + se ejecuta el **bucle de efectividad** (baseline → cambio → re-medición) que resuelve la hipótesis de slice 2 con datos. Código y datasets yo; `ollama pull`, port-forwards, evals y git Jay.

## Objetivo
- Cerrar las dos causas raíz de slice 2 confirmadas en código y **medir su efectividad** con el harness existente:
  1. La regla Prometheus `HighCPU`/`HighMemory` no llevaba el límite actual → el modelo alucina el `current_value`.
  2. El retrieval de HighCPU nunca se había medido (slice 2 lo infirió).
- Decisiones de diseño (preguntadas y validadas con Jay): enriquecer vía **anotación Prometheus** (declarativo); alcance **HighCPU + memoria** (simétrico).

## Hecho
**4 mejoras (todas validadas: JSON/YAML/`py_compile` OK; loader carga las 5 alertas nuevas; `inject_runbook_context` OK para highmemory):**
- **Mejora 1 — anotaciones Prometheus** (`k8s/prometheus.yaml`, reglas `HighCPU`/`HighMemory`): templating `query()` sobre `kube_pod_container_resource_limits` → la `description` ahora incluye `(current resources.limits.cpu: 250m)` / `(...memory: 256Mi)`. CPU usa `humanize` (cores→millicores: `0.25`→`"250m"`), memoria `humanize1024` (bytes→`"256Mi"`). El PromQL va en **una línea física** (el folding `>` de YAML mete espacios al unir líneas y corromperia el selector).
- **Mejora 2 — prompt anti-alucinación** (`agent/diagnosis.py`, `DIAGNOSIS_PROMPT`): "If the current limit is NOT present… OMIT proposed_action. NEVER guess, fabricate, or default the current_value". `test_diagnosis.py` no asserta sobre el prompt → sin riesgo de romper tests (pytest pendiente de Jay).
- **Mejora 3 — activos de medición**: `alerts_highcpu.json` (001 enriquecida con límite, +003 **adversaria** sin límite); `alerts_highmemory.json` **nuevo** (001 enriquecida 256Mi + 002 adversaria); `expected_runbooks.json` +highcpu-003/highmemory-001/002.
- **Mejora 4 — inconsistencias OOM** (`alerts_oom.json`): `context_note` en oom-001..005 marcando intención (003/005 enriquecidas, 001/002/004 adversarias **explícitas**). Campo ignorado por el loader.

**3 evals corridos por Jay (Mac local + port-forward):**
- **Retrieval baseline**: **p@1=73.3% (11/15), p@3=86.7% (13/15)**. Clave: **HighCPU/HighMemory 5/5 @1** (top-1 correcto en las 5).
- **Model-compare post-fix** (`qwen2.5:1.5b`, `--alerts highcpu,highmemory`): **field_ok 5/5**, auto(on) **0/5**, conf 0.84, ~2s/diagnóstico.

## Encontrado / gotchas
- **DECISIVO — las dos mitades de la hipótesis slice 2 resueltas con datos**: retrieval HighCPU/HighMemory = **5/5 @1** → nunca fue el problema. Con el límite en la `description`, field_ok salta a **5/5** en ~2s. → El fallo de cluster de slice 2 era **100% el límite ausente en la alerta**, ni el modelo ni el retrieval. "Contexto > tamaño de modelo" queda probado.
- **auto(on)=0/5 — y NO es un solo gate, son límites del modelo pequeño** (todos legítimos, la validation layer trabajando):
  - **`has_set_resources=null` en 4/5** (HALLAZGO NUEVO): el modelo propone la acción en `proposed_action` (campo correcto) pero **no emite el `kubectl set resources` ejecutable** — sus `commands` son investigativos. **Field correcto ≠ comando ejecutable.**
  - **`risk=high` en 2/5** (el modelo rates conservador) → escala por regla 5.
  - **`conf~0.80 < 0.9`** del umbral de la excepción 4.5.
  - **Sobre-escala**: highcpu-002 propuso `1000m` desde `250m` (4×) → cap 2× (regla 4.6). Y usó `--containers=container-name` (placeholder literal sin sustituir).
- **La abstención (Mejora 2) NO funcionó a nivel de modelo**: highcpu-003 y highmemory-002 (sin límite) **no se abstuvieron**. highmemory-002 fabricó un valor basura (`"256Mi (or another value as per the alert context)"`). **PERO la validation layer lo atrapó** (`unparseable_memory` → escalate). → **La seguridad viene de la defensa en profundidad del motor, no de que el modelo se abstenga.** Decirle a un 1.5B "omite si no sabes" no es fiable.
- **Retrieval backlog (ortogonal a F3)**: los 4 fallos de p@1 están en otras clases — **imagepull-001 fallo total** (ni en top-3), **imagepull-002** solo @3; **oom-001 fallo total** (descripción genérica "was OOMKilled"), **oom-004** solo @3 (HighMemory rankea por encima por "memory leak"). Debilidad real de retrieval en imagepull/oom.
- **Gotcha de ejecución del retrieval**: necesita port-forward `kubectl port-forward -n arturo-llm-test svc/chromadb-svc 8001:8000` (CHROMADB_PORT=8001→8000) + `nomic-embed-text` en el Ollama local. La 1ª corrida dio `0/15` por "Could not connect to a Chroma server" (sin port-forward) — artefacto, no medición.
- **Ruido benigno**: la telemetría de ChromaDB lanza `capture() takes 1 positional argument but 3 were given` — no afecta.

## Decisiones + por qué
- **Enriquecer vía anotación Prometheus, no en el agente**. *Por qué*: declarativo, alerta autodescriptiva, cero código/kubectl extra en la ingesta, y el eval offline ya consume la `description` → medible sin cluster. Encaja con "medir offline antes de tocar GKE".
- **Alcance HighCPU + memoria (simétrico)**. *Por qué*: misma causa raíz (ambas reglas ya referencian `kube_pod_container_resource_limits` en su `expr`); `HighMemory` es el camino proactivo de memoria, espejo de HighCPU (aunque el OOM post-mortem ya funcionaba).
- **NO relajar el gate `conf≥0.9` de la excepción 4.5**. *Por qué*: es la validation layer haciendo su trabajo; bajarlo para "demostrar" auto sería deshonesto e inseguro. El eval lo confirma: aun relajándolo, `risk=high/medium` bloquearía 2/5 igual.
- **Cerrar F4 con escalate-first como postura honesta de producción**. *Por qué*: los datos dicen que auto CPU/memoria **no es alcanzable de forma fiable** con `qwen2.5:1.5b` (no emite comando, rates risk alto, no abstiene). Perseguir auto sería una métrica vanidosa. Valor real entregado: **escalaciones de mejor calidad** (recurso correcto + límite actual visibles al operador) + **infra de medición honesta** que prueba dónde está el techo.
- **Marcar las fixtures OOM con `context_note` en vez de homogeneizarlas**. *Por qué*: el loader ignora campos extra; mantener variantes con/sin límite es cobertura adversaria valiosa (verifica la abstención), ahora intencional y no por descuido.

## Siguiente
- **`/promote`** de F3 (slice 1+1b+2) + esta sesión F4 + el eval. Destilar a:
  - `docs/02` — motor CPU + flag `remediation_auto_cpu_enabled` + capture/revert field-agnostic; anotaciones Prometheus enriquecidas.
  - `docs/07` — F3 hecho + F4 + changelog; modo de fallo **"auto limitado por el modelo (no emite comando / risk alto / conf<0.9), no por el flag/field"**; retrieval HighCPU/HighMemory 5/5 @1.
  - `docs/10` — tabla model-compare + retrieval baseline (p@1 73%/p@3 87%) + el hallazgo **"contexto > tamaño de modelo"** y **"abstención garantizada por el motor, no por el modelo"**.
  - `CLAUDE.md` — remediation dimensión CPU + flag, anotaciones Prometheus, conteo de tests, imagen actual.
- **Redeploy**: `kubectl apply` de `k8s/prometheus.yaml` + reload Prometheus → **validar que el `query()` renderiza el límite en una alerta real** (el riesgo vivo del templating); rebuild + deploy de la imagen del agente con el prompt nuevo.
- **Validación en cluster (cierre E2E)**: `scripts/chaos.sh cpu` → ver la tarjeta de Mattermost con el límite actual + el modelo proponiendo el recurso correcto.
- **Tweak opcional (UX de escalación, NO desbloquea auto)**: endurecer el prompt para emitir SIEMPRE el `kubectl set resources` exacto cuando hay `proposed_action` con field de límite (hoy `has_set_resources=null` en 4/5) → tarjeta con comando listo para aprobar de un click. Re-medir.
- **Backlog retrieval (ortogonal)**: mejorar imagepull (p@1 miss, top-3 miss en 001) + oom-001/004 (descripciones genéricas / colisión con HighMemory). Pasada de calidad RAG futura.
- **Pendientes arrastrados**: decidir auto-alert HighCPU del propio agente (limits 300m); matriz E1–E6 (`docs/14`); Gate 8 screenshots; validar self-heal NOGROUP live; test de integración auto-CPU (`TestProcessRemediationCpuAuto`) ya hecho la sesión previa, pytest pendiente.
