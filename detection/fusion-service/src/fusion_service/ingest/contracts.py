"""Read reference schemas from their owners; tolerate additive properties, validate known fields."""

import copy
import json
import math

from jsonschema import Draft202012Validator, FormatChecker

from fusion_service.domain.errors import Rejected, Unsupported
from fusion_service.domain.topics import TOPICS
from fusion_service.settings import Settings


def _allow_additions(schema):
    if isinstance(schema, dict):
        if schema.get("additionalProperties") is False:
            schema["additionalProperties"] = True
        for value in schema.values():
            _allow_additions(value)
    elif isinstance(schema, list):
        for value in schema:
            _allow_additions(value)


class Contracts:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.validators = {}
        for topic, contract in TOPICS.items():
            directory = settings.schema_dir(contract.subsystem)
            reference = json.loads((directory / f"{contract.filename}.v0.schema.json").read_text())
            Draft202012Validator.check_schema(reference)
            compatible = copy.deepcopy(reference)
            _allow_additions(compatible)
            if contract.event_type == "physical.event":
                # Physical's contract explicitly permits new event_type values in v0.
                # Preserve them as data rather than discard otherwise valid events.
                compatible["properties"]["event_type"] = {"type": "string", "minLength": 1}
            self.validators[topic] = Draft202012Validator(
                compatible, format_checker=FormatChecker()
            )

    def decode(self, topic: str, raw: bytes) -> dict:
        if topic not in TOPICS:
            raise Unsupported("unsupported_topic")
        if len(raw) > self.settings.max_message_bytes:
            raise Rejected("message_too_large")
        try:

            def invalid_constant(_):
                raise ValueError("nonfinite JSON")

            payload = json.loads(raw.decode("utf-8"), parse_constant=invalid_constant)
        except (ValueError, UnicodeError, RecursionError):
            raise Rejected("invalid_json") from None
        self._check_shape(payload)
        if not isinstance(payload, dict):
            raise Rejected("expected_object")
        expected = self.validators[topic].schema["properties"]["schema"]["const"]
        if payload.get("schema") != expected:
            raise Unsupported("unsupported_schema")
        if next(self.validators[topic].iter_errors(payload), None) is not None:
            raise Rejected("schema_validation_failed")
        source_id = (
            payload.get("event_id") if TOPICS[topic].subsystem == "camera" else payload.get("id")
        )
        if not source_id or len(source_id) > 256:
            raise Rejected("invalid_source_id")
        return payload

    def _check_shape(self, payload) -> None:
        """Reject nesting, non-finite numbers and invalid strings before schema validation."""
        pending = [(payload, 1)]
        while pending:
            value, depth = pending.pop()
            if depth > self.settings.max_message_depth:
                raise Rejected("message_too_deep")
            if isinstance(value, float) and not math.isfinite(value):
                raise Rejected("nonfinite_number")
            if isinstance(value, str):
                try:
                    value.encode("utf-8")
                except UnicodeError:
                    raise Rejected("invalid_unicode") from None
            if isinstance(value, dict):
                pending.extend((key, depth) for key in value)
                pending.extend((v, depth + 1) for v in value.values())
            elif isinstance(value, list):
                pending.extend((v, depth + 1) for v in value)
