"""
RAG Module - Backward Compatibility Wrapper
============================================
Provides backward-compatible functions that wrap RAGEngine (Qdrant + Gemini embeddings).
"""
from typing import Dict, List, Any, Optional, Tuple
import logging

from rag_engine import get_rag_engine
from rag_config import app_config

logger = logging.getLogger(__name__)

COLLECTION_NAME = app_config.collection_name


def ingest_text_cloud(
    text: str,
    workspace_id: int,
    source: str = "manual",
    metadata: Optional[Dict[str, Any]] = None,
    collection_name: str = None,
) -> Dict[str, Any]:
    """Ingest text into Qdrant Cloud with workspace isolation."""
    engine = get_rag_engine()
    full_metadata = metadata or {}
    full_metadata["source"] = source
    return engine.ingest_text(text, workspace_id, full_metadata, collection_name)


def retrieve_with_context_window(
    query: str,
    workspace_id: int,
    top_k: int = 5,
    score_threshold: float = 0.25,
    collection_name: str = None,
) -> Tuple[List[Dict], Dict]:
    """Retrieve relevant context with context window expansion."""
    engine = get_rag_engine()
    return engine.retrieve(
        query,
        workspace_id,
        top_k,
        collection_name,
        score_threshold=score_threshold,
    )


def delete_workspace_data(workspace_id: int, collection_name: str = None) -> Dict[str, Any]:
    """Delete all data for a specific workspace."""
    engine = get_rag_engine()
    return engine.delete_workspace_data(workspace_id, collection_name)


def get_workspace_stats(workspace_id: int) -> Dict[str, Any]:
    """Get statistics for a specific workspace."""
    engine = get_rag_engine()
    return engine.get_workspace_stats(workspace_id)


def extract_text_from_url(url: str, use_playwright: bool = False) -> Dict[str, Any]:
    """Extract text content from a URL."""
    try:
        if use_playwright:
            return _extract_with_playwright(url)
        return _extract_with_requests(url)
    except Exception as e:
        logger.exception("URL extraction failed: %s", e)
        return {"success": False, "text": "", "title": "", "error": str(e)}


def _extract_with_requests(url: str) -> Dict[str, Any]:
    """Extract text using requests + HTMLParser (no extra deps)."""
    import requests
    from html.parser import HTMLParser

    class TextExtractor(HTMLParser):
        def __init__(self):
            super().__init__()
            self.text_parts = []
            self._skip = False

        def handle_starttag(self, tag, attrs):
            if tag in ("script", "style", "noscript", "nav", "footer", "header", "aside"):
                self._skip = True

        def handle_endtag(self, tag):
            if tag in ("script", "style", "noscript", "nav", "footer", "header", "aside"):
                self._skip = False

        def handle_data(self, data):
            if not self._skip:
                stripped = data.strip()
                if stripped:
                    self.text_parts.append(stripped)

    headers = {"User-Agent": "SocioChat/1.0 (Knowledge Base Indexer)"}
    response = requests.get(url, headers=headers, timeout=30)
    response.raise_for_status()

    parser = TextExtractor()
    parser.feed(response.text)
    text = "\n".join(parser.text_parts)
    title = url

    if len(text) < 50:
        return {"success": False, "text": "", "title": title, "error": "Not enough content extracted"}

    return {"success": True, "text": text, "title": title, "error": None}


def _extract_with_playwright(url: str) -> Dict[str, Any]:
    """Extract text using Playwright for JS-heavy sites."""
    try:
        from playwright.sync_api import sync_playwright
        from html.parser import HTMLParser

        class TextExtractor(HTMLParser):
            def __init__(self):
                super().__init__()
                self.text_parts = []
                self._skip = False

            def handle_starttag(self, tag, attrs):
                if tag in ("script", "style", "noscript", "nav", "footer", "header", "aside"):
                    self._skip = True

            def handle_endtag(self, tag):
                if tag in ("script", "style", "noscript", "nav", "footer", "header", "aside"):
                    self._skip = False

            def handle_data(self, data):
                if not self._skip:
                    stripped = data.strip()
                    if stripped:
                        self.text_parts.append(stripped)

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page()
            page.goto(url, timeout=60000, wait_until="domcontentloaded")
            try:
                page.wait_for_load_state("networkidle", timeout=15000)
            except Exception:
                pass
            html = page.content()
            title = page.title() or url
            browser.close()

        parser = TextExtractor()
        parser.feed(html)
        text = "\n".join(parser.text_parts)

        if len(text) < 50:
            return {"success": False, "text": "", "title": title, "error": "Not enough content"}

        return {"success": True, "text": text, "title": title, "error": None}
    except Exception as e:
        logger.warning("Playwright extraction failed, falling back to requests: %s", e)
        return _extract_with_requests(url)

# ---------------------------------------------------------------------------
# Restored (crawl_site_with_requests): same-domain BFS over HTTP, with a
# Playwright fallback for JS/SPA sites that return little/no content via plain
# requests. Returns {success, text, title, pages_scraped, pages, crawl_mode}.
# ---------------------------------------------------------------------------
def crawl_site_with_requests(url: str, max_pages: int = 10, same_domain_only: bool = True) -> Dict[str, Any]:
    from urllib.parse import urljoin, urlparse
    import requests as _requests
    from html.parser import HTMLParser

    start = url
    base_netloc = urlparse(start).netloc
    seen = set()
    queue = [start]
    pages = []
    combined = []

    class _LinkParser(HTMLParser):
        def __init__(self):
            super().__init__()
            self.links = []
        def handle_starttag(self, tag, attrs):
            if tag == "a":
                for k, v in attrs:
                    if k == "href" and v:
                        self.links.append(v)

    headers = {"User-Agent": "SocioChat/1.0 (Knowledge Base Indexer)"}

    while queue and len(pages) < max_pages:
        u = queue.pop(0)
        if u in seen:
            continue
        seen.add(u)
        try:
            res = _extract_with_requests(u)
        except Exception:
            res = {"success": False, "text": ""}
        text = (res.get("text") or "").strip()
        if res.get("success") and len(text) >= 50:
            pages.append({"url": u, "title": res.get("title", u), "text": text})
            combined.append(text)
            try:
                resp = _requests.get(u, headers=headers, timeout=20)
                lp = _LinkParser()
                lp.feed(resp.text)
                for href in lp.links:
                    full = urljoin(u, href.split("#")[0])
                    if (not same_domain_only) or urlparse(full).netloc == base_netloc:
                        if full not in seen and full not in queue and len(queue) + len(pages) < max_pages * 4:
                            queue.append(full)
            except Exception:
                pass

    if not pages:
        # SPA / empty over HTTP -> Playwright fallback (multi-page, then single)
        try:
            from file_processors import scrape_url_sync
            pw = scrape_url_sync(url, max_pages=max_pages)
            if pw.get("status") == "success" and pw.get("pages_scraped", 0) > 0:
                pw_pages = pw.get("pages", [])
                return {
                    "success": True,
                    "text": pw.get("combined_text", ""),
                    "title": pw_pages[0].get("title", url) if pw_pages else url,
                    "pages_scraped": pw.get("pages_scraped", 0),
                    "pages": pw_pages,
                    "crawl_mode": "playwright_fallback",
                }
        except Exception as _e:
            logger.warning("crawl_site_with_requests: playwright multi fallback failed: %s", _e)
        single = _extract_with_playwright(url)
        if single.get("success"):
            return {
                "success": True,
                "text": single.get("text", ""),
                "title": single.get("title", url),
                "pages_scraped": 1,
                "pages": [{"url": url, "title": single.get("title", url), "text": single.get("text", "")}],
                "crawl_mode": "playwright_single",
            }
        return {"success": False, "text": "", "title": "", "pages_scraped": 0, "pages": [], "error": "No content extracted"}

    return {
        "success": True,
        "text": "\n\n".join(combined),
        "title": pages[0].get("title", url),
        "pages_scraped": len(pages),
        "pages": pages,
        "crawl_mode": "http",
    }