"""
Schemas Pydantic del agente AIOps.

Define los modelos de entrada y salida para la API de extracción
de parámetros de infraestructura.
"""

from typing import Any
from pydantic import BaseModel, Field, field_validator

# ── Alertmanager Webhook Schemas (Fase 1 - Observabilidad) ────────────────────

class AlertItem(BaseModel):
    """Representa una sola alerta dentro del array de alertas de Alertmanager."""
    status: str = Field(description="firing or resolved")
    labels: dict[str, str] = Field(default_factory=dict, description="Etiquetas como alertname, pod, namespace")
    annotations: dict[str, str] = Field(default_factory=dict, description="Anotaciones extra como description, summary")
    startsAt: str
    endsAt: str | None = None
    generatorURL: str | None = None
    fingerprint: str | None = None

class AlertmanagerPayload(BaseModel):
    """El payload JSON completo (Data Contract) que empuja Alertmanager al Webhook."""
    receiver: str
    status: str = Field(description="firing or resolved")
    alerts: list[AlertItem] = Field(default_factory=list)
    groupLabels: dict[str, str] = Field(default_factory=dict)
    commonLabels: dict[str, str] = Field(default_factory=dict)
    commonAnnotations: dict[str, str] = Field(default_factory=dict)
    externalURL: str | None = None
    version: str | None = None
    groupKey: str | None = None


# ── Legacy Extraction Schemas (Fase 0) ────────────────────────────────────────
class InfraRequest(BaseModel):
    message: str

    @field_validator("message")
    @classmethod
    def message_not_empty(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("message cannot be empty")
        if len(v) > 2000:
            raise ValueError("message too long (max 2000 chars)")
        return v


class ExtractedParams(BaseModel):
    project_name:  str | None = None
    region:        str | None = None
    instance_type: str | None = None
    purpose:       str | None = None


class ExtractResponse(BaseModel):
    request_id:           str
    input_message:        str
    extracted_parameters: ExtractedParams | None
    validation_warnings:  list[str]
    raw_response:         str
    model_used:           str
    extraction_method:    str | None
    duration_ms:          int
