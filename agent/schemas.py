"""
Schemas Pydantic del agente AIOps.

Define los modelos de entrada y salida para la API de extracción
de parámetros de infraestructura.
"""

from pydantic import BaseModel, field_validator


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
