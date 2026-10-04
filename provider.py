"""Adapt Cloudflare's Web Search REST response to the Hermes search contract."""

from __future__ import annotations

import re

import httpx

from agent.secret_scope import get_secret
from agent.web_search_provider import WebSearchProvider

_API_ROOT = "https://api.cloudflare.com/client/v4"
_TOKEN_ENV = "CLOUDFLARE_WEB_SEARCH_API_TOKEN"


def _failure(message):
    return {"success": False, "error": f"Cloudflare Web Search: {message}"}


def _results(data, limit):
    if not isinstance(data, dict) or not isinstance(data.get("items"), list):
        raise ValueError("response must contain an items array")
    results = []
    for item in data["items"][:limit]:
        if not isinstance(item, dict) or not all(
            isinstance(item.get(key), str) for key in ("title", "url")
        ):
            raise ValueError("each result must contain a title and URL")
        description = item.get("description", "")
        if not isinstance(description, str):
            raise ValueError("result description must be a string")
        results.append(
            {
                "title": item["title"],
                "url": item["url"],
                "description": description,
                "position": len(results) + 1,
            }
        )
    return {"success": True, "data": {"web": results}}


class CloudflareWebSearchProvider(WebSearchProvider):
    def __init__(self, ctx):
        # Resolve settings on every call: one backend process can serve several profiles.
        self._get_config = ctx.get_config

    @property
    def name(self):
        return "cloudflare-web-search"

    @property
    def display_name(self):
        return "Cloudflare Web Search"

    def _settings(self):
        values = {}
        for key, default in (
            ("account_id", ""),
            ("gateway_id", "default"),
            ("provider", "ceramic"),
            ("byok_alias", ""),
        ):
            value = self._get_config(key, default)
            if not isinstance(value, str):
                raise ValueError(f"{key} must be a string in the plugin settings")
            values[key] = value.strip()
        if not re.fullmatch(r"[a-fA-F0-9]{32}", values["account_id"]):
            raise ValueError(
                "set account_id to your 32-character Cloudflare account ID"
            )
        if not values["gateway_id"]:
            raise ValueError("gateway_id must not be empty")
        if values["provider"] not in ("ceramic", "exa", "linkup"):
            raise ValueError("provider must be ceramic, exa, or linkup")
        if values["byok_alias"] and not re.fullmatch(
            r"[A-Za-z0-9_-]{1,64}", values["byok_alias"]
        ):
            raise ValueError(
                "byok_alias must contain 1-64 letters, digits, underscores, or hyphens"
            )
        return values

    def is_available(self):
        if not (get_secret(_TOKEN_ENV) or "").strip():
            return False
        try:
            self._settings()
        except ValueError:
            return False
        return True

    def get_setup_schema(self):
        return {
            "name": self.display_name,
            "badge": "paid",
            "tag": "Search only. Set account and gateway in the plugin settings.",
            "env_vars": [
                {
                    "key": _TOKEN_ENV,
                    "prompt": "Cloudflare token (Workers AI Read and AI Gateway Read)",
                    "url": "https://developers.cloudflare.com/web-search/how-to-use/",
                }
            ],
        }

    def search(self, query, limit=5):
        token = (get_secret(_TOKEN_ENV) or "").strip()
        if not token:
            return _failure(
                f"set {_TOKEN_ENV} through hermes tools or the plugin settings"
            )
        if not isinstance(query, str) or not query.strip() or len(query) > 1024:
            return _failure("query must contain 1-1024 characters")
        try:
            settings = self._settings()
        except ValueError as exc:
            return _failure(str(exc))
        count = max(1, min(int(limit), 10))
        payload = {
            "query": query,
            "provider": settings["provider"],
            "limit": count,
            "options": {"gateway": {"id": settings["gateway_id"]}},
        }
        if settings["byok_alias"]:
            payload["byokAlias"] = settings["byok_alias"]
        try:
            response = httpx.post(
                f"{_API_ROOT}/accounts/{settings['account_id']}/ai/websearch/",
                headers={
                    "Authorization": f"Bearer {token}",
                    "Accept": "application/json",
                },
                json=payload,
                timeout=30,
                follow_redirects=False,
            )
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            return _failure(f"request returned HTTP {exc.response.status_code}")
        except httpx.RequestError:
            return _failure("could not reach the API")
        try:
            return _results(response.json(), count)
        except ValueError as exc:
            return _failure(f"invalid API response: {exc}")
