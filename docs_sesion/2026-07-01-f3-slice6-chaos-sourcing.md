---
fecha: 2026-07-01
slug: f3-slice6-chaos-sourcing
promoted: true
---

> Continuación de `f3-auto-engine-resourcing`. Slice 6: validar la auto-remediación de verdad en cluster (chaos OOM con `da7aafb` desplegada). Tres fallos en cascada revelaron la causa raíz real → el target/valor que el motor ejecuta sale del LLM y un 1.5b lo alucina. Pivote: experimento de modelo grande (refutado por latencia) + dos fixes hechos (temp=0, OOM enrich) + diseño de C (sourcing determinista). Código y manifiestos yo; kubectl/chaos/evals/git Jay.

## Objetivo
- Slice 6 de F3: ejecutar `scripts/chaos.sh oom` contra el agente con `da7aafb` y **ver el auto-remediate real** (no tarjeta inerte): `Rule 4.5 exception` + comando sintetizado + rollback.
- A media sesión, pivote validado con Jay: antes de la slice C (sourcing), **probar un modelo mucho mejor** offline para ver si el problema era tamaño de modelo. Luego seguir con C.

## Hecho
**Validación en cluster (Slice 6) — 4 corridas de `chaos.sh oom`:**
- Deploy `da7aafb` OK (`agent-9c5d6d76f-kt7sj`, READY 1/1; readyz `{"status":"ready","mode":"queue","redis":"up"}` en :8000, no :8080).
- **21:47** — auto disparó: `Rule 5 bypassed: structured remediation` (risk=high del modelo), comando sintetizado correcto `set resources deployment chaos-oom-target ... --containers=chaos-oom-target --limits=memory=512Mi`, decisión `auto_remediate`. **Solo falló por RBAC** (`Forbidden`: SA `arturo-llm-test:default` no podía `get deployments` en `arturo-chaos`).
- **Fix RBAC**: `kubectl apply -f k8s/rbac.yaml` → `Forbidden` pasó a `NotFound` (la SA ya alcanza la API).
- **21:52** (CrashLoop derivado del OOM) — target alucinado: `deployment arturo-chaos` (=namespace), `--containers=chaos-oom-target-...-h4gfw` (=pod) → `NotFound`.
- **22:02** (OOM limpio) — valor no parseable: `current='256Mi, 512Mi, 1Gi'` (lista fabricada) → `unparseable_memory` → `escalate`. Outcome chaos `auto_remediate` no se registró (dedup correcto de reenvíos + escalate real).

**A1 — temp=0 (código, hecho):**
- `agent/config.py`: nuevo `ollama_temperature: float = 0.0` (greedy decoding, default determinista).
- `agent/diagnosis.py`: `"options": {"temperature": settings.ollama_temperature}` en el POST a Ollama.
- `agent/tests/test_diagnosis.py`: `test_generation_uses_configured_temperature` (asserta que `options.temperature` viaja en el body). `py_compile` verde; pytest pendiente Jay.

**B — enriquecer regla OOM (manifiesto, staged):**
- `k8s/prometheus.yaml` `KubePodOOMKilled`: añadida la anotación `(current resources.limits.memory: …)` con la misma plantilla `query()` que `HighMemory` (printf en UNA línea física por el folding `>`). Pendiente: `kubectl apply` + reload de Prometheus para validar el render en vivo.

**Experimento de modelo grande (Microtask A2, refutado por latencia):**
- Buscados tags reales en Ollama (qwen2.5 no tiene salto fácil): elegidos `qwen3.5:9b` (6.6GB) y `mistral-small:24b` (14GB, JSON nativo); control `qwen3.5:2b`; alternativa ligera `mistral-nemo:12b`. Docstring de `eval_model_compare.py` actualizado con tags reales + nota temp=0.
- Corrida: **1.5b@temp=0 completa** (~1.2–2.3s/alerta, 10 alertas). **`qwen3.5:9b` murió a los 600s exactos** (timeout httpx del harness, línea 182) en la 1ª alerta → swapping en M4. 24b descartado.

## Encontrado / gotchas
- **CAUSA RAÍZ (el hallazgo de la sesión)**: el comando que el motor EJECUTA toma `name`/`namespace`/`container`/`current`/`new` **directos de `proposed_action`** (lo emite el LLM; `diagnosis.py:163` lo construye entero, `remediation.py:320-323/645-647` lo consume). `is_structured_remediation` valida que existan + prefijo de ns + valor parseable + solo-subir, pero **NO que el deployment exista ni que `container` no sea un pod**. Un 1.5b alucina esos campos → distinta basura cada corrida.
- **El target sigue mal AUN con temp=0** (clave): `highmemory-001` → `deployment arturo-llm-test` (=ns); `highmemory-002` → `deployment cache-6c7f8d9-mzr4p` (=pod). temp=0 mató la varianza de **valor** (cero listas), pero el **target es problema de SOURCING, no de varianza ni de tamaño**. Un modelo mejor no lo arregla: la alerta YA lleva `container=stress`/`namespace`/`pod` correctos y el modelo los ignora.
- **`auto(on)` offline es optimista**: `highmemory-001` dio `Rule 4.5 exception` + `Rule 5 bypassed` → cuenta AUTO, pero `deployment arturo-llm-test` no existe → en vivo sería `NotFound`. El harness no valida identidad de target (no ejecuta) → sobre-reporta auto. Es la prueba en mano de por qué C hace falta.
- **OOM nunca se enriqueció en F4**: F4 metió `query()` solo en `HighCPU`/`HighMemory` (proactivas), NO en `KubePodOOMKilled` (post-mortem). Por eso el modelo no recibía el `current` y lo fabricaba (la lista). Límite real del chaos = **32Mi** (chaos-oom.yaml:54), el `256Mi,512Mi,1Gi` fue 100% inventado.
- **Modelos grandes NO viables**: ni en M4 (9b = 600s timeout, swapping) ni en cluster (e2-standard-2, sin GPU). El MTTR de 247s ya es casi todo LLM con el 1.5b. → refuerza "enviar el pequeño + motor determinista".
- **`temperature` no estaba fijada** → Ollama default 0.8 → varianza run-to-run (un OOM `32Mi` limpio, otro la lista). Causa de que Slice 6 fuera no-determinista.
- **`chaos.sh cleanup` borra el namespace `arturo-chaos` → se lleva el RBAC** (Role/RoleBinding viven ahí). `chaos.sh oom` recrea el ns pero NO el RBAC. Orden correcto SIEMPRE: cleanup → `kubectl create namespace arturo-chaos` → `kubectl apply -f rbac.yaml` → oom. NUNCA `cleanup && apply rbac` en cadena (el `&&` falla: el ns no existe aún → `NotFound`).
- **El tag de deploy es SIEMPRE el short SHA del commit, nunca el build ID** (arrastrado de la sesión previa; el ImagePullBackOff venía de usar el build ID).
- **`nginx -n production` / `app-backend -n staging` en el eval NO son workloads reales** — son fixtures sintéticos de `alerts_oom.json` (oom-001/003). El eval es offline, no toca cluster. El guardrail de prefijo `arturo-` los rechazó → buen material de demo (blast-radius sobre datos de prueba, sin riesgo).
- **Guardrails impecables en el eval**: prefijo de ns rechazó `staging`/`production`; cap 2× bloqueó `1000m`←`250m` (`cpu_exceeds_2x`); `restart-implies` capturó rollouts. La validation layer hace su trabajo aun con un modelo malo.

## Decisiones + por qué
- **Pivotar a C (sourcing determinista) como camino de envío, NO a un modelo mayor.** *Por qué*: el experimento probó que (a) los grandes no son desplegables (latencia/HW) y (b) **no arreglan el target** (es sourcing, no inteligencia). C es model-agnostic y se envía sobre el 1.5b actual.
- **temp=0 como default de producción**, no solo lever de experimento. *Por qué*: un motor de remediación necesita razonamiento estructurado reproducible; la varianza de sampling era la mitad del problema (valor). Es keeper, no scaffolding.
- **Enriquecer OOM (B) aunque venga C**: defensa en profundidad barata y declarativa. El `current` en la alerta mejora la escalación (operador ve el límite real) aunque el motor luego lo sourcee de `kubectl get`.
- **Diseño de C (3 decisiones, mi recomendación)**: (1) `container`+`namespace` desde los labels de la alerta (exacto, gratis — arregla el fallo de las 21:52); (2) nombre de deployment por **strip-hash** (`pod.rsplit("-",2)[0]`) + **gate de existencia** (el `capture_pre_patch_value` ya hace `kubectl get`; si `NotFound` → escala, nunca parchea un nombre adivinado) en vez de owner-ref (que pide RBAC de replicasets + latencia); (3) valores desde el snapshot `kubectl get` (verdad de tierra), `new = 2×current` capado, descartando los números del LLM. El LLM queda reducido a **field + dirección**.
- **No correr Mistral 24b ni reintentar 9b**: en M4 no caben a latencia usable y la decisión ya está tomada. `qwen3.5:4b` solo confirmaría calidad de valor (ya OK con 1.5b@0) → no aporta.

## Siguiente
- **Ejecutar C (slice de sourcing determinista)** — refactor de `is_structured_remediation` + flujo de decisión + `diagnosis.py:163` (sellar target desde labels) + tests. Es lo demo-crítico para "proyecto becarios" (auto-remediación robusta, no dependiente de que el modelo adivine).
- **Aplicar B en cluster**: `kubectl apply -f k8s/prometheus.yaml` + reload → validar que el `query()` renderiza `32Mi` en una alerta OOM real (riesgo vivo del templating).
- **Re-validar Slice 6 tras C**: `chaos.sh oom` (memoria, sin flag) → ejecución `exit_code=0` + `snapshot_captured: true` + rollback. Luego CPU con `REMEDIATION_AUTO_CPU_ENABLED=true`.
- **Commits** (sin `Co-Authored-By`): A1 `feat(diagnosis): generación a temp=0 configurable`; B `feat(prometheus): enriquecer KubePodOOMKilled con límite de memoria actual` (tras validar render). pytest los corre Jay.
- **Para el paper / demo "proyecto becarios"**: narrativa "el modelo propone, el motor dispone" — el LLM nunca elige el target destructivo ni la magnitud; el motor los saca de hechos del cluster. Más fuerte que "usamos un modelo grande". Tabla model-compare (1.5b viable / grandes no) + el guardrail rechazando namespaces ajenos como prueba de seguridad.
- **Pendiente arrastrado**: `/promote` masivo (7 bitácoras `promoted: true` ahora); matriz E1–E6 (`docs/14`); Gate 8 screenshots; self-heal NOGROUP live; doc de fallo F4 "auto limitado por el modelo" → superado por re-sourcing + queda como "limitado por sourcing", que C cierra.
