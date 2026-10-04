"""Cloudflare Web Search backend for Hermes Agent."""


def register(ctx):
    from .provider import CloudflareWebSearchProvider

    ctx.register_web_search_provider(CloudflareWebSearchProvider(ctx))
