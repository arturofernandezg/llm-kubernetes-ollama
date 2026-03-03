# Ollama LLM on Kubernetes
Despliegue de una Large Language Model usando **Ollama** en un cluster de Kubernetes.

Este proyecto demuestra cómo:

- Desplegar una LLM en Kubernetes
- Exponerla mediante un Service (ClusterIP)
- Acceder vía `kubectl port-forward`
- Descargar modelos dinámicamente
- Consumir la API vía HTTP


# Requisitos: Clúster de Kubernetes, kubectl configurado, namespace configurado.

Si no existe namespace: kubectl create namespace name

