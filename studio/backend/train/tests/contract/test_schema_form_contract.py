"""Workstream H — optional compatible field appears in schema without model-specific React."""

from quictrain_config import ActConfig, Pi05Config
from quictrain_model_specs import get_model


def _training_props(schema: dict) -> dict:
    training = schema.get("properties", {}).get("training", {})
    if "$ref" in training:
        ref = training["$ref"].rsplit("/", 1)[-1]
        training = schema["$defs"][ref]
    return training["properties"]


def test_schema_exposes_x_quic_and_optional_field_contract():
    schema = ActConfig.model_json_schema()
    training = _training_props(schema)
    steps = training["steps"]
    assert steps["x-quic"]["group"] == "training"
    assert steps["x-quic"]["visibility"] in {"basic", "advanced"}
    assert "editable_by" in steps["x-quic"]

    model = get_model("act")
    assert model.schema_hash
    assert "batch_size" in training
    assert steps["x-quic"]["order"] == 10


def test_pi05_schema_also_exposes_x_quic_without_model_specific_form():
    schema = Pi05Config.model_json_schema()
    training = _training_props(schema)
    assert "steps" in training
    assert training["steps"]["x-quic"]["group"] == "training"
    model = get_model("pi05")
    assert model.schema_hash
    assert model.schema["properties"] or model.schema.get("$defs")
    assert Pi05Config().training.batch_size == 1
