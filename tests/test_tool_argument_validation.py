"""Arguments must obey the schema even when provider strict mode is disabled."""
from __future__ import annotations

import unittest
from unittest.mock import patch

from tsm_agt.core.tool import InvalidToolArguments, validate_tool_arguments
from tsm_agt.ports import ToolRisk, ToolSpec


class ToolArgumentValidationTest(unittest.TestCase):
    def validate(self, schema, arguments):
        validate_tool_arguments(ToolSpec("fixture.validate", "Validate arguments", schema, ToolRisk.R0), arguments)

    def test_string_constraints_and_enums_are_enforced(self):
        schema = {"type": "object", "properties": {
            "resource": {"type": "string", "minLength": 1, "maxLength": 8},
            "mode": {"type": "string", "enum": ["read", "list"]},
        }}
        for args, field in [({"resource": ""}, "resource"), ({"resource": "a" * 9}, "resource"),
                            ({"mode": "write"}, "mode"), ({"resource": 7}, "resource")]:
            with self.subTest(args=args), self.assertRaisesRegex(InvalidToolArguments, field):
                self.validate(schema, args)
        self.validate(schema, {"resource": "SKILL.md", "mode": "read"})

    def test_nested_arrays_objects_and_numeric_bounds_are_enforced(self):
        schema = {"type": "object", "properties": {"checks": {
            "type": "array", "minItems": 1, "maxItems": 2, "items": {
                "type": "object", "required": ["limit"], "additionalProperties": False,
                "properties": {"limit": {"type": "integer", "minimum": 1, "maximum": 10}},
            },
        }}}
        for args in [{"checks": []}, {"checks": [{"limit": 0}]}, {"checks": [{}]},
                     {"checks": [{"limit": True}]}, {"checks": [{"limit": 11}]},
                     {"checks": [{"limit": 1, "unknown": 1}]},
                     {"checks": [{"limit": 1}] * 3}]:
            with self.subTest(args=args), self.assertRaises(InvalidToolArguments):
                self.validate(schema, args)
        self.validate(schema, {"checks": [{"limit": 1}, {"limit": 10}]})

    def test_union_composition_pattern_and_const(self):
        schema = {"type": "object", "properties": {
            "value": {"anyOf": [{"type": "null"}, {"type": "string", "pattern": "^ok$"}]},
            "version": {"const": 1},
            "optional": {"type": ["string", "null"]},
            "one": {"oneOf": [{"type": "integer"}, {"type": "number"}]},
        }}
        self.validate(schema, {"value": None, "version": 1, "optional": None, "one": 1.5})
        for args in [{"value": "wrong"}, {"version": 2}, {"optional": 7}, {"one": 1}]:
            with self.subTest(args=args), self.assertRaises(InvalidToolArguments):
                self.validate(schema, args)

    def test_local_refs_work_without_network_and_defaults_do_not_mutate_arguments(self):
        schema = {"type": "object", "$defs": {"name": {"type": "string", "minLength": 1}},
                  "properties": {"name": {"$ref": "#/$defs/name"}, "mode": {"default": "read"}}}
        args = {"name": "valid"}
        with patch("urllib.request.urlopen", side_effect=AssertionError("must not fetch schemas")):
            self.validate(schema, args)
            with self.assertRaises(InvalidToolArguments):
                self.validate(schema, {"name": ""})
            with self.assertRaisesRegex(InvalidToolArguments, "unresolved"):
                self.validate({"type": "object", "$ref": "https://example.invalid/schema"}, {})
        self.assertEqual(args, {"name": "valid"})

    def test_error_names_nested_field_and_rule_without_echoing_argument_value(self):
        schema = {"type": "object", "properties": {"secret": {"type": "string", "maxLength": 2}}}
        with self.assertRaises(InvalidToolArguments) as raised:
            self.validate(schema, {"secret": "private-secret-value"})
        self.assertIn("arguments.secret", str(raised.exception))
        self.assertIn("at most 2", str(raised.exception))
        self.assertNotIn("private-secret-value", str(raised.exception))

    def test_invalid_schema_fails_closed(self):
        with self.assertRaisesRegex(InvalidToolArguments, "invalid.*schema"):
            self.validate({"type": "object", "properties": {"x": {"minLength": -1}}}, {})
