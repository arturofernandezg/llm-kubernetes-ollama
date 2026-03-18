"""
Tests unitarios de la función extract_json().

Verifica las 3 estrategias de extracción: JSON directo, bloque markdown
y regex fallback.
"""

from extraction import extract_json
from tests.helpers import VALID_PARAMS, VALID_JSON_STR


class TestExtractJson:

    def test_direct_json(self):
        result, method = extract_json(VALID_JSON_STR)
        assert method == "direct"
        assert result == VALID_PARAMS

    def test_direct_json_with_surrounding_whitespace(self):
        result, method = extract_json(f"  {VALID_JSON_STR}  \n")
        assert method == "direct"
        assert result is not None

    def test_markdown_block_with_json_tag(self):
        text = f"Aquí tienes el resultado:\n```json\n{VALID_JSON_STR}\n```"
        result, method = extract_json(text)
        assert method == "markdown_block"
        assert result["region"] == "europe-west1"

    def test_markdown_block_without_json_tag(self):
        text = f"Resultado:\n```\n{VALID_JSON_STR}\n```"
        result, method = extract_json(text)
        assert method == "markdown_block"
        assert result is not None

    def test_regex_fallback(self):
        text = f"El objeto JSON sería {VALID_JSON_STR} espero que ayude."
        result, method = extract_json(text)
        assert method == "regex_search"
        assert result["purpose"] == "web server"

    def test_no_json_returns_none(self):
        result, method = extract_json("No hay JSON aquí, solo texto plano.")
        assert result is None
        assert method is None

    def test_malformed_json_returns_none(self):
        result, method = extract_json('{"project_name": "test", "region":}')
        assert result is None
        assert method is None

    def test_empty_string_returns_none(self):
        result, method = extract_json("")
        assert result is None
        assert method is None

    def test_prefers_direct_over_regex(self):
        result, method = extract_json(VALID_JSON_STR)
        assert method == "direct"

    def test_nested_json_regex_fallback(self):
        nested = '{"project_name": "web", "meta": {"env": "prod"}, "region": "europe-west1"}'
        text = f"Here is the result: {nested} hope it helps."
        result, method = extract_json(text)
        assert result is not None
        assert result["project_name"] == "web"
        assert result["meta"]["env"] == "prod"
        assert method == "regex_search"

    def test_markdown_block_with_nested_json(self):
        nested = '{"project_name": "web", "meta": {"env": "prod"}}'
        text = f"```json\n{nested}\n```"
        result, method = extract_json(text)
        assert method == "markdown_block"
        assert result["meta"]["env"] == "prod"
