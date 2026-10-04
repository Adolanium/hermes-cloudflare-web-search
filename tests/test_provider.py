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


@pytest.fixture
def search_cache():
    from tools.web_result_cache import search_memo

    search_memo.clear()
    yield
    search_memo.clear()


def set_cache_mode(home, enabled):
    config_file = home / "config.yaml"
    config = json.loads(config_file.read_text(encoding="utf-8"))
    if enabled:
        # Omit the setting to exercise Hermes' default, not an explicit opt-in.
        config["web"].pop("cache_enabled")
    else:
        config["web"]["cache_enabled"] = False
    config_file.write_text(json.dumps(config), encoding="utf-8")


def dispatch_search(query):
    importlib.import_module("tools.web_tools")
    from tools.registry import registry

    return json.loads(registry.dispatch("web_search", {"query": query, "limit": 2}))


@pytest.mark.parametrize(
    "cache_enabled", [True, False], ids=["default-cache", "cache-disabled"]
)
@pytest.mark.parametrize(
    "field,value",
    [
        ("provider", "exa"),
        ("gateway_id", "new-gateway"),
        ("account_id", "d" * 32),
        ("byok_alias", "new-key"),
    ],
)
def test_search_cache_respects_saved_route_changes(
    profiles, api, monkeypatch, search_cache, request, cache_enabled, field, value
):
    from hermes_cli.config import save_config
    from hermes_cli.plugins_settings import save_plugin_settings

    home = profiles["a"]
    set_cache_mode(home, cache_enabled)
    api[2]["body"] = {
        "items": [
            {
                "title": "Route response",
                "url": "https://example.com/route",
                "description": "before",
            }
        ]
    }
    with profile_scope(home):
        load_provider(api, monkeypatch)
        for _ in range(2):
            assert dispatch_search("repeat query") == {
                "success": True,
                "data": {
                    "web": [
                        {
                            "title": "Route response",
                            "url": "https://example.com/route",
                            "description": "before",
                            "position": 1,
                        }
                    ]
                },
            }
        calls_before = len(api[1])
        assert calls_before == (1 if cache_enabled else 2)

        save_plugin_settings(
            "cloudflare-web-search",
            home / "plugins" / "cloudflare-web-search",
            {field: value},
        )
        api[2]["body"]["items"][0]["description"] = "after"
        changed = dispatch_search("repeat query")
        calls_after = len(api[1])

        # A new query proves that the real settings writer changed the outgoing route.
        fresh = dispatch_search("different query")
        assert fresh["data"]["web"][0]["description"] == "after"
        settings = {
            "account_id": "a" * 32,
            "gateway_id": "gateway-a",
            "provider": "ceramic",
            "byok_alias": "",
        }
        settings[field] = value
        payload = {
            "query": "different query",
            "provider": settings["provider"],
            "limit": 10,
            "options": {"gateway": {"id": settings["gateway_id"]}},
        }
        if settings["byok_alias"]:
            payload["byokAlias"] = settings["byok_alias"]
        assert api[1][-1] == {
            "path": f"/client/v4/accounts/{settings['account_id']}/ai/websearch/",
            "authorization": "Bearer fake-token-a",
            "json": payload,
        }

        save_config({"web": {"cache_enabled": False}}, merge_existing=True)
        assert (
            dispatch_search("repeat query")["data"]["web"][0]["description"] == "after"
        )

    # Mark only after setup and controls pass, so unrelated failures stay failures.
    if cache_enabled:
        request.node.add_marker(
            pytest.mark.xfail(
                strict=True,
                raises=AssertionError,
                reason="Hermes search cache omits routing settings; PR #132851 F1",
            )
        )
    assert {
        "description": changed["data"]["web"][0]["description"],
        "new_requests": calls_after - calls_before,
    } == {"description": "after", "new_requests": 1}


@pytest.mark.parametrize(
    "cache_enabled", [True, False], ids=["default-cache", "cache-disabled"]
)
def test_search_cache_isolates_profiles(
    profiles, api, monkeypatch, search_cache, request, cache_enabled
):
    descriptions = []
    for home in profiles.values():
        set_cache_mode(home, cache_enabled)
    for name in ("a", "b", "a"):
        with profile_scope(profiles[name]):
            provider = load_provider(api, monkeypatch)
            # Direct calls must honor the profile even when the host memo does not.
            assert (
                provider.search("direct profile control")["data"]["web"][0][
                    "description"
                ]
                == f"Bearer fake-token-{name}"
            )
            descriptions.append(
                dispatch_search("same profile query")["data"]["web"][0]["description"]
            )
    if cache_enabled:
        request.node.add_marker(
            pytest.mark.xfail(
                strict=True,
                raises=AssertionError,
                reason="Hermes search cache omits the active profile; upstream PR #95036",
            )
        )
    assert descriptions == [
        "Bearer fake-token-a",
        "Bearer fake-token-b",
        "Bearer fake-token-a",
    ]


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
