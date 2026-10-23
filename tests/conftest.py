import pytest


@pytest.fixture(autouse=True)
def _no_prompt_override(monkeypatch):
    """The suite is written against the default prompt; a THESIS_PROMPT set on the worker (or in
    a shell) must not steer it. Tests that want another prompt set the variable themselves."""
    monkeypatch.delenv("THESIS_PROMPT", raising=False)
