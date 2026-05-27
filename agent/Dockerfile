FROM python:3.11.12-slim

# Evitar ficheros .pyc y buffering en logs
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Instalar dependencias primero — esta capa se cachea si requirements.txt no cambia
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# kubectl para ejecutar comandos de remediación (in-cluster auth via SA token montado)
# Versión pineada al minor del server GKE (v1.35.x) para evitar skew de API.
# Cloud Build tiene internet para el ADD; el pod en runtime no lo necesita (API server in-cluster).
ADD https://dl.k8s.io/release/v1.35.0/bin/linux/amd64/kubectl /usr/local/bin/kubectl
RUN chmod 0755 /usr/local/bin/kubectl

# Copiar código fuente (todos los módulos Python)
COPY *.py ./
COPY runbooks/ ./runbooks/

# Ejecutar como usuario no-root (buena práctica de seguridad)
RUN adduser --disabled-password --gecos "" appuser
USER appuser

EXPOSE 8000

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
