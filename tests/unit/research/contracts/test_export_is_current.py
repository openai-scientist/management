import json
from pathlib import Path

from jsonschema import Draft202012Validator

from scripts.export_contract import DIRECTORIES, WIRE_MODELS, folder, render_contract

DOCS = Path(__file__).resolve().parents[4] / "docs" / "contract"


def test_export_is_current():
    expected = render_contract()
    actual_names = {
        path.relative_to(DOCS).as_posix()
        for directory in DIRECTORIES
        for path in (DOCS / directory).glob("*.json")
    }
    assert actual_names == set(expected), "Run: uv run python scripts/export_contract.py"
    for name, content in expected.items():
        assert (DOCS / name).read_text(encoding="utf-8") == content, (
            f"{name} is stale; run: uv run python scripts/export_contract.py"
        )


def test_every_wire_model_and_event_has_valid_example():
    files = render_contract()
    for name, model in WIRE_MODELS.items():
        example = files[f"{folder(model)}examples/{name}.json"]
        schema = json.loads(files[f"{folder(model)}schema/{name}.json"])
        model.model_validate_json(example)
        assert schema["title"] == name
        Draft202012Validator.check_schema(schema)
        Draft202012Validator(schema).validate(json.loads(example))
    events = [name for name in files if name.startswith("examples/event-")]
    from management.research.contracts import LIFECYCLE_TYPES, EventEnvelope

    assert {json.loads(files[name])["type"] for name in events} >= LIFECYCLE_TYPES
    for name in events:
        EventEnvelope.model_validate_json(files[name])
        Draft202012Validator(json.loads(files["schema/EventEnvelope.json"])).validate(
            json.loads(files[name])
        )
