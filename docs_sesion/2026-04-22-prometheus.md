# Sesión 2026-04-22 — Prometheus standalone + 5 reglas AIOps

## Objetivo
Desplegar Prometheus mínimo en `arturo-monitoring` para alerting real (sin `curl` manual),
scrape de métricas del agente y base para Grafana en la siguiente sesión.

Basado en decisiones del tutor (reunión 2026-04-20):
- Prometheus standalone, no kube-prometheus-stack (aunque admin confirmado)
- 5 reglas: OOMKilled, CrashLoopBackOff, HighMemory, HighCPU, TargetDown

## Lo que se hizo

### 1. Creado `k8s/prometheus.yaml`
Manifiesto único (todos los recursos en arturo-monitoring):
- ServiceAccount + ClusterRole `prometheus-viewer` + ClusterRoleBinding (necesario para cAdvisor via proxy API)
- ServiceAccount + ClusterRole `kube-state-metrics-viewer` + ClusterRoleBinding
- ConfigMap `prometheus-config` — prometheus.yml con 2 scrape jobs (kubernetes-endpoints + kubernetes-cadvisor) y alertmanager URL
- ConfigMap `prometheus-rules` — 5 reglas del tutor en formato YAML plano (no CRD)
- Deployment `prometheus` (`prom/prometheus:v2.54.0`, emptyDir 2h retención, spot toleration)
- Service `prometheus-svc` ClusterIP :9090
- Deployment `kube-state-metrics` (`kube-state-metrics:v2.13.0`) — métricas K8s necesarias para OOMKilled y CrashLoopBackOff
- Service `kube-state-metrics-svc` ClusterIP :8080 anotado con `prometheus.io/scrape=true`

### 2. Actualizado `k8s/service-agent.yaml`
Añadidas 3 annotations para que Prometheus descubra el agente vía kubernetes_sd_configs:
```yaml
prometheus.io/scrape: "true"
prometheus.io/port: "8000"
prometheus.io/path: "/metrics"
```

### 3. Eliminados archivos obsoletos
- `k8s/prometheus-rules.yaml` — CRD PrometheusRule (requería operador, absorbido en prometheus.yaml)
- `k8s/prometheus-stack-values.yaml` — Helm values descartados (stack no se instala)

### 4. Documentación actualizada
- `docs/01-architecture.md` — tabla de componentes + decisión de arquitectura actualizada
- `docs/03-kubernetes.md` — tabla de manifiestos + nueva sección "Prometheus standalone"
- `docs/07-roadmap.md` — Prometheus marcado como hecho, próximas sesiones anotadas
- `CLAUDE.md` — estado actual actualizado (2026-04-22, Fase 1 ~95%)

## Cómo desplegar

```bash
kubectl apply -f k8s/prometheus.yaml
kubectl apply -f k8s/service-agent.yaml
kubectl rollout status deploy/prometheus -n arturo-monitoring
kubectl rollout status deploy/kube-state-metrics -n arturo-monitoring
```

## Verificación end-to-end

```bash
kubectl get pods -n arturo-monitoring
kubectl port-forward svc/prometheus-svc 9090:9090 -n arturo-monitoring
```

Abrir `http://localhost:9090/targets` → `agent-svc` y `kube-state-metrics-svc` y `cadvisor` UP.

Alerta real de test (OOMKilled):
```bash
kubectl run oom-test --image=polinux/stress --requests=memory=64Mi --limits=memory=64Mi -n arturo-llm-test -- stress --vm 1 --vm-bytes 250M --vm-hang 0
```
Esperar ~2 min → Prometheus `/alerts` → `KubePodOOMKilled` FIRING → Alertmanager → Mattermost.

Cleanup:
```bash
kubectl delete pod oom-test -n arturo-llm-test
```

## Notas técnicas

- **ClusterRole**: necesario porque cAdvisor está en los nodos y el proxy va por la API de K8s
  (`/api/v1/nodes/<name>/proxy/metrics/cadvisor`). Sin ClusterRole `nodes/proxy` no funciona.
- **kube-state-metrics**: GKE no lo incluye por defecto. Sin él, las reglas OOMKilled y CrashLoopBackOff
  no tienen series (`kube_pod_container_status_*`).
- **HighCPU y HighMemory**: la regla usa un join `on(namespace, pod, container)` entre cAdvisor
  (CPU/mem usage) y KSM (resource limits). Si KSM está down, estas alertas no disparan.
- **Retención 2h**: suficiente para modo proactivo futuro (consulta tendencias en ventana corta).
  Si se necesita retención histórica, añadir PVC en sesión futura.

## Próxima sesión sugerida

**Grafana** — datasource `prometheus-svc.arturo-monitoring.svc.cluster.local:9090`, dashboard
`aiops_diagnosis_total` / `aiops_remediation_total` / `aiops_feedback_total`, webhook desde Grafana.

## Vault Impact

| Área | Archivo vault | Cambio |
|---|---|---|
| AIOps project node | `01_Projects/AIOps_K8s_Agent.md` | Prometheus standalone desplegado, KSM, 5 reglas, roadmap Grafana |
