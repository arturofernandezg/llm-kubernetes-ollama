"""
Tests del módulo tf_generator.

Verifica safe_name() y generate_terraform() con distintas combinaciones
de parámetros, incluyendo valores faltantes y caracteres especiales.
"""

import pytest

from tf_generator import safe_name, generate_terraform


# ── Datos de test ─────────────────────────────────────────────────────────────

FULL_AGENT_RESPONSE = {
    "request_id": "abc12345",
    "input_message": "Server for web-prod in europe-west1 with e2-standard-4",
    "model_used": "qwen2.5:1.5b",
    "extracted_parameters": {
        "project_name": "web-prod",
        "region": "europe-west1",
        "instance_type": "e2-standard-4",
        "purpose": "web traffic",
    },
}

EMPTY_AGENT_RESPONSE = {
    "request_id": "empty001",
    "input_message": "algo sin sentido",
    "model_used": "qwen2.5:1.5b",
    "extracted_parameters": None,
}


# ── safe_name ─────────────────────────────────────────────────────────────────

class TestSafeName:

    def test_basic_hyphen(self):
        assert safe_name("web-prod") == "web_prod"

    def test_special_chars(self):
        assert safe_name("My Project!") == "my_project"

    def test_empty_string(self):
        assert safe_name("") == "project"

    def test_only_special_chars(self):
        assert safe_name("---") == "project"

    def test_already_valid(self):
        assert safe_name("payments_api") == "payments_api"

    def test_uppercase(self):
        assert safe_name("WebProd") == "webprod"

    def test_numbers_preserved(self):
        assert safe_name("project-42") == "project_42"


# ── generate_terraform ────────────────────────────────────────────────────────

class TestGenerateTerraform:

    def test_all_params_present(self):
        tf = generate_terraform(FULL_AGENT_RESPONSE)
        assert 'project_name  = "web-prod"' in tf
        assert 'region        = "europe-west1"' in tf
        assert 'instance_type = "e2-standard-4"' in tf
        assert 'purpose       = "web traffic"' in tf

    def test_missing_params_uses_defaults(self):
        tf = generate_terraform(EMPTY_AGENT_RESPONSE)
        assert 'project_name  = "undefined-project"' in tf
        assert 'region        = "europe-west1"' in tf
        assert 'instance_type = "e2-standard-2"' in tf
        assert 'purpose       = "general purpose"' in tf

    def test_contains_terraform_block(self):
        tf = generate_terraform(FULL_AGENT_RESPONSE)
        assert "terraform {" in tf
        assert 'required_version = ">= 1.3"' in tf
        assert 'source  = "hashicorp/google"' in tf

    def test_contains_provider_block(self):
        tf = generate_terraform(FULL_AGENT_RESPONSE)
        assert 'provider "google"' in tf
        assert 'region  = "europe-west1"' in tf

    def test_contains_module_block(self):
        tf = generate_terraform(FULL_AGENT_RESPONSE)
        assert 'module "vm_web_prod"' in tf
        assert 'source  = "terraform-modules/gcp-vm"' in tf

    def test_contains_outputs(self):
        tf = generate_terraform(FULL_AGENT_RESPONSE)
        assert 'output "instance_name"' in tf
        assert 'output "instance_ip"' in tf
        assert 'output "instance_zone"' in tf

    def test_contains_required_labels(self):
        tf = generate_terraform(FULL_AGENT_RESPONSE)
        assert 'managed-by  = "aiops-agent"' in tf
        assert 'project     = "web_prod"' in tf
        assert 'environment = "production"' in tf
        assert 'created-by  = "terraform"' in tf

    def test_metadata_in_header(self):
        tf = generate_terraform(FULL_AGENT_RESPONSE)
        assert "Request ID:   abc12345" in tf
        assert "Modelo LLM:   qwen2.5:1.5b" in tf

    def test_safe_name_used_in_module(self):
        """Verifica que el nombre del módulo usa safe_name, no el raw."""
        response = {
            **FULL_AGENT_RESPONSE,
            "extracted_parameters": {
                **FULL_AGENT_RESPONSE["extracted_parameters"],
                "project_name": "My Cool Project!",
            },
        }
        tf = generate_terraform(response)
        assert 'module "vm_my_cool_project"' in tf
