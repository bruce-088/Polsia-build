"""Web research via Tavily. Grounds agent research (competitor analysis,
prospect discovery) in real, current data instead of relying on the LLM's
own (possibly stale or invented) knowledge."""
from app.config import settings


def search_web(query: str, max_results: int = 5) -> list[dict]:
    """Run a real web search via Tavily. Raises RuntimeError if not
    configured, so callers can fall back to LLM-only reasoning and say so
    explicitly rather than silently guessing. Returns a list of
    {"title", "url", "content"} results."""
    if not settings.tavily_api_key:
        raise RuntimeError("Tavily is not configured (tavily_api_key)")

    from tavily import TavilyClient

    client = TavilyClient(api_key=settings.tavily_api_key)
    response = client.search(query, max_results=max_results)
    return [
        {"title": r.get("title"), "url": r.get("url"), "content": r.get("content")}
        for r in response.get("results", [])
    ]
