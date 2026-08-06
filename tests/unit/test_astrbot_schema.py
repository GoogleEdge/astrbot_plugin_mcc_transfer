import json
from pathlib import Path

SCHEMA_PATH = Path(__file__).parents[2] / "_conf_schema.json"


def _assert_astrbot_schema(node, path=""):
    for name, field in node.items():
        assert "type" in field, f"missing type: {path}{name}"
        if field["type"] == "object":
            assert "items" in field, f"missing items: {path}{name}"
            _assert_astrbot_schema(field["items"], f"{path}{name}.")


def test_every_object_schema_has_items():
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    _assert_astrbot_schema(schema)


def test_schema_uses_astrbot_type_names():
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    supported = {"string", "text", "int", "float", "bool", "object", "list", "dict", "file"}

    def visit(node):
        for field in node.values():
            assert field["type"] in supported
            if field["type"] == "object":
                visit(field["items"])

    visit(schema)
