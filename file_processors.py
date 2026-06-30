"""
File Processors Module for Multimodal RAG.
Handles extraction of text from various file formats.

Copied from reference multilingual_rag implementation.
"""
import asyncio
import logging
import os
import re
from typing import Dict, List, Optional
from urllib.parse import urljoin, urlparse

logger = logging.getLogger(__name__)


def _playwright_headless() -> bool:
    """Set CRAWL_HEADED=1 locally to open a visible Chromium window for debugging."""
    return os.getenv("CRAWL_HEADED", "").strip().lower() not in ("1", "true", "yes", "on")


class PDFProcessor:
    """Extract text from PDF files using pdfplumber."""
    
    @staticmethod
    def extract_text(file_path: str) -> Dict:
        """
        Extract text from a PDF file.
        
        Args:
            file_path: Path to the PDF file
            
        Returns:
            Dict with extracted text, page count, and metadata
        """
        try:
            import pdfplumber
        except ImportError:
            return {"status": "error", "message": "pdfplumber not installed. Run: pip install pdfplumber"}
        
        try:
            all_text = []
            page_texts = []
            
            with pdfplumber.open(file_path) as pdf:
                for i, page in enumerate(pdf.pages):
                    text = page.extract_text() or ""
                    if text.strip():
                        page_texts.append({
                            "page": i + 1,
                            "text": text
                        })
                        all_text.append(f"[Page {i+1}]\n{text}")
            
            combined_text = "\n\n".join(all_text)
            
            return {
                "status": "success",
                "text": combined_text,
                "page_count": len(page_texts),
                "pages": page_texts,
                "file_name": os.path.basename(file_path)
            }
            
        except Exception as e:
            return {"status": "error", "message": str(e)}



class TextFileProcessor:
    """Read content from plain text files (txt, md, json, etc)."""
    
    @staticmethod
    def read_text(file_path: str) -> Dict:
        """Read text from a text-based file."""
        try:
            with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
                content = f.read()
            
            return {
                "status": "success",
                "text": content,
                "page_count": 1, 
                "file_name": os.path.basename(file_path)
            }
        except Exception as e:
            return {"status": "error", "message": str(e)}


class DocxProcessor:
    """Extract text from Word documents (.docx)."""
    @staticmethod
    def extract_text(file_path: str) -> Dict:
        try:
            import docx
        except ImportError:
            return {"status": "error", "message": "python-docx not installed. Run: pip install python-docx"}
        
        try:
            doc = docx.Document(file_path)
            full_text = []
            for para in doc.paragraphs:
                if para.text.strip():
                    full_text.append(para.text)
            
            return {
                "status": "success",
                "text": "\n".join(full_text),
                "page_count": 1, # DOCX doesn't really have pages in the same way
                "file_name": os.path.basename(file_path)
            }
        except Exception as e:
            return {"status": "error", "message": str(e)}


class PptxProcessor:
    """Extract text from PowerPoint presentations (.pptx)."""
    @staticmethod
    def extract_text(file_path: str) -> Dict:
        try:
            from pptx import Presentation
        except ImportError:
            return {"status": "error", "message": "python-pptx not installed. Run: pip install python-pptx"}
        
        try:
            prs = Presentation(file_path)
            text_runs = []
            slide_count = 0
            
            for slide in prs.slides:
                slide_count += 1
                slide_text = []
                for shape in slide.shapes:
                    if hasattr(shape, "text") and shape.text:
                        slide_text.append(shape.text)
                if slide_text:
                    text_runs.append(f"[Slide {slide_count}]\n" + "\n".join(slide_text))
            
            return {
                "status": "success",
                "text": "\n\n".join(text_runs),
                "page_count": slide_count,
                "file_name": os.path.basename(file_path)
            }
        except Exception as e:
            return {"status": "error", "message": str(e)}


class SpreadsheetProcessor:
    """Extract text from CSV and Excel files."""
    @staticmethod
    def extract_text(file_path: str) -> Dict:
        try:
            import pandas as pd
        except ImportError:
            return {"status": "error", "message": "pandas not installed. Run: pip install pandas openpyxl"}
            
        try:
            filename = os.path.basename(file_path).lower()
            dfs = []
            
            if filename.endswith('.csv'):
                dfs.append(pd.read_csv(file_path))
            elif filename.endswith('.xlsx') or filename.endswith('.xls'):
                xls = pd.ExcelFile(file_path)
                for sheet_name in xls.sheet_names:
                    df = pd.read_excel(xls, sheet_name=sheet_name)
                    df['sheet_name'] = sheet_name # Add metadata
                    dfs.append(df)
            
            text_parts = []
            for df in dfs:
                # Convert dataframe to string/markdown
                text_parts.append(df.to_string())
            
            return {
                "status": "success",
                "text": "\n\n".join(text_parts),
                "page_count": 1,
                "file_name": os.path.basename(file_path)
            }
        except Exception as e:
             return {"status": "error", "message": str(e)}


class WebScraper:
    """
    Web scraper using Playwright for JavaScript-rendered pages.
    Supports automatic link discovery and crawling.
    """
    
    def __init__(self, max_pages: int = 10, same_domain_only: bool = True):
        """
        Initialize scraper settings.
        
        Args:
            max_pages: Maximum number of pages to crawl from a single URL
            same_domain_only: Only follow links on the same domain
        """
        self.max_pages = max_pages
        self.same_domain_only = same_domain_only
        self.visited_urls = set()
    
    def _get_domain(self, url: str) -> str:
        """Extract domain from URL (ignoring www)."""
        parsed = urlparse(url)
        domain = parsed.netloc
        if domain.startswith('www.'):
            return domain[4:]
        return domain
    
    def _clean_text(self, text: str) -> str:
        """Clean extracted text by removing excess whitespace."""
        # Remove multiple newlines
        text = re.sub(r'\n{3,}', '\n\n', text)
        # Remove multiple spaces
        text = re.sub(r' {2,}', ' ', text)
        return text.strip()
    
    async def scrape_page(self, url: str) -> Dict:
        """
        Scrape a single page with JavaScript rendering.
        
        Args:
            url: URL to scrape
            
        Returns:
            Dict with page content, title, and discovered links
        """
        try:
            from playwright.async_api import async_playwright
        except ImportError:
            return {"status": "error", "message": "playwright not installed. Run: pip install playwright && playwright install chromium"}
        
        try:
            async with async_playwright() as p:
                browser = await p.chromium.launch(headless=_playwright_headless())
                page = await browser.new_page()
                
                # domcontentloaded + short idle wait — networkidle hangs on analytics-heavy sites
                await page.goto(url, wait_until="domcontentloaded", timeout=60000)
                try:
                    await page.wait_for_load_state("networkidle", timeout=12000)
                except Exception:
                    pass
                await page.wait_for_timeout(2500)
                
                # Extract title
                title = await page.title()
                
                # Extract all links on the page (BEFORE cleaning DOM)
                links = await page.evaluate('''() => {
                    const anchors = document.querySelectorAll('a[href]');
                    return Array.from(anchors).map(a => a.href).filter(href => 
                        href.startsWith('http') && !href.includes('#')
                    );
                }''')

                # Extract main text content (After cleaning DOM)
                text_content = await page.evaluate('''() => {
                    // Remove script, style, nav, header, footer elements
                    const elementsToRemove = document.querySelectorAll('script, style, nav, header, footer, aside, .nav, .header, .footer, .sidebar, .menu, .advertisement, .ad');
                    elementsToRemove.forEach(el => el.remove());
                    
                    // Get text from body
                    return document.body.innerText || document.body.textContent;
                }''')
                
                await browser.close()
                
                logger.info("Found %s links on %s", len(links), url)
                
                return {
                    "status": "success",
                    "url": url,
                    "title": title,
                    "text": self._clean_text(text_content),
                    "links": list(set(links))  # Remove duplicates
                }
                
        except Exception as e:
            return {"status": "error", "url": url, "message": str(e)}
    
    async def _scrape_page_concurrent(self, context, url: str) -> Dict:
        """Internal concurrent scrape method."""
        try:
            page = await context.new_page()
            try:
                response = await page.goto(url, wait_until="domcontentloaded", timeout=60000)
                try:
                    await page.wait_for_load_state("networkidle", timeout=12000)
                except Exception:
                    pass
                await page.wait_for_timeout(2500)
                if response and response.status >= 400:
                    await page.close()
                    logger.warning("Skipping %s (HTTP %s)", url, response.status)
                    return {"status": "error", "url": url, "message": f"HTTP {response.status}"}
                
                title = await page.title()
                
                links = await page.evaluate('''() => {
                    const anchors = document.querySelectorAll('a[href]');
                    return Array.from(anchors).map(a => a.href).filter(href => 
                        href.startsWith('http') && !href.includes('#')
                    );
                }''')

                text_content = await page.evaluate('''() => {
                    const elementsToRemove = document.querySelectorAll('script, style, nav, header, footer, aside, .nav, .header, .footer, .sidebar, .menu, .advertisement, .ad');
                    elementsToRemove.forEach(el => el.remove());
                    return document.body.innerText || document.body.textContent;
                }''')
                return {
                    "status": "success", "url": url, "title": title,
                    "text": self._clean_text(text_content), "links": list(set(links))
                }
            finally:
                await page.close()
        except Exception as e:
            return {"status": "error", "url": url, "message": str(e)}

    async def crawl_site(self, start_url: str) -> Dict:
        """Crawl a website concurrently."""
        try:
            from playwright.async_api import async_playwright
        except ImportError:
            return {"status": "error", "message": "playwright not installed"}

        self.visited_urls = set()
        pages_data = []
        urls_to_visit = [start_url]
        base_domain = self._get_domain(start_url)
        
        logger.info("Starting concurrent crawl of %s", base_domain)
        
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=_playwright_headless())
            context = await browser.new_context()
            
            while urls_to_visit and len(self.visited_urls) < self.max_pages:
                # Prepare batch
                batch = []
                while len(batch) < 5 and urls_to_visit and (len(self.visited_urls) + len(batch)) < self.max_pages:
                    url = urls_to_visit.pop(0)
                    if url in self.visited_urls: continue
                    if self.same_domain_only and self._get_domain(url) != base_domain: continue
                    batch.append(url)
                    self.visited_urls.add(url)
                
                if not batch: break
                
                logger.info("Scraping batch of %s pages", len(batch))
                tasks = [self._scrape_page_concurrent(context, url) for url in batch]
                results = await asyncio.gather(*tasks)
                
                for res in results:
                    if res.get("status") == "success":
                        pages_data.append({"url": res["url"], "title": res["title"], "text": res["text"]})
                        for link in res.get("links", []):
                            if link not in self.visited_urls: urls_to_visit.append(link)
            await browser.close()
        
        # Combine all text
        combined_text = ""
        for page in pages_data:
            combined_text += f"\n\n=== {page['title']} ===\nURL: {page['url']}\n\n{page['text']}"
        
        return {
            "status": "success",
            "pages_scraped": len(pages_data),
            "pages": pages_data,
            "combined_text": combined_text.strip()
        }


# Synchronous wrapper for the async scraper
def scrape_url_sync(url: str, max_pages: int = 10, same_domain_only: bool = True) -> Dict:
    """
    Synchronous wrapper to run web scraping in a separate thread.
    This avoids event loop conflicts and ensures ProactorEventLoop is used on Windows.
    """
    import asyncio
    import sys
    from concurrent.futures import ThreadPoolExecutor

    def run_crawler_in_thread():
        # Set policy for this new thread
        if sys.platform == 'win32':
            asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
        
        # Create and run new loop
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        
        scraper = WebScraper(max_pages=max_pages, same_domain_only=same_domain_only)
        try:
            return loop.run_until_complete(scraper.crawl_site(url))
        finally:
            loop.close()

    # Run in thread pool to avoid blocking the main thread (although this function itself is sync blocking)
    # The caller (FastAPI async def) should wrap this in run_in_executor if not already
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(run_crawler_in_thread)
        return future.result()


def scrape_single_page_sync(url: str) -> Dict:
    """
    Synchronous wrapper for single page scraping.
    Returns text content from a single URL using Playwright.
    """
    import asyncio
    import sys
    from concurrent.futures import ThreadPoolExecutor

    def run_scraper_in_thread():
        if sys.platform == 'win32':
            asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
        
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        
        scraper = WebScraper(max_pages=1, same_domain_only=True)
        try:
            return loop.run_until_complete(scraper.scrape_page(url))
        finally:
            loop.close()

    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(run_scraper_in_thread)
        return future.result()
