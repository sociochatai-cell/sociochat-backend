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
