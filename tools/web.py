"""Web tools: search, fetch, summarise and YouTube.

This is the engineered replacement for ``RealtimeSearchEngine.py``. Changes:

* search is an abstraction - DuckDuckGo (no key at all), Tavily, Brave or your own
  SearXNG instance, chosen from config, so a quota running out is not fatal
* results are returned as real, linkable data.  If the search failed, JARVIS says
  so; it never invents sources
* page text is stripped with ``html.parser`` (not regex) and capped before it
  reaches a model, which matters both for token cost and for Pi memory
"""

from __future__ import annotations

import html as html_module
import re
import urllib.parse
from html.parser import HTMLParser
from typing import Any, Dict, List, Optional

from ai.http import HttpError, request_json, request_text, with_query
from core.logging_setup import get_logger
from tools.base import ToolContext, ToolResult, tool

log = get_logger("tools.web")


# --------------------------------------------------------------------------- #
# HTML -> text
# --------------------------------------------------------------------------- #
class _TextExtractor(HTMLParser):
    SKIP = {"script", "style", "noscript", "svg", "head", "nav", "footer", "form"}
    BLOCK = {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "section", "article"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: List[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs: Any) -> None:
        if tag in self.SKIP:
            self._skip_depth += 1
        elif tag in self.BLOCK:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in self.SKIP and self._skip_depth:
            self._skip_depth -= 1
        elif tag in self.BLOCK:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._skip_depth and data.strip():
            self.parts.append(data.strip() + " ")

    def text(self) -> str:
        raw = "".join(self.parts)
        raw = re.sub(r"[ \t]+", " ", raw)
        raw = re.sub(r"\n\s*\n\s*\n+", "\n\n", raw)
        return raw.strip()


def html_to_text(markup: str, limit: int = 20000) -> str:
    parser = _TextExtractor()
    try:
        parser.feed(markup)
        parser.close()
    except Exception:  # broken markup is normal on the web
        pass
    text = parser.text()
    if not text:
        text = re.sub(r"<[^>]+>", " ", markup)
        text = html_module.unescape(text)
        text = re.sub(r"\s+", " ", text).strip()
    return text[: max(200, limit)]


# --------------------------------------------------------------------------- #
# search providers
# --------------------------------------------------------------------------- #
_DDG_LINK = re.compile(
    r'<a[^>]+class="[^"]*result__a[^"]*"[^>]*href="([^"]+)"[^>]*>(.*?)</a>', re.S | re.I
)
_DDG_SNIPPET = re.compile(
    r'<a[^>]+class="[^"]*result__snippet[^"]*"[^>]*>(.*?)</a>', re.S | re.I
)
_DDG_LITE_LINK = re.compile(
    r"<a[^>]+class=['\"]result-link['\"][^>]*href=['\"]([^'\"]+)['\"][^>]*>(.*?)</a>", re.S | re.I
)


def _strip_tags(fragment: str) -> str:
    return html_module.unescape(re.sub(r"<[^>]+>", "", fragment or "")).strip()


def _decode_ddg_url(url: str) -> str:
    if "duckduckgo.com/l/" in url or url.startswith("/l/"):
        parsed = urllib.parse.urlparse(url if url.startswith("http") else "https://duckduckgo.com" + url)
        target = urllib.parse.parse_qs(parsed.query).get("uddg")
        if target:
            return target[0]
    if url.startswith("//"):
        return "https:" + url
    return url


BROWSER_UA = (
    "Mozilla/5.0 (X11; Linux aarch64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/122.0.0.0 Safari/537.36"
)


def _duckduckgo_html(query: str, max_results: int, timeout: float) -> List[Dict[str, str]]:
    """Scrape DuckDuckGo's keyless HTML endpoint.

    Home connections get real result pages; some datacentre IPs get a bot
    challenge instead, which is detected and reported rather than silently
    returning nothing.
    """
    results: List[Dict[str, str]] = []
    headers = {
        "User-Agent": BROWSER_UA,
        "Accept": "text/html,application/xhtml+xml",
        "Accept-Language": "en-US,en;q=0.9",
    }
    endpoints = (
        ("https://html.duckduckgo.com/html/" + urllib.parse.urlencode({"q": query}), None),
        ("https://lite.duckduckgo.com/lite/", urllib.parse.urlencode({"q": query}).encode()),
    )
    for endpoint, body in endpoints:
        try:
            markup = request_text(
                endpoint,
                method="POST" if body else "GET",
                data=body,
                headers=headers,
                timeout=timeout,
                retries=0,
            )
        except HttpError as exc:
            log.debug("duckduckgo endpoint failed: %s", exc)
            continue

        links = _DDG_LINK.findall(markup) or _DDG_LITE_LINK.findall(markup)
        if not links:
            log.info("duckduckgo returned no parseable results (%d bytes)", len(markup))
            continue
        snippets = [_strip_tags(item) for item in _DDG_SNIPPET.findall(markup)]
        for index, (href, title_html) in enumerate(links):
            url = _decode_ddg_url(href)
            title = _strip_tags(title_html)
            if not url.startswith("http") or not title:
                continue
            snippet = snippets[index] if index < len(snippets) else ""
            results.append(
                {"title": title, "url": url, "snippet": snippet, "provider": "duckduckgo"}
            )
            if len(results) >= max_results:
                return results
        if results:
            return results
    return results


def _duckduckgo_instant(query: str, max_results: int, timeout: float) -> List[Dict[str, str]]:
    """DuckDuckGo's official keyless Instant Answer API.

    Narrower than a web search (definitions, disambiguation, related topics) but
    it does not block datacentre IPs, so it keeps no-key installs working.
    """
    url = with_query(
        "https://api.duckduckgo.com/",
        {"q": query, "format": "json", "no_html": 1, "no_redirect": 1, "skip_disambig": 0},
    )
    try:
        data = request_json(url, headers={"User-Agent": BROWSER_UA}, timeout=timeout, retries=0)
    except Exception as exc:  # noqa: BLE001
        log.debug("duckduckgo instant answers failed: %s", exc)
        return []
    if not isinstance(data, dict):
        return []

    results: List[Dict[str, str]] = []
    if data.get("AbstractText"):
        results.append(
            {
                "title": data.get("Heading") or query,
                "url": data.get("AbstractURL") or "https://duckduckgo.com/?q=" + urllib.parse.quote(query),
                "snippet": str(data["AbstractText"])[:300],
                "provider": "duckduckgo-instant",
            }
        )
    if data.get("Answer"):
        results.append(
            {
                "title": "DuckDuckGo answer",
                "url": "https://duckduckgo.com/?q=" + urllib.parse.quote(query),
                "snippet": _strip_tags(str(data["Answer"]))[:300],
                "provider": "duckduckgo-instant",
            }
        )
    flat: List[Dict[str, Any]] = []
    for item in data.get("RelatedTopics") or []:
        if isinstance(item, dict) and item.get("Topics"):
            flat.extend(item["Topics"])
        elif isinstance(item, dict):
            flat.append(item)
    for item in flat:
        if not item.get("FirstURL"):
            continue
        results.append(
            {
                "title": _strip_tags(str(item.get("Text", "")))[:120] or "related",
                "url": str(item["FirstURL"]),
                "snippet": _strip_tags(str(item.get("Text", "")))[:300],
                "provider": "duckduckgo-instant",
            }
        )
        if len(results) >= max_results:
            break
    return results[:max_results]


def _wikipedia(query: str, max_results: int, timeout: float) -> List[Dict[str, str]]:
    """Keyless last resort with real, citable results."""
    url = with_query(
        "https://en.wikipedia.org/w/api.php",
        {"action": "query", "list": "search", "srsearch": query, "format": "json", "srlimit": max_results},
    )
    try:
        data = request_json(url, headers={"User-Agent": BROWSER_UA}, timeout=timeout, retries=0)
    except Exception:  # noqa: BLE001
        return []
    hits = (((data or {}).get("query") or {}).get("search")) or []
    return [
        {
            "title": str(item.get("title", "")),
            "url": "https://en.wikipedia.org/wiki/" + urllib.parse.quote(str(item.get("title", "")).replace(" ", "_")),
            "snippet": _strip_tags(str(item.get("snippet", "")))[:300],
            "provider": "wikipedia",
        }
        for item in hits
        if item.get("title")
    ]


def _tavily(query: str, max_results: int, timeout: float, api_key: str) -> List[Dict[str, str]]:
    payload = {"api_key": api_key, "query": query, "max_results": max_results, "search_depth": "basic"}
    data = request_json("https://api.tavily.com/search", payload=payload, timeout=timeout, retries=0)
    found = (data or {}).get("results") or []
    return [
        {
            "title": str(item.get("title", "")).strip(),
            "url": str(item.get("url", "")).strip(),
            "snippet": str(item.get("content", "")).strip()[:300],
        }
        for item in found
        if item.get("url")
    ][:max_results]


def _brave(query: str, max_results: int, timeout: float, api_key: str) -> List[Dict[str, str]]:
    url = with_query(
        "https://api.search.brave.com/res/v1/web/search",
        {"q": query, "count": max_results},
    )
    data = request_json(
        url, headers={"X-Subscription-Token": api_key, "Accept": "application/json"},
        timeout=timeout, retries=0,
    )
    found = ((data or {}).get("web") or {}).get("results") or []
    return [
        {
            "title": str(item.get("title", "")).strip(),
            "url": str(item.get("url", "")).strip(),
            "snippet": _strip_tags(str(item.get("description", "")))[:300],
        }
        for item in found
        if item.get("url")
    ][:max_results]


def _searxng(query: str, max_results: int, timeout: float, base: str) -> List[Dict[str, str]]:
    url = with_query(base.rstrip("/") + "/search", {"q": query, "format": "json"})
    data = request_json(url, timeout=timeout, retries=0)
    found = (data or {}).get("results") or []
    return [
        {
            "title": str(item.get("title", "")).strip(),
            "url": str(item.get("url", "")).strip(),
            "snippet": str(item.get("content", "")).strip()[:300],
        }
        for item in found
        if item.get("url")
    ][:max_results]


def run_search(query: str, settings: Any, max_results: int, timeout: float = 15.0) -> List[Dict[str, str]]:
    """Search with the configured provider, then the keyless fallback chain.

    A configured Tavily/Brave/SearXNG key is tried first.  With no key at all
    DuckDuckGo's HTML endpoint is primary (it works on home connections), backed
    by DuckDuckGo's official Instant Answer API and Wikipedia so a keyless
    install still returns something real and citable.
    """
    attempts: List[tuple] = []
    provider = "duckduckgo"
    try:
        provider = settings.search.active
        if provider == "tavily":
            attempts.append(("tavily", lambda: _tavily(query, max_results, timeout, settings.search.tavily_api_key)))
        elif provider == "brave":
            attempts.append(("brave", lambda: _brave(query, max_results, timeout, settings.search.brave_api_key)))
        elif provider == "searxng":
            attempts.append(("searxng", lambda: _searxng(query, max_results, timeout, settings.search.searxng_url)))
    except AttributeError:  # settings missing pieces
        provider = "duckduckgo"

    attempts.append(("duckduckgo", lambda: _duckduckgo_html(query, max_results, timeout)))
    attempts.append(("duckduckgo-instant", lambda: _duckduckgo_instant(query, max_results, timeout)))
    attempts.append(("wikipedia", lambda: _wikipedia(query, max_results, timeout)))

    errors: List[str] = []
    for name, call in attempts:
        try:
            results = call()
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{name}: {exc}")
            log.warning("search provider %s failed: %s", name, exc)
            continue
        if results:
            for item in results:
                item.setdefault("provider", name)
            log.info("search via %s returned %d results", name, len(results))
            return results
        errors.append(f"{name}: no results")
    if errors:
        log.info("search ended with: %s", "; ".join(errors))
    return []


def _format_results(query: str, results: List[Dict[str, str]]) -> str:
    sources = {str(item.get("provider", "web")) for item in results}
    lines = [f"Search results for '{query}' (via {', '.join(sorted(sources))}):"]
    for index, item in enumerate(results, 1):
        lines.append(f"{index}. {item['title']}")
        lines.append(f"   {item['url']}")
        if item.get("snippet"):
            lines.append(f"   {item['snippet']}")
    lines.append("")
    lines.append("(say 'open result 2' or name a link and I'll open it)")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# tools
# --------------------------------------------------------------------------- #
@tool(
    name="web_search",
    description="Search the web and return a list of result titles, URLs and snippets.",
    parameters={
        "type": "object",
        "properties": {
            "query": {"type": "string"},
            "max_results": {"type": "integer", "description": "default from config (usually 5)"},
        },
        "required": ["query"],
    },
    category="web",
    aliases=("search_web", "google_search"),
)
def web_search(query: str, max_results: int = 0, ctx: Optional[ToolContext] = None) -> ToolResult:
    text = (query or "").strip()
    if not text:
        return ToolResult.failure("what should I search for?")
    settings = getattr(ctx, "settings", None)
    limit = int(max_results or (settings.search.max_results if settings else 5))
    limit = max(1, min(limit, 10))
    results = run_search(text, settings, limit)
    if not results:
        return ToolResult.failure(
            "the search didn't return anything - the connection may be down or the search provider refused the request"
        )
    return ToolResult.success(_format_results(text, results), data={"query": text, "results": results})


@tool(
    name="fetch_page",
    description="Download a web page and return its readable text.",
    parameters={
        "type": "object",
        "properties": {
            "url": {"type": "string"},
            "max_chars": {"type": "integer", "description": "default 4000"},
        },
        "required": ["url"],
    },
    category="web",
    aliases=("read_webpage", "open_page"),
)
def fetch_page(url: str, max_chars: int = 4000, ctx: Optional[ToolContext] = None) -> ToolResult:
    target = (url or "").strip()
    if not target:
        return ToolResult.failure("which page should I read?")
    if not target.startswith(("http://", "https://")):
        target = "https://" + target.lstrip("/")
    try:
        markup = request_text(
            target,
            headers={"Accept": "text/html,application/xhtml+xml", "Accept-Language": "en-US,en;q=0.9"},
            timeout=20.0,
            retries=1,
            max_bytes=3 * 1024 * 1024,
        )
    except HttpError as exc:
        return ToolResult.failure(f"I couldn't load {target}: {exc}")
    text = html_to_text(markup, limit=max(500, int(max_chars or 4000)))
    if not text:
        return ToolResult.failure(f"{target} had no readable text")
    return ToolResult.success(text, data={"url": target, "chars": len(text)})


@tool(
    name="summarize_page",
    description="Read a web page and summarise it in a few sentences.",
    parameters={
        "type": "object",
        "properties": {
            "url": {"type": "string"},
            "question": {"type": "string", "description": "optional focus for the summary"},
        },
        "required": ["url"],
    },
    category="web",
)
def summarize_page(url: str, question: str = "", ctx: Optional[ToolContext] = None) -> ToolResult:
    fetched = fetch_page(url, max_chars=6000, ctx=ctx)
    if not fetched.ok:
        return fetched
    manager = getattr(ctx, "ai", None) if ctx is not None else None
    if manager is None:
        return ToolResult.success(
            f"(no AI provider available, showing raw text)\n\n{fetched.output[:2000]}", data=fetched.data
        )
    focus = f" Focus on: {question}." if question else ""
    try:
        summary = manager.complete(
            f"Summarise this web page in 4-6 sentences.{focus}\n\nURL: {url}\n\n{fetched.output[:6000]}",
            system="You are JARVIS, a concise assistant. Summarise accurately and mention the source URL.",
            max_tokens=400,
        )
    except Exception as exc:  # noqa: BLE001 - provider failure must not lose the fetch
        return ToolResult.success(
            f"(could not summarise: {exc})\n\n{fetched.output[:2000]}", data=fetched.data
        )
    return ToolResult.success(summary, data={"url": url})


@tool(
    name="web_research",
    description="Search the web for a question, read the best results and answer with sources.",
    parameters={
        "type": "object",
        "properties": {
            "query": {"type": "string"},
            "depth": {"type": "integer", "description": "how many pages to read (1-4, default 2)"},
        },
        "required": ["query"],
    },
    category="web",
    aliases=("research", "search_and_answer", "lookup"),
)
def web_research(query: str, depth: int = 2, ctx: Optional[ToolContext] = None) -> ToolResult:
    text = (query or "").strip()
    if not text:
        return ToolResult.failure("what should I look up?")
    settings = getattr(ctx, "settings", None)
    results = run_search(text, settings, max_results=max(3, int(depth or 2) + 2))
    if not results:
        return ToolResult.failure(
            "I couldn't reach a search provider, so I have no current information. Check the connection."
        )

    manager = getattr(ctx, "ai", None) if ctx is not None else None
    if manager is None or not getattr(manager, "has_online_provider", lambda: False)():
        return ToolResult.success(_format_results(text, results), data={"query": text, "results": results})

    pages: List[str] = []
    for item in results[: max(1, min(int(depth or 2), 4))]:
        try:
            fetched = fetch_page(item["url"], max_chars=3500, ctx=ctx)
        except Exception:  # noqa: BLE001
            continue
        if fetched.ok:
            pages.append(f"SOURCE: {item['url']}\n{fetched.output}")

    sources = "\n".join(f"- {item['title']} ({item['url']})" for item in results)
    if not pages:
        return ToolResult.success(_format_results(text, results), data={"query": text, "results": results})

    prompt = (
        f"Question: {text}\n\n"
        f"Web results:\n{sources}\n\n"
        f"Page extracts:\n{chr(10).join(pages)[:9000]}\n\n"
        "Answer the question using only the information above. Be concise, and finish with the "
        "source URLs you actually used. If the extracts do not answer it, say so plainly."
    )
    try:
        answer = manager.complete(
            prompt,
            system="You are JARVIS. Never invent facts or sources; only use the provided extracts.",
            max_tokens=600,
        )
    except Exception as exc:  # noqa: BLE001
        return ToolResult.success(
            f"(could not summarise the results: {exc})\n\n{_format_results(text, results)}",
            data={"query": text, "results": results},
        )
    return ToolResult.success(answer, data={"query": text, "results": results, "read": len(pages)})


@tool(
    name="youtube_search",
    description="Find YouTube videos for a query, optionally opening the browser.",
    parameters={
        "type": "object",
        "properties": {
            "query": {"type": "string"},
            "open_in_browser": {"type": "boolean", "description": "open the results page (default true)"},
        },
        "required": ["query"],
    },
    category="web",
)
def youtube_search(
    query: str, open_in_browser: bool = True, ctx: Optional[ToolContext] = None
) -> ToolResult:
    text = (query or "").strip()
    if not text:
        text = "trending"
    url = with_query("https://www.youtube.com/results", {"search_query": text})
    if open_in_browser:
        from tools.system import open_url

        opened = open_url(url)
        if opened.ok:
            return ToolResult.success(f"Opened YouTube results for '{text}'.", data={"url": url})
        return ToolResult.success(
            f"I couldn't open a browser, but here's the link: {url}", data={"url": url}
        )
    return ToolResult.success(f"YouTube search for '{text}': {url}", data={"url": url})


@tool(
    name="weather",
    description="Current weather and a short forecast for a place (no API key needed).",
    parameters={
        "type": "object",
        "properties": {
            "location": {"type": "string", "description": "city or place; empty means here"}
        },
    },
    category="web",
)
def weather(location: str = "", ctx: Optional[ToolContext] = None) -> ToolResult:
    place = (location or "").strip()
    target = urllib.parse.quote(place) if place else ""
    url = f"https://wttr.in/{target}?format=j1"
    try:
        data = request_json(url, headers={"User-Agent": "curl/8.0"}, timeout=15.0, retries=1)
    except Exception as exc:  # noqa: BLE001
        return ToolResult.failure(
            f"I couldn't reach the weather service ({exc}). Check the connection."
        )
    if not isinstance(data, dict):
        return ToolResult.failure("the weather service returned something unexpected")

    try:
        now = (data.get("current_condition") or [{}])[0]
        where = (data.get("nearest_area") or [{}])[0]
        place_name = ""
        if isinstance(where, dict):
            area = where.get("areaName") or where.get("region")
            if isinstance(area, list) and area:
                place_name = str(area[0].get("value", ""))
            country = where.get("country")
            if isinstance(country, list) and country:
                place_name = f"{place_name}, {country[0].get('value', '')}".strip(", ")
        if not place_name:
            place_name = place or "your area"

        description = (now.get("weatherDesc") or [{}])[0].get("value", "unknown")
        temperature = now.get("temp_C", "?")
        feels = now.get("FeelsLikeC", "?")
        humidity = now.get("humidity", "?")
        wind = now.get("windspeedKmph", "?")
        lines = [
            f"Weather in {place_name}: {description}, {temperature} degrees C (feels like {feels}).",
            f"Humidity {humidity}%, wind {wind} km/h.",
        ]
        forecast_days = data.get("weather") or []
        for day in forecast_days[1:3]:
            lines.append(
                f"  {day.get('date', '')}: {day.get('mintempC', '?')} to {day.get('maxtempC', '?')} degrees C"
            )
    except Exception as exc:  # noqa: BLE001
        return ToolResult.failure(f"I couldn't read the weather report: {exc}")
    return ToolResult.success("\n".join(lines), data={"location": place_name, "now": now})


__all__ = ["html_to_text", "run_search", "weather"]
