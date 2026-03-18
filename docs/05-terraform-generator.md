# Generador de Terraform

## Archivo

`generate_tf.py` — script CLI independiente (no requiere K8s para ejecutarse,
solo necesita que el agente esté accesible via port-forward o URL directa).

**Nota**: la lógica core de generación (template, `safe_name()`, `generate_terraform()`)
vive ahora en `agent/tf_generator.py` como módulo importable. `generate_tf.py` lo importa
para evitar duplicación de código. Esto permite que el agente FastAPI también genere
Terraform directamente sin pasar por la CLI.

## Flujo

```
python generate_tf.py "mensaje" ──► POST /extract ──► JSON params ──► .tf file
```

1. Envía el mensaje al endpoint `/extract` del agente
2. Recibe parámetros extraídos (project_name, region, instance_type, purpose)
3. Genera un archivo `.tf` usando el template corporativo
4. Lo guarda en `terraform_output/<project_name>.tf`

## Uso

```bash
# Requisito: port-forward activo al agente
kubectl port-forward svc/agent-svc 8000:8000 -n arturo-llm-test

# Generar .tf
python generate_tf.py "Servidor para web-prod en europe-west1 con e2-standard-4"

# Solo extraer parámetros (sin generar archivo)
python generate_tf.py --dry-run "Servidor para web-prod en europe-west1"

# Especificar URL del agente
python generate_tf.py --agent-url http://10.0.0.5:8000 "..."

# Especificar archivo de salida
python generate_tf.py --output ./infra/mi-proyecto.tf "..."
```

## Template Terraform generado

Cada `.tf` generado incluye:
- Header con metadatos (fecha, request_id, modelo, mensaje original)
- Provider `google` con la región extraída
- Variables: `project_id`, `ssh_public_key` (sensitive)
- Módulo `terraform-modules/gcp-vm ~> 1.0` con los parámetros extraídos
- Labels obligatorios MasOrange: `managed-by`, `project`, `environment`, `created-by`
- Outputs: `instance_name`, `instance_ip`, `instance_zone`

## Valores por defecto (cuando el LLM no extrae un campo)

| Campo | Default |
|---|---|
| project_name | "undefined-project" |
| region | "europe-west1" |
| instance_type | "e2-standard-2" |
| purpose | "general purpose" |

## Función `safe_name()`

Convierte cualquier string a identificador válido para Terraform:
- Solo `a-z`, `0-9`, `_`
- Elimina underscores finales (`.strip("_")`) — fix del commit e36ceab
- Si queda vacío tras la conversión, retorna `"project"` como fallback
- Ej: "Web-Prod 2024" → "web_prod_2024", "test-" → "test" (no "test_")

---

## Errores conocidos y soluciones

### "No se puede conectar con el agente"
**Causa**: port-forward no activo o URL incorrecta.
**Solución**: `kubectl port-forward svc/agent-svc 8000:8000 -n arturo-llm-test`

### "El modelo no devolvió JSON válido" + se genera .tf con defaults
**Causa**: tinyllama a veces no genera JSON válido, especialmente con mensajes ambiguos.
**Solución**: usar `--dry-run` primero para ver qué extrae. Reformular el mensaje
con más detalle. El agente ahora incluye retry automático con backoff exponencial.
Se migró a qwen2.5:1.5b que tiene mejor precisión en extracción.

### safe_name produce trailing underscore
**Causa**: input terminado en caracteres especiales (ej: "test-") generaba "test_".
**Solución**: se añadió `.strip("_")` al final de `safe_name()`.
Corregido en commit e36ceab.

### datetime.utcnow() deprecation warning
**Causa**: Python 3.12+ depreca `datetime.utcnow()` por no incluir timezone info.
**Solución**: reemplazado por `datetime.now(timezone.utc)`.
Corregido en commit 87675be.
