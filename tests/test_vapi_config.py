"""vapi/*.json is generated from app/vapi.py. These fail if someone edits one but not the other."""

import json
from pathlib import Path

from app import shop, vapi

VAPI = Path(__file__).parent.parent / "vapi"


def test_committed_config_matches_the_code():
    assert json.loads((VAPI / "tools.json").read_text()) == vapi.registered_tools(), \
        "run `make vapi-config`"
    assert json.loads((VAPI / "assistant.json").read_text()) == vapi.assistant_definition(), \
        "run `make vapi-config`"


def test_every_tool_the_webhook_handles_is_registered_with_its_arguments():
    registered = {t["function"]["name"]: t["function"] for t in vapi.registered_tools()}
    assert list(registered) == list(vapi.TOOLS)
    for name, model in vapi.TOOLS.items():
        params = registered[name]["parameters"]
        assert set(params["properties"]) == set(model.model_fields)
        assert registered[name]["description"]
        assert all("title" not in spec for spec in params["properties"].values())
    assert registered["create_booking"]["parameters"]["required"] == ["when"]


def test_the_prompt_names_every_mechanic_and_forbids_date_arithmetic():
    for mechanic in shop.MECHANICS.values():
        assert mechanic in vapi.SYSTEM_PROMPT
    assert "Never work out a date yourself" in vapi.SYSTEM_PROMPT
