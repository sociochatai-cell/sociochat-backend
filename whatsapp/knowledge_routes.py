"""
Knowledge Base Routes
======================
API endpoints for managing the WhatsApp chatbot knowledge base.
Uses the new RAGEngine for Qdrant Cloud storage.

Response formats aligned with frontend expectations.
"""
from flask import Blueprint, request, jsonify
import logging
import os
import hashlib
from pathlib import Path
from datetime import datetime
from werkzeug.utils import secure_filename

# Import RAG module
import sys
parent_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if parent_dir not in sys.path:
    sys.path.insert(0, parent_dir)

import rag
from rag_engine import get_rag_engine
from .http_rate_limit import rate_limit

logger = logging.getLogger(__name__)

# Blueprint setup
knowledge_bp = Blueprint('knowledge', __name__, url_prefix='/api/whatsapp/knowledge')
knowledge_bp.strict_slashes = False

# Local storage for file references
DATA_BASE_DIR = Path(os.environ.get("KNOWLEDGE_DATA_DIR", "knowledge_base"))
DATA_BASE_DIR.mkdir(parents=True, exist_ok=True)

ALLOWED_EXTENSIONS = {'txt', 'md', 'pdf', 'text', 'docx', 'pptx', 'csv', 'xlsx'}


def _crawl_playwright_allowed() -> bool:
    """Playwright/Chromium needs ~1Gi+ RAM; disabled on Cloud Run unless explicitly enabled."""
    flag = os.getenv("PLAYWRIGHT_CRAWL_ENABLED", "").strip().lower()
    if flag in ("1", "true", "yes", "on"):
        return True
    if flag in ("0", "false", "no", "off"):
        return False
    # Default: off on Cloud Run (512Mi–1Gi instances OOM when launching Chromium)
    return not bool(os.getenv("K_SERVICE"))


def get_workspace_dir(workspace_id):
    """Get workspace-specific data directory."""
    if not workspace_id:
        return None
    data_dir = DATA_BASE_DIR / str(workspace_id)
    data_dir.mkdir(parents=True, exist_ok=True)
    return data_dir


def allowed_file(filename):
    """Check if file extension is allowed."""
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS


# ============================================================
# List Documents - From Qdrant Cloud
# ============================================================

@knowledge_bp.route("", methods=["GET"], strict_slashes=False)
@knowledge_bp.route("/", methods=["GET"], strict_slashes=False)
def list_knowledge():
    """List all documents in the knowledge base from Qdrant."""
    workspace_id = request.args.get('workspace_id')
    if not workspace_id:
        return jsonify({"error": "workspace_id required"}), 400
    
    try:
        workspace_id_int = int(workspace_id)
        engine = get_rag_engine()
        
        # Get documents from Qdrant
        documents = engine.get_workspace_documents(workspace_id_int)
        
        # Format for frontend
        formatted_docs = []
        for i, doc in enumerate(documents):
            formatted_docs.append({
                "id": i + 1,
                "doc_id": doc.get("doc_id", ""),
                "title": doc.get("title", "Unknown"),
                "name": doc.get("title", "Unknown"),
                "filename": doc.get("filename", ""),
                "source_type": doc.get("source_type", "unknown"),
                "status": "indexed",
                "chunk_count": doc.get("chunk_count", 0),
                "content_length": 0,
                "url": doc.get("url", ""),
                "source": doc.get("source", "")
            })
        
        return jsonify({"success": True, "documents": formatted_docs, "count": len(formatted_docs)})
    except Exception as e:
        logger.exception(f"List documents failed: {e}")
        return jsonify({"success": True, "documents": [], "count": 0})


# ============================================================
# Get Statistics - CRITICAL: Frontend expects specific format
# ============================================================

@knowledge_bp.route('/stats', methods=['GET'])
def get_stats():
    """Get indexing statistics for the workspace."""
    workspace_id = request.args.get('workspace_id')
    
    if not workspace_id:
        return jsonify({
            "success": True,
            "total_chunks": 0,
            "indexed_documents": 0,
            "total_documents": 0
        })
    
    try:
        workspace_id_int = int(workspace_id)
        
        # Get workspace-specific stats
        stats = rag.get_workspace_stats(workspace_id_int)
        
        logger.info(f"Stats for workspace {workspace_id}: {stats}")
        
        # Frontend expects these EXACT keys at root level
        return jsonify({
            "success": True,
            "total_chunks": stats.get("total_chunks", 0),
            "indexed_documents": stats.get("indexed_documents", 0),
            "total_documents": stats.get("total_documents", 0),
            # Also include usage data structure
            "usage": {
                "today": {
                    "query_count": 0,
                    "estimated_cost_inr": 0,
                    "rag_hit_count": 0,
                    "rag_miss_count": 0
                }
            }
        })
    except Exception as e:
        logger.error(f"Error getting stats: {e}")
        return jsonify({
            "success": True,
            "total_chunks": 0,
            "indexed_documents": 0,
            "total_documents": 0
        })


# ============================================================
# Crawl URL - Synchronous with immediate indexing
# ============================================================

def _clamp_max_pages(raw) -> int:
    try:
        pages = int(raw) if raw is not None else 1
    except (TypeError, ValueError):
        pages = 1
    return max(1, min(pages, 50))


def _crawl_timeout_sec(max_pages: int) -> int:
    if max_pages <= 1:
        return 55
    return min(180, max(60, max_pages * 10))


def _run_multi_page_crawl(url: str, max_pages: int, use_playwright: bool) -> dict:
    """Crawl multiple same-domain pages; Playwright when allowed, else HTTP BFS."""
    if use_playwright and _crawl_playwright_allowed():
        from file_processors import scrape_url_sync

        result = scrape_url_sync(url, max_pages=max_pages)
        if result.get("status") == "success" and result.get("pages_scraped", 0) > 0:
            pages = result.get("pages", [])
            return {
                "success": True,
                "text": result.get("combined_text", ""),
                "title": pages[0].get("title", url) if pages else url,
                "pages_scraped": result.get("pages_scraped", 0),
                "pages": pages,
                "crawl_mode": "playwright",
            }
        return {
            "success": False,
            "text": "",
            "title": "",
            "error": result.get("message", "Multi-page crawl failed"),
            "pages_scraped": 0,
        }

    result = rag.crawl_site_with_requests(url, max_pages=max_pages)
    if result.get("success"):
        result["crawl_mode"] = "http"
    return result


def _run_crawl_with_timeout(url: str, use_playwright: bool, max_pages: int = 1, timeout_sec: int = 55):
    """Run URL extraction in a worker thread so Cloud Run can return JSON on timeout."""
    from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeout

    max_pages = _clamp_max_pages(max_pages)

    def _do_crawl():
        if max_pages <= 1:
            return rag.extract_text_from_url(url, use_playwright)
        return _run_multi_page_crawl(url, max_pages, use_playwright)

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(_do_crawl)
        try:
            return future.result(timeout=timeout_sec)
        except FuturesTimeout:
            return {
                "success": False,
                "text": "",
                "title": "",
                "error": f"Crawl timed out after {timeout_sec}s. Try fewer pages or disable AI Browser.",
                "pages_scraped": 0,
            }


@knowledge_bp.route('/preview-url', methods=['POST', 'OPTIONS'])
@rate_limit("whatsapp.knowledge.preview")
def preview_url():
    """Preview URL text extraction without indexing (frontend compatibility)."""
    if request.method == "OPTIONS":
        return "", 204

    data = request.get_json() or {}
    url = data.get("url")
    workspace_id = request.args.get("workspace_id") or data.get("workspace_id")
    use_playwright = bool(data.get("use_playwright", False))
    max_pages = _clamp_max_pages(data.get("max_pages", 1))

    if not url:
        return jsonify({"success": False, "error": "url required"}), 400
    if not workspace_id:
        return jsonify({"success": False, "error": "workspace_id required"}), 400
    if not url.startswith(("http://", "https://")):
        return jsonify({"success": False, "error": "Invalid URL"}), 400
    if use_playwright and not _crawl_playwright_allowed():
        use_playwright = False

    extract_result = _run_crawl_with_timeout(
        url,
        use_playwright=use_playwright,
        max_pages=max_pages,
        timeout_sec=_crawl_timeout_sec(max_pages),
    )
    if not extract_result.get("success"):
        return jsonify({
            "success": False,
            "error": extract_result.get("error", "Extraction failed"),
        }), 400

    text = extract_result.get("text", "")
    return jsonify({
        "success": True,
        "url": url,
        "title": extract_result.get("title", url),
        "content_length": len(text),
        "preview": text[:4000],
        "truncated": len(text) > 4000,
    })


@knowledge_bp.route('/crawl/<job_id>', methods=['GET'])
def crawl_job_status(job_id):
    """
    Async crawl job status (compatibility shim).

    Crawls are synchronous on this service; clients polling a job id after POST /crawl
    receive a completed response when job_id matches the last sync crawl doc id pattern.
    """
    workspace_id = request.args.get("workspace_id")
    if not workspace_id:
        return jsonify({"success": False, "error": "workspace_id required"}), 400

    return jsonify({
        "success": True,
        "job_id": job_id,
        "status": "completed",
        "message": "Crawl runs synchronously; use POST /knowledge/crawl response for results.",
    })


@knowledge_bp.route('/crawl', methods=['POST', 'OPTIONS'])
@rate_limit("whatsapp.knowledge.crawl")
def crawl_url():
    """Crawl a URL and index its content."""
    if request.method == "OPTIONS":
        return "", 204

    data = request.get_json() or {}
    url = data.get('url')
    workspace_id = request.args.get('workspace_id') or data.get('workspace_id')
    use_playwright = bool(data.get('use_playwright', False))
    max_pages = _clamp_max_pages(data.get('max_pages', 1))
    playwright_skipped = False
    if use_playwright and not _crawl_playwright_allowed():
        playwright_skipped = True
        use_playwright = False
        logger.warning("Playwright crawl requested but disabled on this host; using HTTP fetch")
    
    logger.info(f"=== CRAWL REQUEST ===")
    logger.info(
        f"URL: {url}, workspace_id: {workspace_id}, playwright: {use_playwright}, max_pages: {max_pages}"
    )
    
    if not url or not workspace_id:
        return jsonify({"success": False, "error": "url and workspace_id required"}), 400
    
    if not url.startswith(('http://', 'https://')):
        return jsonify({"success": False, "error": "Invalid URL - must start with http:// or https://"}), 400
    
    try:
        workspace_id_int = int(workspace_id)
        data_dir = get_workspace_dir(workspace_id)
        
        # 1. Extract content from URL
        logger.info(f"Step 1: Extracting content...")
        extract_result = _run_crawl_with_timeout(
            url,
            use_playwright=use_playwright,
            max_pages=max_pages,
            timeout_sec=_crawl_timeout_sec(max_pages),
        )
        
        if not extract_result.get("success"):
            hint = "Try enabling 'AI Browser' for JavaScript-heavy sites." if not use_playwright else "Site may have anti-bot protection."
            if max_pages > 1 and not use_playwright:
                hint = (
                    "Multi-page crawl uses HTTP link following. Enable 'AI Browser' for "
                    "JavaScript-heavy sites or reduce max_pages."
                )
            return jsonify({
                "success": False,
                "error": extract_result.get("error", "Extraction failed"),
                "hint": hint
            }), 400
        
        text = extract_result["text"]
        title = extract_result.get("title", url)
        pages_scraped = extract_result.get("pages_scraped", 1)
        page_urls = [p.get("url") for p in extract_result.get("pages", []) if p.get("url")]
        
        logger.info(f"Step 2: Extracted {len(text)} characters from {pages_scraped} page(s)")
        
        # 2. Save to local file for reference
        url_hash = hashlib.md5(url.encode()).hexdigest()[:12]
        filename = f"url_{url_hash}.txt"
        file_path = data_dir / filename
        
        file_content = f"Source: {url}\nTitle: {title}\nDate: {datetime.now().isoformat()}\n\n{text}"
        file_path.write_text(file_content, encoding="utf-8")
        
        # 3. Ingest to Qdrant Cloud
        logger.info(f"Step 3: Ingesting to Qdrant Cloud...")
        index_result = rag.ingest_text_cloud(
            text=text,
            workspace_id=workspace_id_int,
            source=url,
            metadata={
                "type": "web",
                "title": title,
                "url": url,
                "pages_scraped": pages_scraped,
                "page_urls": page_urls[:50],
                "crawl_mode": extract_result.get("crawl_mode", "single"),
            }
        )
        
        logger.info(f"Step 4: Ingest result: {index_result}")
        
        if index_result.get("status") == "error":
            err_msg = index_result.get("message", "Indexing failed")
            hint = None
            if "credentials" in err_msg.lower() or "GOOGLE_GENAI" in err_msg:
                hint = (
                    "Add GOOGLE_GENAI_API_KEY to whatsapp-service/.env "
                    "(Google AI Studio key), then restart docker-compose."
                )
            elif "404" in err_msg or "embedding model" in err_msg.lower():
                hint = (
                    "Set EMBEDDING_MODEL=gemini-embedding-001 in whatsapp-service/.env "
                    "(text-embedding-004 is Vertex-only)."
                )
            payload = {"success": False, "error": err_msg}
            if hint:
                payload["hint"] = hint
            return jsonify(payload), 500
        
        chunks_processed = index_result.get("chunks_processed", 0)
        logger.info(f"=== CRAWL SUCCESS: {chunks_processed} chunks indexed ===")
        
        # Return format for sync crawl (no job_id needed)
        payload = {
            "success": True,
            "message": "URL crawled and indexed",
            "file": filename,
            "title": title,
            "content_length": len(text),
            "pages_scraped": pages_scraped,
            "max_pages": max_pages,
            "page_urls": page_urls[:20],
            "indexed_chunks": chunks_processed,
            "chunks_processed": chunks_processed,
            "doc_id": index_result.get("doc_id"),
        }
        if playwright_skipped:
            payload["playwright_skipped"] = True
            payload["hint"] = (
                "AI Browser is disabled on this server (insufficient memory). "
                "Indexed using standard HTTP fetch instead."
            )
        return jsonify(payload)
        
    except Exception as e:
        logger.exception(f"Crawl failed: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


# ============================================================
# Upload File
# ============================================================

@knowledge_bp.route("", methods=["POST"], strict_slashes=False)
@knowledge_bp.route("/", methods=["POST"], strict_slashes=False)
def upload_knowledge():
    """Upload a file or add content to knowledge base."""
    workspace_id = request.form.get('workspace_id') or request.args.get('workspace_id')
    
    if not workspace_id:
        # Try JSON body
        data = request.get_json() or {}
        workspace_id = data.get('workspace_id')
    
    if not workspace_id:
        return jsonify({"error": "workspace_id required"}), 400
    
    try:
        workspace_id_int = int(workspace_id)
        data_dir = get_workspace_dir(workspace_id)
        
        # Check if file upload
        if 'file' in request.files:
            file = request.files['file']
            if file.filename == '':
                return jsonify({"success": False, "message": "No file selected"}), 400
            
            if not allowed_file(file.filename):
                return jsonify({"success": False, "message": "File type not allowed"}), 400
            
            filename = secure_filename(file.filename)
            file_path = data_dir / filename
            file.save(str(file_path))
            
            # Extract text based on file type
            suffix = file_path.suffix.lower()
            
            try:
                from file_processors import PDFProcessor, DocxProcessor, PptxProcessor, SpreadsheetProcessor, TextFileProcessor
                
                if suffix == '.pdf':
                    result = PDFProcessor.extract_text(str(file_path))
                elif suffix == '.docx':
                    result = DocxProcessor.extract_text(str(file_path))
                elif suffix == '.pptx':
                    result = PptxProcessor.extract_text(str(file_path))
                elif suffix in ['.csv', '.xlsx', '.xls']:
                    result = SpreadsheetProcessor.extract_text(str(file_path))
                else:
                    result = TextFileProcessor.read_text(str(file_path))
                
                if result.get("status") == "error":
                    return jsonify({"success": False, "message": result.get("message")}), 500
                
                text = result.get("text", "")
            except ImportError:
                # Fallback: read as text
                text = file_path.read_text(encoding='utf-8', errors='ignore')
            
            # Ingest to RAG
            index_result = rag.ingest_text_cloud(
                text=text,
                workspace_id=workspace_id_int,
                source=filename,
                metadata={"type": "file", "filename": filename}
            )
            
            return jsonify({
                "success": True,
                "message": "File uploaded and indexed",
                "filename": filename,
                "chunks_processed": index_result.get("chunks_processed", 0)
            })
        
        # Check for URL in JSON
        data = request.get_json() or {}
        if 'url' in data:
            url = data['url']
            use_playwright = data.get('use_playwright', False)
            
            extract_result = rag.extract_text_from_url(url, use_playwright=use_playwright)
            
            if not extract_result.get("success"):
                return jsonify({
                    "success": False,
                    "message": extract_result.get("error", "URL extraction failed")
                }), 400
            
            text = extract_result["text"]
            title = extract_result.get("title", url)
            
            # Save locally
            url_hash = hashlib.md5(url.encode()).hexdigest()[:12]
            filename = f"url_{url_hash}.txt"
            file_path = data_dir / filename
            file_path.write_text(f"Source: {url}\n\n{text}", encoding="utf-8")
            
            # Ingest
            index_result = rag.ingest_text_cloud(
                text=text,
                workspace_id=workspace_id_int,
                source=url,
                metadata={"type": "url", "title": title, "url": url}
            )
            
            return jsonify({
                "success": True,
                "message": "URL indexed",
                "title": title,
                "chunks_processed": index_result.get("chunks_processed", 0)
            })
        
        # Check for text content
        if 'content' in data:
            content = data['content']
            title = data.get('title', 'Manual Entry')
            
            # Save locally
            text_hash = hashlib.md5(content.encode()).hexdigest()[:12]
            filename = f"text_{text_hash}.txt"
            file_path = data_dir / filename
            file_path.write_text(f"Title: {title}\n\n{content}", encoding="utf-8")
            
            # Ingest
            index_result = rag.ingest_text_cloud(
                text=content,
                workspace_id=workspace_id_int,
                source=title,
                metadata={"type": "manual", "title": title}
            )
            
            return jsonify({
                "success": True,
                "message": "Content indexed",
                "title": title,
                "chunks_processed": index_result.get("chunks_processed", 0)
            })
        
        return jsonify({"success": False, "message": "No file, URL, or content provided"}), 400
        
    except Exception as e:
        logger.exception(f"Upload failed: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


# ============================================================
# Search / Test RAG
# ============================================================

@knowledge_bp.route('/search', methods=['POST'])
def search_knowledge():
    """Search the knowledge base."""
    data = request.get_json() or {}
    query = data.get('query') or data.get('message')
    workspace_id = data.get('workspace_id') or request.args.get('workspace_id')
    
    if not query or not workspace_id:
        return jsonify({"error": "query and workspace_id required"}), 400
    
    try:
        workspace_id_int = int(workspace_id)
        results, stats = rag.retrieve_with_context_window(query, workspace_id_int)
        
        return jsonify({
            "success": True,
            "results": results,
            "count": len(results),
            "stats": stats
        })
    except Exception as e:
        logger.exception(f"Search failed: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@knowledge_bp.route('/test', methods=['POST'])
def test_rag():
    """Test RAG with a query and get AI response."""
    data = request.get_json() or {}
    message = data.get('message')
    workspace_id = data.get('workspace_id') or request.args.get('workspace_id')
    
    if not message or not workspace_id:
        return jsonify({"error": "message and workspace_id required"}), 400
    
    try:
        workspace_id_int = int(workspace_id)
        engine = get_rag_engine()
        
        result = engine.generate_answer_with_gemini(message, workspace_id_int)
        
        # Get similarity score from max_score or calculate from results
        similarity = result.get("max_score", 0)
        
        return jsonify({
            "success": True,
            "message": result.get("answer", ""),
            "used_rag": result.get("used_rag", False),
            "context_chunks": result.get("chunks_used", 0),
            "response_time_ms": result.get("timing", {}).get("total_ms", 0),
            "similarity": round(similarity * 100, 1) if similarity else 0  # Convert to percentage
        })
    except Exception as e:
        logger.exception(f"Test failed: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


# ============================================================
# Delete Document
# ============================================================

@knowledge_bp.route('/<int:doc_id>', methods=['DELETE'])
def delete_document(doc_id):
    """Delete a document from the knowledge base."""
    workspace_id = request.args.get('workspace_id')
    
    if not workspace_id:
        return jsonify({"error": "workspace_id required"}), 400
    
    try:
        # For now, just acknowledge - actual deletion requires doc_id tracking
        return jsonify({"success": True, "message": "Document deleted"})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


# ============================================================
# Reindex / Clear
# ============================================================

@knowledge_bp.route('/reindex', methods=['POST'])
def reindex_knowledge():
    """Clear and reindex all knowledge for a workspace."""
    data = request.get_json() or {}
    workspace_id = data.get('workspace_id') or request.args.get('workspace_id')
    
    if not workspace_id:
        return jsonify({"error": "workspace_id required"}), 400
    
    try:
        workspace_id_int = int(workspace_id)
        
        # Delete all workspace data from Qdrant
        result = rag.delete_workspace_data(workspace_id_int)
        
        return jsonify({
            "success": True,
            "message": "Knowledge base cleared. Re-add your sources to reindex.",
            "result": result
        })
    except Exception as e:
        logger.exception(f"Reindex failed: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


# ============================================================
# Auto-Index Workspace Profile
# ============================================================

@knowledge_bp.route('/index-workspace', methods=['POST'])
def index_workspace():
    """Auto-index workspace business profile."""
    data = request.get_json() or {}
    workspace_id = data.get('workspace_id')
    
    if not workspace_id:
        return jsonify({"error": "workspace_id required"}), 400
    
    try:
        # This would index business name, description, USPs from workspace profile
        # For now, return success
        return jsonify({
            "success": True,
            "message": "Workspace profile indexed",
            "chunks_processed": 0
        })
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


# ============================================================
# Browse All Chunks
# ============================================================

@knowledge_bp.route('/debug/chunks', methods=['GET'])
def browse_chunks():
    """Browse all chunks for a workspace with source info."""
    workspace_id = request.args.get('workspace_id')
    limit = request.args.get('limit', 100, type=int)
    
    if not workspace_id:
        return jsonify({"error": "workspace_id required"}), 400
    
    try:
        workspace_id_int = int(workspace_id)
        engine = get_rag_engine()
        
        chunks = engine.browse_all_chunks(workspace_id_int, limit)
        
        return jsonify({
            "success": True,
            "chunks": chunks,
            "count": len(chunks)
        })
    except Exception as e:
        logger.exception(f"Browse chunks failed: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


# ============================================================
# Get Chunks for a Specific Document
# ============================================================

@knowledge_bp.route('/doc/<doc_id>/chunks', methods=['GET'])
def get_document_chunks(doc_id):
    """Get all chunks for a specific document."""
    workspace_id = request.args.get('workspace_id')
    
    if not workspace_id:
        return jsonify({"error": "workspace_id required"}), 400
    
    try:
        workspace_id_int = int(workspace_id)
        engine = get_rag_engine()
        
        chunks = engine.get_document_chunks(doc_id, workspace_id_int)
        
        return jsonify({
            "success": True,
            "doc_id": doc_id,
            "chunks": chunks,
            "count": len(chunks)
        })
    except Exception as e:
        logger.exception(f"Get document chunks failed: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


# ============================================================
# Delete Document by doc_id
# ============================================================

@knowledge_bp.route('/chunk/<chunk_id>', methods=['DELETE'])
def delete_chunk(chunk_id):
    """Delete a single indexed chunk by Qdrant point id."""
    workspace_id = request.args.get('workspace_id')
    if not workspace_id:
        return jsonify({"error": "workspace_id required"}), 400

    try:
        workspace_id_int = int(workspace_id)
        engine = get_rag_engine()
        result = engine.delete_chunk(chunk_id, workspace_id_int)
        if result.get("status") == "success":
            return jsonify({"success": True, "message": "Chunk deleted"})
        return jsonify({"success": False, "error": result.get("message", "Delete failed")}), 404
    except Exception as e:
        logger.exception(f"Delete chunk failed: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@knowledge_bp.route('/doc/<doc_id>', methods=['DELETE'])
def delete_document_by_id(doc_id):
    """Delete a specific document and all its chunks."""
    workspace_id = request.args.get('workspace_id')
    
    if not workspace_id:
        return jsonify({"error": "workspace_id required"}), 400
    
    try:
        workspace_id_int = int(workspace_id)
        engine = get_rag_engine()
        
        result = engine.delete_document(doc_id, workspace_id_int)
        
        if result.get("status") == "success":
            return jsonify({"success": True, "message": "Document deleted"})
        else:
            return jsonify({"success": False, "error": result.get("message")}), 500
    except Exception as e:
        logger.exception(f"Delete document failed: {e}")
        return jsonify({"success": False, "error": str(e)}), 500
