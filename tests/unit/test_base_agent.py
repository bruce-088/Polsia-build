"""Test BasePolsiaAgent.call_claude() — subprocess mock."""
import json
import os
import pytest
from unittest.mock import patch

from app.agents.base_agent import BasePolsiaAgent


class ConcreteAgent(BasePolsiaAgent):
    agent_type = "test"

    def run(self, task, context):
        return {"summary": "test"}


def test_call_claude_returns_mock_when_env_set(monkeypatch):
    monkeypatch.setenv("CLAUDE_CLI_MOCK", "true")
    monkeypatch.setenv("CLAUDE_CLI_MOCK_RESPONSE", json.dumps({"result": "hello from mock"}))

    agent = ConcreteAgent()
    result = agent.call_claude("test prompt")

    assert result == "hello from mock"


def test_call_claude_does_not_invoke_subprocess_in_mock_mode(monkeypatch):
    monkeypatch.setenv("CLAUDE_CLI_MOCK", "true")
    agent = ConcreteAgent()

    with patch("subprocess.run") as mock_run:
        agent.call_claude("test prompt")
        mock_run.assert_not_called()


def test_call_claude_json_parses_result(monkeypatch):
    monkeypatch.setenv("CLAUDE_CLI_MOCK", "true")
    monkeypatch.setenv(
        "CLAUDE_CLI_MOCK_RESPONSE",
        json.dumps({"result": '{"key": "value", "number": 42}'}),
    )

    agent = ConcreteAgent()
    result = agent.call_claude_json("test")
    assert result == {"key": "value", "number": 42}


def test_timed_run_adds_duration():
    agent = ConcreteAgent()
    result = agent.timed_run({"title": "test"}, {})
    assert "duration_secs" in result
    assert isinstance(result["duration_secs"], float)


def test_call_claude_structured_parses_clean_json(monkeypatch):
    monkeypatch.setenv("CLAUDE_CLI_MOCK", "true")
    monkeypatch.setenv(
        "CLAUDE_CLI_MOCK_RESPONSE", json.dumps({"result": '{"summary": "ok", "score": 5}'})
    )
    agent = ConcreteAgent()
    result = agent.call_claude_structured("test")
    assert result == {"summary": "ok", "score": 5}


def test_call_claude_structured_extracts_json_from_fenced_block_with_preamble(monkeypatch):
    """The response shape actually seen repeatedly in this codebase: an
    explanatory sentence before a ```json fenced object."""
    monkeypatch.setenv("CLAUDE_CLI_MOCK", "true")
    raw = 'I\'ll do X.\n\n```json\n{"summary": "ok", "file_path": "README.md"}\n```'
    monkeypatch.setenv("CLAUDE_CLI_MOCK_RESPONSE", json.dumps({"result": raw}))

    agent = ConcreteAgent()
    result = agent.call_claude_structured("test")
    assert result == {"summary": "ok", "file_path": "README.md"}


def test_call_claude_structured_falls_back_to_summary_when_truly_unparseable(monkeypatch):
    monkeypatch.setenv("CLAUDE_CLI_MOCK", "true")
    monkeypatch.setenv("CLAUDE_CLI_MOCK_RESPONSE", json.dumps({"result": "just plain text, no JSON here"}))

    agent = ConcreteAgent()
    result = agent.call_claude_structured("test")
    assert result == {"summary": "just plain text, no JSON here"}
