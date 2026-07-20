import json
from copy import deepcopy
from decimal import Decimal
from pathlib import Path

import pytest

from brigade import code_references


REFERENCE = {
    "schema": "brigade.code-reference.v1",
    "repository": "escoffier-labs/brigade",
    "revision": {"commit": "a" * 40},
    "file_path": "src/brigade/receipts_cmd.py",
    "qualified_name": "brigade.receipts_cmd._metadata_with_delta",
    "symbol_kind": "function",
    "source_span": {"start_line": 787, "line_count": 3},
    "change_kind": "changed",
}


def test_code_reference_v1_schema_is_strict_and_validates_the_canonical_example():
    schema_path = Path(__file__).parents[1] / "schemas" / "code-reference.v1.schema.json"
    schema = json.loads(schema_path.read_text())

    assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    assert schema["$id"].endswith("/code-reference.v1.schema.json")
    assert schema["$defs"]["source_span"] == {
        "type": "object",
        "additionalProperties": False,
        "required": ["start_line", "line_count"],
        "properties": {
            "start_line": {"type": "integer", "minimum": 1, "maximum": 9007199254740991},
            "line_count": {"type": "integer", "minimum": 1, "maximum": 9007199254740991},
        },
    }
    assert code_references.validate(REFERENCE) == REFERENCE
    assert code_references.canonical_json(REFERENCE) == json.dumps(REFERENCE, sort_keys=True, separators=(",", ":"))


@pytest.mark.parametrize(
    ("name", "reference"),
    [
        ("missing required field", {key: value for key, value in REFERENCE.items() if key != "revision"}),
        ("unknown field", {**REFERENCE, "unrecognized": True}),
        (
            "wrong source span type",
            {**REFERENCE, "source_span": {"start_line": "787", "line_count": 1}},
        ),
        ("source_span must contain start_line and line_count", {**REFERENCE, "source_span": {"start_line": 788}}),
        ("unversioned schema", {**REFERENCE, "schema": "brigade.code-reference.v2"}),
    ],
)
def test_code_reference_v1_schema_rejects_invalid_or_unpinned_references(name, reference):
    with pytest.raises(ValueError, match=name):
        code_references.validate(deepcopy(reference))


def test_code_reference_integer_vectors_are_normalized_without_runtime_schema_dependency():
    vectors_path = Path(__file__).parents[1] / "schemas" / "code-reference.v1.integer-vectors.json"
    vectors = json.loads(vectors_path.read_text())
    for vector in vectors["accept"]:
        reference = {
            **REFERENCE,
            "source_span": json.loads(vector["source_span_json"], parse_int=Decimal, parse_float=Decimal),
        }
        validated = code_references.validate(reference)
        assert validated["source_span"] == vector["canonical"], vector["name"]
        assert json.loads(code_references.canonical_json(reference))["source_span"] == vector["canonical"], vector[
            "name"
        ]
    for vector in vectors["reject"]:
        reference = {
            **REFERENCE,
            "source_span": json.loads(vector["source_span_json"], parse_int=Decimal, parse_float=Decimal),
        }
        with pytest.raises(ValueError):
            code_references.validate(reference)


@pytest.mark.parametrize("source_line", [1.0, json.loads("9007199254740991.1")])
def test_code_reference_mapping_validation_rejects_native_floats_even_when_they_look_integral(source_line):
    reference = {**REFERENCE, "source_span": {"start_line": source_line, "line_count": 1}}

    with pytest.raises(ValueError, match="wrong source span type"):
        code_references.validate(reference)


def test_code_reference_raw_json_parser_preserves_numeric_lexemes_for_the_shared_vectors():
    vectors_path = Path(__file__).parents[1] / "schemas" / "code-reference.v1.integer-vectors.json"
    vectors = json.loads(vectors_path.read_text())
    prefix = json.dumps({key: value for key, value in REFERENCE.items() if key not in {"source_span", "change_kind"}})
    prefix = prefix[:-1] + ',"source_span":'

    for vector in vectors["accept"]:
        reference = code_references.parse_json_reference(
            prefix + vector["source_span_json"] + ',"change_kind":"changed"}'
        )
        assert reference["source_span"] == vector["canonical"], vector["name"]
    for vector in vectors["reject"]:
        with pytest.raises(ValueError):
            code_references.parse_json_reference(prefix + vector["source_span_json"] + ',"change_kind":"changed"}')


def test_code_reference_canonical_json_matches_the_shared_cross_runtime_vector():
    vector_path = Path(__file__).parents[1] / "schemas" / "code-reference.v1.canonical-vector.json"
    vector = json.loads(vector_path.read_text(), parse_int=Decimal, parse_float=Decimal)

    canonical = code_references.canonical_json(vector["reference"])
    assert canonical == vector["canonical_json"]
    assert canonical.encode("utf-8") == vector["canonical_json"].encode("utf-8")
