from datetime import timedelta
from typing import Any

import pytest

from kelpie.connectors.llm_quotas.config import load_llm_quotas
from kelpie.connectors.llm_quotas.source import DEFAULT_HEADER_FIELDS, ProbeKind


def entry(**kw: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "name": "main",
        "kind": "headers",
        "api_url": "https://provider.example",
        "path": "/v1/models",
        "token_env": "MAIN_KEY",
    }
    base.update(kw)
    return {k: v for k, v in base.items() if v is not None}


def test_absent_section_is_off() -> None:
    assert load_llm_quotas(None) is None


def test_minimal_provider_gets_defaults() -> None:
    settings = load_llm_quotas({"providers": [entry()]})
    assert settings is not None
    (p,) = settings.providers
    assert p.kind is ProbeKind.HEADERS
    assert p.fields == DEFAULT_HEADER_FIELDS
    assert (p.auth_header, p.auth_scheme) == ("Authorization", "Bearer")
    assert p.interval == timedelta(minutes=5) and p.timeout == timedelta(seconds=10)
    assert p.display_url == "https://provider.example"
    assert settings.tick_budget == timedelta(seconds=10)
    assert settings.build(environ={"MAIN_KEY": "k"}).tick_budget == 10.0
    built = p.build({"MAIN_KEY": "k"})
    assert built.request_headers()["Authorization"] == "Bearer k"


def test_full_status_url_provider() -> None:
    settings = load_llm_quotas(
        {
            "providers": [
                entry(
                    kind="status_url",
                    api_url="http://127.0.0.1:8080/api",
                    path="/key?verbose=1",
                    auth_header="x-api-key",
                    auth_scheme="",
                    headers={"x-api-version": "2"},
                    fields={"remaining": "data.left", "expires_at": "key.expires_at"},
                    unit="tokens",
                    interval="2m",
                    timeout="3s",
                ),
                entry(name="second", api_url="http://localhost:9"),
            ]
        }
    )
    assert settings is not None
    p = settings.providers[0]
    assert p.fields == {"remaining": "data.left", "expires_at": "key.expires_at"}
    assert p.build({"MAIN_KEY": "k"}).request_headers()["x-api-key"] == "k"
    assert p.build({"MAIN_KEY": "k"}).request_path() == "/api/key?verbose=1"
    assert p.interval == timedelta(minutes=2) and p.unit == "tokens"


@pytest.mark.parametrize(
    ("section", "message"),
    [
        ("x", "must be a table"),
        ({"other": 1}, "unknown"),
        ({"providers": []}, "at least one"),
        ({"providers": ["x"]}, "must be a table"),
        ({"providers": [entry()] * 21}, "at most 20"),
        ({"providers": [entry(), entry()]}, "unique"),
        ({"providers": [entry(name="bad name")]}, "name must be"),
        ({"providers": [entry(extra=1)]}, "unknown keys"),
        ({"providers": [entry(kind="sdk")]}, "kind must be"),
        ({"providers": [entry(api_url=None)]}, "api_url is required"),
        ({"providers": [entry(api_url="ftp://provider.example")]}, "https URL"),
        ({"providers": [entry(api_url="https://provider.example:0")]}, "https URL"),
        ({"providers": [entry(api_url="https://provider.example:x")]}, "invalid port"),
        ({"providers": [entry(api_url="https://u:p@provider.example")]}, "credentials"),
        ({"providers": [entry(api_url="https://provider.example/?k=1")]}, "query"),
        ({"providers": [entry(api_url="http://provider.example")]}, "must use https"),
        ({"providers": [entry(path="v1/models")]}, "path must be"),
        ({"providers": [entry(path="//elsewhere.example/x")]}, "path must be"),
        ({"providers": [entry(path="/a b")]}, "path must be"),
        ({"providers": [entry(path="/a#frag")]}, "path must be"),
        ({"providers": [entry(token_env="1BAD")]}, "token_env"),
        ({"providers": [entry(auth_header="bad header")]}, "auth_header must be"),
        ({"providers": [entry(auth_header="Cookie")]}, "auth_header cannot"),
        ({"providers": [entry(auth_scheme="Bearer x")]}, "auth_scheme"),
        ({"providers": [entry(headers=[])]}, "headers must be a table"),
        ({"providers": [entry(headers={"bad name": "x"})]}, "not a header name"),
        ({"providers": [entry(headers={"authorization": "x"})]}, "cannot set"),
        ({"providers": [entry(headers={"Host": "x"})]}, "cannot set"),
        ({"providers": [entry(headers={"x-a": "1\n2"})]}, "printable ASCII"),
        ({"providers": [entry(kind="status_url")]}, "needs [fields]"),
        ({"providers": [entry(fields={})]}, "non-empty table"),
        ({"providers": [entry(fields={"spent": "x"})]}, "unknown fields"),
        ({"providers": [entry(fields={"remaining": "bad name"})]}, "header name"),
        ({"providers": [entry(kind="status_url", fields={"limit": "a..b"})]}, "JSON path"),
        ({"providers": [entry(unit="x" * 40)]}, "unit"),
        ({"providers": [entry(interval="10s")]}, "at least"),
        ({"providers": [entry(interval="soon")]}, "interval"),
        ({"providers": [entry(timeout="0s")]}, "positive"),
        ({"providers": [entry(timeout="2m")]}, "at most"),
        ({"providers": [entry()], "tick_budget": "2m"}, "tick_budget must be between"),
        ({"providers": [entry()], "tick_budget": "soon"}, "tick_budget"),
    ],
)
def test_invalid_config(section: Any, message: str) -> None:
    with pytest.raises(ValueError, match=message.replace("[", r"\[")):
        load_llm_quotas(section)
