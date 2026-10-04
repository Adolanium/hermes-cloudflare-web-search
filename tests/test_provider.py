"""Run from a Hermes checkout with scripts/run_tests.sh and this file's path."""

import importlib
import json
import shutil
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread

import pytest

PLUGIN = Path(__file__).resolve().parents[1]
TOKEN_ENV = "CLOUDFLARE_WEB_SEARCH_API_TOKEN"


@contextmanager
def profile_scope(home):
    from agent.secret_scope import (
        build_profile_secret_scope,
        reset_secret_scope,
        set_secret_scope,
    )
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override

    home_token = set_hermes_home_override(home)
    secret_token = set_secret_scope(
        build_profile_secret_scope(home), profile_home=str(home)
    )
    try:
        yield
    finally:
        reset_secret_scope(secret_token)
        reset_hermes_home_override(home_token)


@pytest.fixture
def profiles(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "launch"))
    monkeypatch.setenv("HERMES_RUNTIME_DIR", str(tmp_path / "runtime"))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv(TOKEN_ENV, "fake-launch-token")
    from agent import secret_scope

    monkeypatch.setattr(secret_scope, "_MULTIPLEX_ACTIVE", True)
    homes = {}
    for name, vendor, alias in (
        ("a", "ceramic", ""),
        ("b", "exa", "team-key"),
        ("c", "linkup", ""),
    ):
        home = tmp_path / name
        target = home / "plugins" / "cloudflare-web-search"
        target.mkdir(parents=True)
        for filename in ("__init__.py", "provider.py", "plugin.yaml"):
            shutil.copy2(PLUGIN / filename, target / filename)
        settings = {
            "account_id": name * 32,
            "gateway_id": f"gateway-{name}",
            "provider": vendor,
            "byok_alias": alias,
        }
        config = {
            "plugins": {
                "enabled": ["cloudflare-web-search"],
                "entries": {"cloudflare-web-search": {"settings": settings}},
            },
            "web": {
                "search_backend": "cloudflare-web-search",
                "extract_backend": "firecrawl",
                "cache_enabled": False,
                "keyless_rescue": False,
            },
        }
        (home / "config.yaml").write_text(json.dumps(config), encoding="utf-8")
        (home / ".env").write_text(f"{TOKEN_ENV}=fake-token-{name}\n", encoding="utf-8")
        homes[name] = home
    return homes


@pytest.fixture
def api():
    requests = []
    reply = {"status": 200, "body": None}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            authorization = self.headers.get("Authorization")
            requests.append(
                {"path": self.path, "authorization": authorization, "json": payload}
            )
            body = reply["body"]
            if body is None:
                body = {
                    "items": [
                        {
                            "url": "https://example.com/guide",
                            "title": "Guide",
                            "description": authorization,
                        },
                        {"url": "https://example.com/reference", "title": "Reference"},
                        {"url": "https://example.com/more", "title": "More"},
                    ],
                    "metadata": {
                        "query": payload["query"],
                        "requestId": "test-request",
                    },
                }
            content = (body if isinstance(body, str) else json.dumps(body)).encode()
            self.send_response(reply["status"])
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/client/v4", requests, reply
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=5)


def load_provider(api, monkeypatch):
    from hermes_cli.plugins import discover_plugins
    from agent.web_search_registry import get_active_search_provider

    discover_plugins()
    provider = get_active_search_provider()
    assert provider.name == "cloudflare-web-search"
    monkeypatch.setattr(
        importlib.import_module(type(provider).__module__), "_API_ROOT", api[0]
    )
    return provider


def test_installed_backend_dispatches_with_each_profiles_settings_and_token(
    profiles, api, monkeypatch
):
    from agent.web_search_registry import get_active_extract_provider

    importlib.import_module("tools.web_tools")  # Registers the real tool handler.
    from tools.registry import registry

    providers = {}
    for name, vendor, alias in (
        ("a", "ceramic", ""),
        ("b", "exa", "team-key"),
        ("a", "ceramic", ""),
        ("c", "linkup", ""),
    ):
        with profile_scope(profiles[name]):
            provider = load_provider(api, monkeypatch)
            providers[name] = provider
            assert provider.is_available() is True
            assert provider.get_setup_schema()["env_vars"][0]["key"] == TOKEN_ENV
            assert get_active_extract_provider().name == "firecrawl"
            result = json.loads(
                registry.dispatch("web_search", {"query": "Hermes docs", "limit": 2})
            )
            assert result["data"]["web"] == [
                {
                    "url": "https://example.com/guide",
                    "title": "Guide",
                    "description": f"Bearer fake-token-{name}",
                    "position": 1,
                },
                {
                    "url": "https://example.com/reference",
                    "title": "Reference",
                    "description": "",
                    "position": 2,
                },
            ]
            expected = {
                "query": "Hermes docs",
                "provider": vendor,
                "limit": 10,
                "options": {"gateway": {"id": f"gateway-{name}"}},
            }
            if alias:
                expected["byokAlias"] = alias
            assert api[1][-1] == {
                "path": f"/client/v4/accounts/{name * 32}/ai/websearch/",
                "authorization": f"Bearer fake-token-{name}",
                "json": expected,
            }

    # Even a retained provider object must resolve settings for its caller's home.
    for name in ("a", "b", "a"):
        with profile_scope(profiles[name]):
            result = providers["a"].search("profile check", limit=99)
            assert (
                result["data"]["web"][0]["description"] == f"Bearer fake-token-{name}"
            )
            assert (
                api[1][-1]["path"] == f"/client/v4/accounts/{name * 32}/ai/websearch/"
            )
            assert api[1][-1]["json"]["limit"] == 10


@pytest.mark.parametrize(
    "case, expected",
    [
        ("missing-token", "set CLOUDFLARE_WEB_SEARCH_API_TOKEN"),
        ("bad-account", "32-character Cloudflare account ID"),
        ("bad-provider", "provider must be ceramic, exa, or linkup"),
        ("bad-alias", "byok_alias must contain"),
        ("empty-query", "query must contain 1-1024 characters"),
        ("long-query", "query must contain 1-1024 characters"),
        ("unauthorized", "request returned HTTP 401"),
        ("throttled", "request returned HTTP 429"),
        ("non-json", "invalid API response"),
        ("wrong-shape", "response must contain an items array"),
        ("bad-item", "each result must contain a title and URL"),
        ("empty-results", None),
    ],
)
def test_failures_and_empty_results_remain_distinct(
    profiles, api, monkeypatch, case, expected
):
    with profile_scope(profiles["a"]):
        provider = load_provider(api, monkeypatch)
        baseline = provider.search("control", limit=1)
        assert baseline["data"]["web"] == [
            {
                "title": "Guide",
                "url": "https://example.com/guide",
                "description": "Bearer fake-token-a",
                "position": 1,
            }
        ]

    config_file = profiles["a"] / "config.yaml"
    config = json.loads(config_file.read_text(encoding="utf-8"))
    edits = {
        "bad-account": ("account_id", "../other"),
        "bad-provider": ("provider", "unknown"),
        "bad-alias": ("byok_alias", "not/a/key"),
    }
    if case in edits:
        key, value = edits[case]
        config["plugins"]["entries"]["cloudflare-web-search"]["settings"][key] = value
        config_file.write_text(json.dumps(config), encoding="utf-8")
    if case == "missing-token":
        (profiles["a"] / ".env").write_text("", encoding="utf-8")
    responses = {
        "unauthorized": (401, {"errors": [{"message": "unauthorized"}]}),
        "throttled": (429, {"errors": [{"message": "rate limited"}]}),
        "non-json": (200, "not json"),
        "wrong-shape": (200, {"success": False, "errors": []}),
        "bad-item": (200, {"items": [{"url": 42}]}),
        "empty-results": (200, {"items": []}),
    }
    if case in responses:
        api[2]["status"], api[2]["body"] = responses[case]
    query = {"empty-query": " ", "long-query": "x" * 1025}.get(case, "search")
    with profile_scope(profiles["a"]):
        result = provider.search(query, limit=0)
        if expected is None:
            assert result == {"success": True, "data": {"web": []}}
            assert api[1][-1]["json"]["limit"] == 1
        else:
            assert result["success"] is False
            assert expected in result["error"]
            assert "fake-launch-token" not in result["error"]
            if case not in responses:
                assert len(api[1]) == 1
