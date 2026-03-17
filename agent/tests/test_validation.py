"""
Tests unitarios de la función validate_params().

Verifica la validación de regiones GCP, tipos de instancia y parámetros
faltantes.
"""

import pytest

from validation import validate_params
from tests.helpers import VALID_PARAMS


class TestValidateParams:

    def test_all_valid_params_no_warnings(self):
        assert validate_params(VALID_PARAMS) == []

    def test_invalid_region_generates_warning(self):
        params = {**VALID_PARAMS, "region": "marte-1"}
        warnings = validate_params(params)
        assert any("region" in w.lower() for w in warnings)

    def test_invalid_instance_type_generates_warning(self):
        params = {**VALID_PARAMS, "instance_type": "grande"}
        warnings = validate_params(params)
        assert any("instance type" in w.lower() for w in warnings)

    def test_null_region_generates_missing_warning(self):
        params = {**VALID_PARAMS, "region": None}
        warnings = validate_params(params)
        assert any("region" in w.lower() for w in warnings)

    def test_all_null_generates_four_warnings(self):
        params = {k: None for k in VALID_PARAMS}
        assert len(validate_params(params)) == 4

    @pytest.mark.parametrize("instance", [
        "e2-standard-4", "n1-standard-2", "n2-standard-8",
        "n2d-standard-4", "c2-standard-4", "t2d-standard-1",
    ])
    def test_valid_instance_prefixes_no_warning(self, instance):
        params = {**VALID_PARAMS, "instance_type": instance}
        warnings = validate_params(params)
        assert not any("instance type" in w.lower() for w in warnings)
