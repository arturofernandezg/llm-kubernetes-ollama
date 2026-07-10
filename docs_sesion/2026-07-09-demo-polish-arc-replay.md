---
fecha: 2026-07-09
slug: demo-polish-arc-replay
promoted: false
---

## Objetivo
Desbloquear el cluster (pods Pending), correr **R4** (feedback-loop gain) y **pulir la demo** — foco
nuevo: los compañeros de prácticas centran la presentación **en la demo**, así que hay que hacerla
destacar. En paralelo, dejar clarísimo (para Jay y para la sala) **cómo funciona la remediación hoy**.

## Hecho
- **Cluster revivido**: los 4 pods de `arturo-llm-test` estaban `Pending`. Causa: la pool
  **`guaranteed-b24d` se quedó a 0 nodos** y el autoscaler no reponía (`NotTriggerScaleUp: 2 in backoff
  after failed scale-up`; solo quedaba 1 nodo **spot**, que los pods no toleran/seleccionan). Fix:
  `gcloud container clusters resize ai-infra-agent --node-pool guaranteed-b24d --num-nodes 2 --zone
  europe-southwest1-a` → 1 nodo guaranteed Ready → los 4 pods `Running` → **R4 desbloqueado**.
- **R4 corrido en cluster** (`qwen2.5:1.5b`, N=15, 3 reps/arm; salida
  `agent/evaluation_results/feedback_2026-07-08.json`):
  - **Capa A — POSITIVO**: retrieval de incidents `0% → 46.7% p@1` (`0% → 60% p@2`), vacío vs poblado.
  - **Capa B — NULL** (el pre-registrado en H0): 3 arms **idénticos** (control / `rolled_back` / `cured`):
    `memory_bump 3/3`, `avg_conf 0.80`, `mentions_failure 0/3` en los tres. El 1.5b **ignora** la
    etiqueta de outcome.
- **Deck pulido** (`scripts/build_demo.py`, 16 → **18 slides**, build limpio, las 4 capturas embeben):
  - **Slide replay animado del arco** (pieza estrella, slide 9): timeline CSS-only de 6 etapas
    (alerta → grounding → LLM → motor sella/escala → approve → `cured`) con **valores reales del run
    06-jul** (32Mi, 16×, `grounded=1.0`, `cured=1`, 512Mi). Auto-reproduce al entrar en la slide (el
    cambio `display:none→flex` reinicia las animaciones; cero JS extra). Jay validó la animación:
    *"perfect"*.
  - **Slide R4** (slide 11) reescrita a **dos preguntas en cristiano** (tras feedback de Jay: la versión
    "ablación/arms/control-negativo-positivo" era ininteligible): *"¿Encuentra casos pasados parecidos?"*
    (sí, 0→46.7%) / *"¿Le hace caso el modelo?"* (no; tabla de 3 filas idénticas).
  - **Alcance del auto explícito** (slide motor): "hoy el auto = subir el límite de `memory` de un
    Deployment; el resto escala/sugiere".
  - **Framing del ×2** corregido en todo el hilo: arco pasos 4-5, stat del motor
    ("cambio máximo en automático; más → humano"), slide de evidencia (redacción vieja "escaló a 2×"
    era engañosa), y **QA nueva** ("¿no rompe tu regla del ×2 aprobar un 16×?").
  - `STATS` con claves `r4_a_*`; wording de las slides de aprendizaje/límites afinado
    (guarda/recupera ✓ vs explota ✗).
  - Guion 6·b (el arco en movimiento) + 6·c (dos preguntas) + QA "¿entonces el aprendizaje no funciona?".
- **`docs/10`**: rellenado el hueco pre-registrado con las tablas A/B reales + interpretación.
- **`docs/11`**: **C-08** (la escalación >2× no ofrece el ×2 conservador como opción por defecto).

## Encontrado / gotchas
- **La pool guaranteed puede caer a 0** (scale-down / stockout de spot) y dejar el cluster inoperante;
  el resize manual la repone **si no es stockout real** (si el scale-up sigue en backoff tras el resize
  → no hay capacidad e2-standard-2 en la zona; ahí toca esperar o cambiar zona/tipo). Los pods core
  llevan `nodeSelector guaranteed=true` → un nodo spot no los acoge. `chromadb-0` además falla por
  `PersistentVolume node affinity` hasta que hay nodo guaranteed en la zona correcta.
- **Confusión clave del ×2 (lo más importante de la sesión para la defensa)**: el `≤2×` **NO es un
  techo absoluto, es la frontera de lo que el motor hace SOLO**. En el run el LLM pidió 512Mi (16×);
  `_normalize_new_value` (remediation.py:242) **solo** sintetiza 2× si el valor del LLM es *inusable*
  (512Mi es usable) → el motor **conserva el 512Mi**, la regla 4.6 lo **escala** (no lo recorta a la
  callada) y **un humano aprueba 512Mi**. 2× (64Mi) habría vuelto a petar sobre unos 32Mi irrisorios,
  por eso NO se clampa a un número arbitrario. La slide lo pintaba como "cap del cambio" → se leía como
  "la regla se saltó". Corregido el framing en todo el deck.
- **Bordes honestos de la remediación no-memoria** (la conversación que Jay quiere razonar con Fable):
  - **Auto solo para memoria**: `is_structured_remediation` exige campo = límite conocido (memory
    siempre; cpu tras `remediation_auto_cpu_enabled=False`). CrashLoop/ImagePull/HighCPU → **nunca auto**,
    siempre escala o sugiere. El camino lo decide **el tipo de arreglo** (subir un límite de memoria),
    no el nombre de la alerta (un CrashLoop que es OOM podría entrar por el camino auto).
  - **Contexto**: grounding **fail-soft** — correcto cuando `kubectl` va, ausente/parcial cuando falla
    (enrichment.py: varios `return None`; línea 223 "partial snapshot, LLM-only downstream"). Nunca
    inventado (prompt anti-fabricación diagnosis.py:59). Pero el snapshot **no captura la causa raíz**
    de un CrashLoop por imagen/config.
  - **Comandos ejecutables**: el prompt los pide "runnable immediately" (diagnosis.py:28) y bloquea
    destructivos, pero **no hay pre-flight de permisos** (C-07) → `kubectl top` sale `Forbidden`. Para
    no-memoria son texto libre del LLM, no sellados. → **no garantizado ejecutable**.
  - **¿Tienen sentido?** — el borde honesto: **medido** que el retrieval trae el runbook correcto
    (p@1 100%) y que los comandos son SAFE (100% RAG vs 25% zero-shot); **NO medido** que el fix
    realmente *cure* para no-memoria. **Safety ≠ correctness.** Solo memoria está validado E2E (`cured`).

## Decisiones + por qué
- **Demo = replay animado embebido** (descartadas: live sobre cluster, vídeo, dejar capturas). Por qué:
  el LLM 1.5b en CPU tarda 150-270s → una demo en vivo son ~4 min mirando la pantalla + riesgo de fallo
  en directo (spot, red). El replay es **reproducible, rápido y honesto** (etiquetado "replay de un run
  real, valores capturados, no simulados"). Elimina el mayor riesgo de la presentación.
- **Capa B null = fortaleza, no debilidad**. Por qué: un null **pre-registrado** (H0 escrita en `docs/10`
  antes de correr) es más creíble que un positivo forzado, y **refuerza la tesis "el motor dispone"**:
  la seguridad no depende de que el LLM recuerde sus fracasos, la impone el motor. Se publica el null.
- **No tocar el comportamiento del ×2 antes de la demo** (está validado en cluster). Solo arreglar el
  **framing** + registrar el refinamiento como **C-08** (la escalación podría ofrecer el ×2 conservador
  como opción por defecto). Es UX de la escalación, no seguridad — el motor ya impide el auto-16×.
- **Reescribir R4 en cristiano** en vez de defender la jerga: si el presentador no lo entiende a la
  primera, la sala tampoco. Marco de dos preguntas encadenadas.
- **Slide "por qué no VPA" descartada** (Jay), pero se guarda como pregunta hostil probable para `/ensayo`
  (subir el límite de memoria es literalmente lo que hace VPA).

## Siguiente
1. **Jay: nueva sesión con Fable** para razonar **mejoras de la remediación no-memoria** — el núcleo:
   ¿cómo pasar de "asistencia al humano" a algo con fix-correctness medible? Palancas sobre la mesa:
   pre-flight `auth can-i` (C-07), escalación con doble opción model/×2 (C-08), medir corrección del
   fix (no solo safety), ¿catálogo de acciones deterministas más allá de memoria?
2. **Commit pendiente** (Jay, sin `Co-Authored-By`):
   `git add scripts/build_demo.py demo/demo.html demo/guion.html docs/10-evaluation.md
   docs/11-quality-backlog.md agent/evaluation_results/feedback_2026-07-08.json && git commit -m
   "feat(demo): replay animado del arco + slide R4 (dos preguntas) + framing ×2 como frontera de
   autonomía + docs/10 resultados R4 + C-08"`.
3. **Validar el deck en pantalla** (`open demo/demo.html`, slides 9 arco + 11 R4).
4. **`/ensayo`** al cerrar el deck (×2/512Mi, null de R4, "¿por qué no VPA?", "¿el comando del CrashLoop
   funciona?").
5. **Opcional (ofrecido, no hecho)**: línea en la slide de límites / QA sobre *safe + contexto ✓ /
   fix-correctness no medido* para no-memoria — blindar el landmine de Q&A por escrito.
6. **Arrastrados**: matriz E1–E6 (`docs/14`); Gate 8 resto de screenshots Grafana; F-11/F-17/F-06;
   `/promote` masivo de bitácoras (`promoted: false`: 07-07, 07-08, este 07-09).
