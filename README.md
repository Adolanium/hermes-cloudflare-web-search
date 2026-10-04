# hermes-cloudflare-web-search

Cloudflare Web Search provider plugin for [Hermes Agent](https://github.com/NousResearch/hermes-agent).

Use Ceramic, Exa, or Linkup through [Cloudflare Web Search API](https://developers.cloudflare.com/web-search/) with Hermes' existing `web_search` tool.

[![License](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Hermes](https://img.shields.io/badge/Hermes-plugin-6f42c1)](https://github.com/NousResearch/hermes-agent)

## Quick start

1. Install the plugin:

   ```sh
   hermes plugins install https://github.com/Adolanium/hermes-cloudflare-web-search
   ```

2. Enter a Cloudflare API token when prompted. The token needs **Account > Workers AI > Read** and **Account > AI Gateway > Read** permissions. Hermes stores it as `CLOUDFLARE_WEB_SEARCH_API_TOKEN` in the active profile's secret store. You can also enter it in the Desktop plugin settings or through `hermes tools`.

3. Open the plugin's settings under **Capabilities > Plugins** in Desktop and enter your account ID. Set the gateway ID, provider, and BYOK alias if needed. For CLI setup, merge this into the active profile's `config.yaml`, preserving other enabled plugins:

   ```yaml
   plugins:
     enabled:
       - cloudflare-web-search
     entries:
       cloudflare-web-search:
         settings:
           account_id: "YOUR_32_CHARACTER_ACCOUNT_ID"
           gateway_id: default
           provider: ceramic
           byok_alias: ""
   web:
     search_backend: cloudflare-web-search
   ```

4. Run `hermes tools` and select **Cloudflare Web Search** under **Web Search & Extract**, or set `web.search_backend` as above. Start a new conversation after installation.

The install command works on Windows, macOS, and Linux. The plugin searches only. Keep your existing `web.extract_backend` for page extraction. Cloudflare requires AI Gateway credits or a provider key stored in AI Gateway. An empty `byok_alias` lets Cloudflare use its default stored key, then gateway credits. A named alias must already exist.

See [Cloudflare's setup guide](https://developers.cloudflare.com/web-search/how-to-use/) for account, gateway, and token setup.

## Behavior and data handling

Each uncached search sends the query, result limit, selected provider, gateway ID, and optional BYOK alias to `api.cloudflare.com`. The account ID is in the request path, and the API token is in the Authorization header. Cloudflare forwards the search to the selected provider and records it in AI Gateway logs. Requests may consume gateway credits or provider quota. See [Cloudflare's provider documentation](https://developers.cloudflare.com/web-search/providers/) for prices and retention policies.

The plugin returns titles, URLs, descriptions, and numbered positions. It accepts queries up to 1,024 characters and requests at most 10 results, matching the beta API's limits. A missing optional description becomes an empty string. HTTP failures and malformed responses return a search error. Requests time out after 30 seconds.

Hermes controls caching and fallback behavior. Its keyless rescue may send a failed search to another service. Set `web.keyless_rescue: false` if searches must stay on your chosen backend.

Settings and credentials are read for the active profile on each call. The plugin does not add model tools, modify Hermes code, start background processes, or update itself. It reads only its own configuration and declared token through Hermes APIs, and writes no files itself.

## Verify

After configuring the plugin, ask Hermes to run a search:

```sh
hermes chat --toolsets web -q "Use web_search to find the Cloudflare Web Search API documentation."
```

This sends a live query and may consume Cloudflare credits or provider quota.

## Development

From a Hermes source checkout with its test environment prepared, run:

```sh
scripts/run_tests.sh /path/to/hermes-cloudflare-web-search/tests/test_provider.py -- -c /path/to/hermes-cloudflare-web-search/pytest.ini
hermes plugins validate /path/to/hermes-cloudflare-web-search --install-deps
```

The tests install the plugin into temporary profile directories, load it through Hermes discovery, and exercise `web_search` against a local HTTP server. They require no Cloudflare credentials. A live authenticated Cloudflare search remains a separate check.

## License

MIT. Cloudflare, Ceramic, Exa, and Linkup are trademarks of their respective owners. This is an independent community plugin, unaffiliated with Cloudflare or Nous Research.
