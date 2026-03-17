"""
Validación de parámetros de infraestructura GCP.

Compara los parámetros extraídos contra valores conocidos de GCP
y genera warnings no bloqueantes.
"""

# Valores válidos de GCP — ampliar según las regiones permitidas en MasOrange
VALID_REGIONS: frozenset[str] = frozenset({
    "europe-west1", "europe-west2", "europe-west3", "europe-west4",
    "europe-southwest1", "us-central1", "us-east1", "us-west1",
    "asia-east1", "asia-northeast1",
})

VALID_INSTANCE_PREFIXES: tuple[str, ...] = (
    "e2-", "n1-", "n2-", "n2d-", "c2-", "m1-", "t2d-"
)


def validate_params(params: dict) -> list[str]:
    """
    Valida los parámetros extraídos contra valores conocidos de GCP.

    No bloquea la respuesta — genera warnings informativos que el llamante
    puede usar para decidir si escalar a revisión humana.
    """
    warnings: list[str] = []

    region = params.get("region")
    if region and region not in VALID_REGIONS:
        warnings.append(
            f"Unknown region '{region}' — verify it is a valid GCP region"
        )

    instance_type = params.get("instance_type")
    if instance_type and not any(
        instance_type.startswith(p) for p in VALID_INSTANCE_PREFIXES
    ):
        warnings.append(
            f"Unusual instance type '{instance_type}' — verify GCP machine type format"
        )

    for field in ("project_name", "region", "instance_type", "purpose"):
        if not params.get(field):
            warnings.append(f"Missing parameter: '{field}'")

    return warnings
