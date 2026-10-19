from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any

import pytest

from kelpie.selftest.mock_llm import MODEL, TEXT, MockLLM


def post(url: str, body: bytes) -> tuple[int, bytes]:
    request = urllib.request.Request(url, body, {"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def chat(llm: MockLLM, **request: Any) -> tuple[int, bytes]:
    return post(f"{llm.base_url}/chat/completions", json.dumps(request).encode())


def test_always_the_same_tool_call() -> None:
    with MockLLM("sleep 1") as llm:
        bodies = [json.loads(chat(llm, model=MODEL, tools=[{}], messages=[])[1]) for _ in "ab"]
        assert llm.calls == 2 and llm.last_call is not None
    calls = [b["choices"][0]["message"]["tool_calls"][0] for b in bodies]
    assert [c["function"] for c in calls] == [
        {"name": "terminal", "arguments": '{"command": "sleep 1"}'}
    ] * 2
    assert calls[0]["id"] != calls[1]["id"]
    assert bodies[0]["choices"][0]["finish_reason"] == "tool_calls"
    assert bodies[0]["usage"]["total_tokens"] == 0


def test_without_tools_a_line_of_text() -> None:
    with MockLLM("x") as llm:
        status, body = chat(llm, messages=[])
    message = json.loads(body)["choices"][0]["message"]
    assert status == 200 and message == {"role": "assistant", "content": TEXT}


def test_streaming() -> None:
    with MockLLM("echo hi") as llm:
        status, body = chat(llm, tools=[{}], stream=True)
    events = [line[6:] for line in body.decode().splitlines() if line.startswith("data: ")]
    assert status == 200 and events[-1] == "[DONE]"
    first = json.loads(events[0])["choices"][0]["delta"]
    assert first["tool_calls"][0]["index"] == 0
    assert first["tool_calls"][0]["function"]["arguments"] == '{"command": "echo hi"}'
    assert json.loads(events[1])["choices"][0]["finish_reason"] == "tool_calls"


def test_models_and_nothing_else() -> None:
    with MockLLM("x") as llm:
        with urllib.request.urlopen(f"{llm.base_url}/models", timeout=5) as response:
            assert json.load(response)["data"][0]["id"] == MODEL
        assert post(f"{llm.base_url}/embeddings", b"{}")[0] == 404
        with pytest.raises(urllib.error.HTTPError) as info:
            urllib.request.urlopen(f"{llm.base_url}/other", timeout=5)
        assert info.value.code == 404
        assert post(f"{llm.base_url}/chat/completions", b"not json")[0] == 400
        assert post(f"{llm.base_url}/chat/completions", b"[]")[0] == 400
        assert post(f"{llm.base_url}/chat/completions", b"")[0] == 411
        assert llm.calls == 0
    assert llm.base_url.startswith("http://127.0.0.1:")
