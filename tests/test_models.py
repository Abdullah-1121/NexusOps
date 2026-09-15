"""_enforce_json strict-parsing contract (FR-4 / NFR-4)."""

import pytest

from app.models import ModelError, _enforce_json


def test_bare_json_parses():
    assert _enforce_json('{"a": 1}', "m") == {"a": 1}


def test_fenced_json_unwrapped_then_strict():
    # GLM via TokenRouter fences JSON even under strict response_format; the
    # fence is transport, unwrapped exactly — the body is still parsed strictly.
    assert _enforce_json('```json\n{"severity": "info", "ok": true}\n```', "m") == {
        "severity": "info",
        "ok": True,
    }


def test_garbage_is_loud():
    with pytest.raises(ModelError, match="unparsable JSON"):
        _enforce_json("not json at all", "m")


def test_fenced_garbage_is_still_loud():
    # fence tolerance must never smuggle in a broken body
    with pytest.raises(ModelError, match="unparsable JSON"):
        _enforce_json("```json\n{\"a\":\n```", "m")