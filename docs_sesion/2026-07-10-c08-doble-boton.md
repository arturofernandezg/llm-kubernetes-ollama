---
fecha: 2026-07-10
slug: c08-doble-boton
promoted: true
---

## Objetivo
**Bloque 4** del plan pre-chapter (primera pata): **C-08 — doble botón model/×2** en la
escalación de una remediación estructurada. Cuando el modelo propone un valor > 2×current
(escalado por la regla 4.6), dar al humano **dos** botones bien formados en vez de uno:
la **×2 determinista del motor** (segura, recomendada) y el **valor del modelo**. Convierte
la tesis "el modelo propone, el motor dispone" en una **elección explícita del operador**
(hasta ahora era una decisión oculta del motor). Buen momento de deck.

## Hecho
- **`agent/remediation.py`** — nuevo helper puro `structured_command_variants(diagnosis)`:
  - Devuelve lista de variantes `{id, action, value, command}`. **2** variantes cuando el
    valor del modelo difiere de la ×2 determinista — `engine` (2×current, `approve_engine`,
    primera) + `model` (valor del LLM, `approve_model`); **1** variante bajo la acción legacy
    `approve` cuando coinciden (caso auto-capped: new_value ya == 2×current) o el
    `current_value` no es doblable (fallback back-compat exacto).
  - `[]` si no es `is_structured_remediation`. Ambos comandos construidos con
    `build_set_resources_command` (nunca free-text del LLM). Colocado justo tras
    `is_structured_remediation` (reusa `_double_limit_value` + `_LIMIT_FIELD_PARSERS`).
- **`agent/mattermost.py`** — `send_escalation_with_buttons(..., approve_variants=None)`:
  - Con `approve_variants` (cada uno `{action, label}`) renderiza un botón de aprobación por
    variante; sin él, botón único "Ejecutar remediación" (back-compat). Refactoricé la
    construcción de botones a un helper interno `_button(action, label)` (DRY, incluye el
    botón reject). **Cada botón firma su propia `action`** vía `make_hmac_token(incident_id,
    action, secret)`.
- **`agent/main.py`**:
  - `PendingEscalation` gana `command_variants: dict[str, str]` (acción→comando) +
    serialización en `_escalation_to_dict`/`_dict_to_escalation` con `.get(..., {})`.
  - Build de escalación: para estructurada calcula `variants = structured_command_variants(
    diagnosis)`, guarda `command_variants = {v["action"]: v["command"]}` y, si hay 2, pasa
    `approve_variants` a Mattermost. Para free-text (C-07) el camino no cambia.
  - Helpers de etiqueta `_variant_button_label` (botón: "✅ ×2 motor (64Mi)" / "⚠️ Valor
    modelo (512Mi)") y `_variant_body_label` (cuerpo: "Opción A/B").
  - `_format_escalation_body(..., command_variants=None)`: con 2 variantes muestra un bloque
    etiquetado por opción con su comando; si no, el camino de antes.
  - Handler `/webhook/action`: la condición pasa de `action == "approve"` a
    `action in ("approve", "approve_engine", "approve_model")`; ejecuta
    `[command_variants[action]]` (fallback a `safe_commands` para escalaciones pre-C-08).
    El safety-net (snapshot + rollback + cooldown) queda **igual** — es value-agnostic
    (mismo target, solo cambia el valor).
- **Tests (+8)**:
  - `test_remediation.py::TestStructuredCommandVariants` (×4): 2 variantes con overshoot,
    1 variante cuando model==2×current, 1 cuando current no doblable, `[]` si no estructurado
    (lowering / namespace ajeno).
  - `test_endpoints.py::TestCommandVariantEscalation` (×1): E2E por
    `_process_alert_with_diagnosis` — gather mockeado gather_ok=False (seal no toca el
    diagnóstico) → `send_escalation_with_buttons` recibe `approve_variants` de 2 con acciones
    correctas, el cuerpo muestra ambos comandos y la escalación persiste `command_variants`.
  - `test_endpoints.py::TestCommandVariantCallback` (×3): `approve_model`/`approve_engine`
    ejecutan el comando correcto; token firmado para `approve_engine` **no** autoriza
    `approve_model` (401 — integridad por acción).
- `py_compile` OK en los 3 módulos + los 2 tests. **pytest lo corre Jay.**

## Encontrado / gotchas
- **HMAC bindeado por acción, no por variante-context**: la alternativa (un solo `approve`
  con un campo `variant` en el context) dejaría la elección model↔engine **sin firmar** →
  flippable en tránsito. Usar acciones distintas (`approve_engine`/`approve_model`) hace que
  `_verify_hmac_token` (que firma el string de acción) las proteja **sin tocar** su lógica.
- **El safety-net no necesita saber qué variante se eligió**: `capture_pre_patch_value` lee el
  CURRENT real del cluster (no `new_value`) y el rollback revierte al snapshot capturado →
  elegir 64Mi o 512Mi solo cambia lo que ejecuta `execute_commands`. Cero inconsistencia en
  snapshot/rollback/cooldown. Esto simplificó mucho el handler.
- **Consistencia build vs process_remediation**: `structured_cmd` (de `process_remediation`)
  y `structured_command_variants(diagnosis)` leen el MISMO `diagnosis` sellado → si
  `structured_cmd` es truthy, `is_structured_remediation` es True → variantes no vacías. No
  hay ventana de divergencia.
- **Caso auto-capped = 1 botón**: cuando el seal ya sintetizó new_value=2×current (valor del
  LLM inusable), model y engine coinciden → una sola variante (acción `approve`), UX idéntica
  a antes. El doble botón solo aparece cuando el modelo aporta un valor *distinto y usable*
  que la regla 4.6 mandó a revisión humana (el caso 16× del chaos OOM).

## Decisiones + por qué
- **Variante `engine` primera y "recomendada"**: es el bound determinista y seguro; el orden
  y las etiquetas (✅ vs ⚠️) empujan al operador hacia la opción acotada sin quitarle la
  agencia de aprobar el valor del modelo si lo juzga correcto. La honestidad del deck: el
  humano ve *ambas* y el sistema no esconde la propuesta del LLM.
- **Fallback a `safe_commands` en el handler**: escalaciones persistidas antes de C-08 (o
  free-text C-07) tienen `command_variants={}` → el handler cae a `safe_commands` con la
  acción `approve`. Cero migración, cero ruptura de escalaciones en vuelo durante un deploy.
- **Set explícito de acciones aprobables** (`in (...)`) en vez de `startswith("approve")`:
  evita el footgun de una acción tipo `approvexyz` colándose al fallback. HMAC ya filtra,
  pero el set explícito es más legible y defensivo.
- **Guardar `command_variants` como acción→comando** (no id→comando): el handler indexa
  directo por `payload.context.action` sin traducir id↔acción. Una fuente, un lookup.

## Siguiente
1. **Validar C-08 con pytest** (suite completa, no solo el subconjunto) y **commit** de F-17 +
   C-07 + C-08 + docs de sesión (R5 ya está en `1dbfdf3`). Sugerencia de mensaje:
   `feat(escalation): C-08 doble botón model/×2 — el humano arbitra propuesta del modelo vs ×2 determinista del motor`.
2. **Bloque 4 resto**: F-06 (docs durabilidad Redis: AOF+PVC o degradar el claim) + `/ensayo`.
3. **Deck**: slide "el humano arbitra" — captura del doble botón en Mattermost (Opción A ×2
   motor 64Mi / Opción B valor modelo 512Mi) sobre el chaos OOM 16×. Junto a R5 (MTTR
   observado) y F-17 (logs+events) cierra el arco de features del sprint.
4. **Arrastrados**: matriz E1-E6 (docs/14); Gate 8 screenshots; `/promote` masivo (07-07,
   07-08, 07-09 ×2, 07-10 R5, 07-10 C-08).

## Vault Impact
| Archivo | Cambio |
|---|---|
| 03_Knowledge/AI_ML/ (patrón) | Doble botón "propuesta del modelo vs bound determinista": convertir la decisión oculta de un motor de reglas en una elección explícita y auditada del humano — el sistema no esconde la salida del LLM, la ofrece junto a la opción segura y deja arbitrar |
| 03_Knowledge/Programming/ (patrón) | Integridad de callbacks multi-opción: firmar la ACCIÓN (approve_engine/approve_model) en vez de un campo de contexto no firmado → la elección queda HMAC-protegida sin tocar la verificación |
| 01_Projects/AIOps node | Bloque 4 (C-08 doble botón) primera pata cerrada en código + tests. Queda F-06 + ensayo. Deck: slide "el humano arbitra" |
