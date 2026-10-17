"""Web search & data fetcher: DuckDuckGo / SearXNG / Tavily, httpx+bs4 scraping,
OpenWeatherMap forecasts and RSS digests. Demo mode returns curated fake results.
"""
from __future__ import annotations

import html as _html
import json
import random
import re
import urllib.parse
from typing import Any

import config
from tools.registry import tool
from tools import sim

_UA = {"User-Agent": "Mozilla/5.0 (SmartAgent) like Safari"}


@tool
async def web_search(query: str, max_results: int = 5) -> dict:
    """Search the web. Tries Tavily → SearXNG → DuckDuckGo HTML endpoint.
    :param query: plain-text search query
    """
    if sim.demo():
        return {"ok": True, "demo": True, "results": _demo_search(query)[:max_results]}
    if config.TAVILY_API_KEY:
        d = await sim.api_post("https://api.tavily.com/search",
                               json_body={"api_key": config.TAVILY_API_KEY,
                                          "query": query, "max_results": max_results})
        return {"ok": True, "engine": "tavily",
                "results": [{"title": r["title"], "url": r["url"],
                             "snippet": r.get("content", "")[:300]} for r in d.get("results", [])]}
    if config.SEARXNG_URL:
        d = await sim.api_get(f"{config.SEARXNG_URL}/search",
                              params={"q": query, "format": "json"})
        return {"ok": True, "engine": "searxng",
                "results": [{"title": r["title"], "url": r["url"],
                             "snippet": r.get("content", "")[:300]}
                            for r in d.get("results", [])[:max_results]]}
    # DuckDuckGo lite HTML scrape
    from bs4 import BeautifulSoup
    raw = await _fetch_html("https://html.duckduckgo.com/html/?q=" + urllib.parse.quote(query))
    soup = BeautifulSoup(raw, "html.parser")
    out = []
    for res in soup.select(".result")[:max_results]:
        a = res.select_one(".result__a")
        sn = res.select_one(".result__snippet")
        if a:
            out.append({"title": a.get_text(strip=True),
                        "url": a.get("href", ""),
                        "snippet": sn.get_text(strip=True) if sn else ""})
    return {"ok": True, "engine": "duckduckgo", "results": out}


def _demo_search(query: str) -> list[dict[str, str]]:
    q = query.lower()
    canned = [
        {"title": f"Wikipedia: {query.title()}",
         "url": "https://en.wikipedia.org/wiki/" + query.replace(" ", "_"),
         "snippet": f"{query.title()} ir tēma, par kuru eksistē daudz avotu…"},
        {"title": f"Reddit discussion about {query}",
         "url": "https://reddit.com/r/askscience/top",
         "snippet": "Top comment: actually it depends on context…"},
        {"title": "Latvian News — diena.lv",
         "url": "https://www.diena.lv/", "snippet": "Jaunākās ziņas no Latvijas un pasaules."},
    ]
    if "laika" in q or "weather" in q or "sinoptisk" in q:
        w = sim.world()["weather"]
        canned.insert(0, {"title": "Prognoze Rīga",
                          "url": "https://met.eo.lv", "snippet":
                              f"{w['temp_c']}°C, {w['cond_lv']}, vējš {w['wind_ms']} m/s"})
    return canned


async def _fetch_html(url: str) -> str:
    import httpx
    async with httpx.AsyncClient(timeout=15, follow_redirects=True, headers=_UA) as c:
        r = await c.get(url)
        r.raise_for_status()
        return r.text


@tool
async def fetch_url(url: str, extract: str = "text", selector: str = "") -> dict:
    """Fetch a web page and extract readable content with BeautifulSoup.
    :param extract: text | links | title | meta | raw
    :param selector: optional CSS selector to narrow extraction
    """
    if sim.demo():
        return {"ok": True, "demo": True, "url": url,
                "title": "Simulated Page", "extract": extract,
                "text": f"[simulated content for {url}] — this is a plausible summary of "
                        f"the page you asked about, ~2 sentences long."}
    from bs4 import BeautifulSoup
    raw = await _fetch_html(url)
    soup = BeautifulSoup(raw, "html.parser")
    node = soup.select_one(selector) if selector else soup
    if node is None:
        return {"ok": False, "error": f"selector '{selector}' matched nothing"}
    if extract == "title":
        return {"ok": True, "title": soup.title.get_text(strip=True) if soup.title else ""}
    if extract == "links":
        return {"ok": True, "links": [(a.get_text(strip=True)[:80], a.get("href"))
                                      for a in node.find_all("a", href=True)][:60]}
    if extract == "meta":
        desc = soup.find("meta", attrs={"name": "description"}) or \
               soup.find("meta", attrs={"property": "og:description"})
        return {"ok": True, "description": desc["content"] if desc else None}
    if extract == "raw":
        return {"ok": True, "html": str(node)[:8000]}
    for tag in node(["script", "style", "nav", "footer", "header"]):
        tag.decompose()
    text = re.sub(r"\n{3,}", "\n\n", node.get_text("\n", strip=True))
    return {"ok": True, "title": soup.title.get_text(strip=True) if soup.title else "",
            "text": text[:6000]}


@tool
async def weather_forecast(location: str = "", days: int = 3) -> dict:
    """Current conditions + short forecast via OpenWeatherMap (or realistic demo simulation)."""
    loc = location or "Riga"
    if sim.demo():
        w = sim.world()["weather"]
        w["location"] = loc
        return {"ok": True, "demo": True, "current": {
            "location": loc, "temp_c": w["temp_c"], "feels_c": w["feels"],
            "condition": w["cond"], "condition_lv": w["cond_lv"],
            "humidity_pct": w["humidity"], "wind_ms": w["wind_ms"]},
            "forecast": w["forecast"][:days]}
    url = ("http://api.openweathermap.org/data/2.5/weather?q=" +
           urllib.parse.quote(loc) + "&units=metric&appid=" + config.OPENWEATHER_API_KEY)
    cur = await sim.api_get(url)
    fc = await sim.api_get(
        "http://api.openweathermap.org/data/2.5/forecast?cnt=8&q=" +
        urllib.parse.quote(loc) + "&units=metric&appid=" + config.OPENWEATHER_API_KEY)
    days_out = {}
    for row in fc.get("list", []):
        day = row["dt_txt"].split()[0]
        days_out.setdefault(day, []).append(row["main"]["temp"])
    return {"ok": True, "current": {
        "location": cur["name"], "temp_c": cur["main"]["temp"],
        "feels_c": cur["main"]["feels_like"], "condition": cur["weather"][0]["main"],
        "humidity_pct": cur["main"]["humidity"], "wind_ms": cur["wind"]["speed"]},
        "forecast": [{"day": d, "min": min(v), "max": max(v)} for d, v in days_out.items()][:days]}


@tool
async def rss_digest(feeds_json: str = "[]", max_items: int = 6) -> dict:
    """Read RSS/Atom feeds and return fresh headlines for a spoken digest.
    :param feeds_json: JSON array of feed URLs; empty uses the configured defaults
    """
    try:
        feeds = json.loads(feeds_json) if feeds_json else []
    except json.JSONDecodeError:
        feeds = []
    feeds = feeds or ["https://feeds.bbci.co.uk/news/world/rss.xml",
                      "https://www.diena.lv/rss"]
    if sim.demo():
        items = [
            {"feed": "diena.lv (sim)", "title": "Latvija palielina zaļās enerģijas jaudu",
             "link": "https://diena.lv/1", "age_h": 2},
            {"feed": "BBC (sim)", "title": "EU adopts new AI transparency rules",
             "link": "https://bbc.co.uk/2", "age_h": 4},
            {"feed": "XBRL (sim)", "title": "Tech news roundup",
             "link": "#", "age_h": 6},
        ]
        return {"ok": True, "demo": True, "items": items[:max_items]}
    from bs4 import BeautifulSoup
    out = []
    for f in feeds:
        try:
            raw = await _fetch_html(f)
            soup = BeautifulSoup(raw, "xml" if "xml" in raw[:200] or "<rss" in raw[:400]
                                 else "html.parser")
            for it in soup.find_all(["item", "entry"])[:max_items]:
                t = it.find(["title"])
                l = it.find(["link"])
                out.append({"feed": f, "title": t.get_text(strip=True) if t else "",
                            "link": (l.get_text(strip=True) or l.get("href", "")) if l else ""})
        except Exception as e:  # noqa: BLE001 keep digest resilient
            out.append({"feed": f, "title": f"(feed error: {e})", "link": ""})
    return {"ok": True, "items": out[:max_items * len(feeds)]}


def _esc(s: str) -> str:
    return _html.escape(s or "")
