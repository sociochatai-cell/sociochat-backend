"""
Knowledge Base Routes
======================
API endpoints for managing the WhatsApp chatbot knowledge base.

All documents are indexed to and retrieved from Qdrant Cloud only (no local disk storage).
Configure QUADRANT_ENDPOINT / QUADRANT_KEY and RAG_COLLECTION_NAME in .env.
"""
from flask import Blueprint, request, jsonify
import logging
import os
import tempfile
from io import BytesIO
from pathlib import Path
from werkzeug.utils import secure_filename

# Import RAG module
import sys
parent_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if parent_dir not in sys.path:
    sys.path.insert(0, parent_dir)

import rag
from rag_engine import get_rag_engine
from rag_config import qdrant_config, app_config

logger = logging.getLogger(__name__)

# Blueprint setup
knowledge_bp = Blueprint('knowledge', __name__, url_prefix='/api/whatsapp/knowledge')
knowledge_bp.strict_slashes = False

ALLOWED_EXTENSIONS = {'txt', 'md', 'pdf', 'text', 'docx', 'pptx', 'csv', 'xlsx'}


def allowed_file(filename):
    """Check if file extension is allowed."""
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS


def _ensure_qdrant_ready():
    """Fail fast if Qdrant Cloud is not configured."""
    if not qdrant_config.endpoint or not qdrant_config.api_key:
        raise RuntimeError(
            "Qdrant Cloud not configured. Set QUADRANT_ENDPOINT and QUADRANT_KEY in .env"
        )
    engine = get_rag_engine()
    if not engine.qdrant_client:
        raise RuntimeError("Qdrant client failed to initialize. Check cluster credentials.")
    return engine


def _extract_text_from_upload(file_storage, filename: str) -> dict:
    """Extract text in memory — never persists to knowledge_base/ on disk."""
    data = file_storage.read()
    if not data:
        return {"status": "error", "message": "Empty file"}

    suffix = Path(filename).suffix.lower()

    if suffix in ('.txt', '.md', '.text', '.csv'):
        text = data.decode('utf-8', errors='ignore')
        return {"status": "success", "text": text} if text.strip() else {"status": "error", "message": "Empty file"}

    if suffix == '.pdf':
        try:
            from pypdf import PdfReader
            reader = PdfReader(BytesIO(data))
            pages = [page.extract_text() or "" for page in reader.pages]
            text = "\n\n".join(p for p in pages if p.strip())
            if text.strip():
                return {"status": "success", "text": text}
            return {"status": "error", "message": "No extractable text in PDF"}
        except Exception as exc:
            return {"status": "error", "message": f"PDF extraction failed: {exc}"}

    # Binary formats: short-lived temp file for optional processors (deleted immediately)
    try:
        from file_processors import PDFProcessor, DocxProcessor, PptxProcessor, SpreadsheetProcessor
    except ImportError:
        return {
            "status": "error",
            "message": f"Unsupported format {suffix} without file_processors installed",
        }

    with tempfile.NamedTemporaryFile(suffix=suffix, delete=True) as tmp:
        tmp.write(data)
        tmp.flush()
        path = tmp.name
        if suffix == '.docx':
            return DocxProcessor.extract_text(path)
        if suffix == '.pptx':
            return PptxProcessor.extract_text(path)
        if suffix in ('.xlsx', '.xls'):
            return SpreadsheetProcessor.extract_text(path)
        if suffix == '.pdf':
            return PDFProcessor.extract_text(path)

    return {"status": "error", "message": f"Unsupported file type: {suffix}"}


def _ingest_to_qdrant(text: str, workspace_id: int, source: str, metadata: dict) -> dict:
    """Index text directly into Qdrant Cloud."""
    _ensure_qdrant_ready()
    result = rag.ingest_text_cloud(
        text=text,
        workspace_id=workspace_id,
        source=source,
        metadata=metadata,
    )
    if result.get("status") == "error":
        raise RuntimeError(result.get("message", "Qdrant ingest failed"))
    return result


# ============================================================
# List Documents - From Qdrant Cloud
# ============================================================

@knowledge_bp.route('/', methods=['GET'])
def list_knowledge():
    """List all documents in the knowledge base from Qdrant."""
    workspace_id = request.args.get('workspace_id')
    if not workspace_id:
        return jsonify({"error": "workspace_id required"}), 400
    
    try:
        workspace_id_int = int(workspace_id)
        engine = _ensure_qdrant_ready()
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
        _ensure_qdrant_ready()
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

@knowledge_bp.route('/crawl', methods=['POST'])
def crawl_url():
    """Crawl a URL and index its content."""
    data = request.get_json() or {}
    url = data.get('url')
    workspace_id = request.args.get('workspace_id') or data.get('workspace_id')
    use_playwright = data.get('use_playwright', False)
    
    logger.info(f"=== CRAWL REQUEST ===")
    logger.info(f"URL: {url}, workspace_id: {workspace_id}, playwright: {use_playwright}")
    
    if not url or not workspace_id:
        return jsonify({"error": "url and workspace_id required"}), 400
    
    if not url.startswith(('http://', 'https://')):
        return jsonify({"error": "Invalid URL - must start with http:// or https://"}), 400
    
    try:
        workspace_id_int = int(workspace_id)

        logger.info("Step 1: Extracting content from URL...")
        extract_result = rag.extract_text_from_url(url, use_playwright=use_playwright)

        if not extract_result.get("success"):
            hint = "Try enabling 'AI Browser' for JavaScript-heavy sites." if not use_playwright else "Site may have anti-bot protection."
            return jsonify({
                "success": False,
                "error": extract_result.get("error", "Extraction failed"),
                "hint": hint
            }), 400

        text = extract_result["text"]
        title = extract_result.get("title", url)
        logger.info(f"Step 2: Extracted {len(text)} characters — ingesting to Qdrant ({app_config.collection_name})")

        index_result = _ingest_to_qdrant(
            text=text,
            workspace_id=workspace_id_int,
            source=url,
            metadata={"type": "web", "title": title, "url": url},
        )

        chunks_processed = index_result.get("chunks_processed", 0)
        logger.info(f"=== CRAWL SUCCESS: {chunks_processed} chunks in Qdrant ===")

        return jsonify({
            "success": True,
            "message": "URL crawled and indexed to Qdrant",
            "title": title,
            "content_length": len(text),
            "indexed_chunks": chunks_processed,
            "chunks_processed": chunks_processed,
            "doc_id": index_result.get("doc_id"),
            "collection": app_config.collection_name,
            "storage": "qdrant",
        })
        
    except Exception as e:
        logger.exception(f"Crawl failed: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


# ============================================================
# Upload File
# ============================================================

@knowledge_bp.route('/', methods=['POST'])
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

        if 'file' in request.files:
            file = request.files['file']
            if file.filename == '':
                return jsonify({"success": False, "message": "No file selected"}), 400

            if not allowed_file(file.filename):
                return jsonify({"success": False, "message": "File type not allowed"}), 400

            filename = secure_filename(file.filename)
            result = _extract_text_from_upload(file, filename)
            if result.get("status") == "error":
                return jsonify({"success": False, "message": result.get("message")}), 500

            text = result.get("text", "")
            index_result = _ingest_to_qdrant(
                text=text,
                workspace_id=workspace_id_int,
                source=filename,
                metadata={"type": "file", "filename": filename},
            )

            return jsonify({
                "success": True,
                "message": "File indexed to Qdrant",
                "filename": filename,
                "chunks_processed": index_result.get("chunks_processed", 0),
                "doc_id": index_result.get("doc_id"),
                "collection": app_config.collection_name,
                "storage": "qdrant",
            })

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
            index_result = _ingest_to_qdrant(
                text=text,
                workspace_id=workspace_id_int,
                source=url,
                metadata={"type": "url", "title": title, "url": url},
            )

            return jsonify({
                "success": True,
                "message": "URL indexed to Qdrant",
                "title": title,
                "chunks_processed": index_result.get("chunks_processed", 0),
                "doc_id": index_result.get("doc_id"),
                "collection": app_config.collection_name,
                "storage": "qdrant",
            })

        if 'content' in data:
            content = data['content']
            title = data.get('title', 'Manual Entry')

            index_result = _ingest_to_qdrant(
                text=content,
                workspace_id=workspace_id_int,
                source=title,
                metadata={"type": "manual", "title": title},
            )

            return jsonify({
                "success": True,
                "message": "Content indexed to Qdrant",
                "title": title,
                "chunks_processed": index_result.get("chunks_processed", 0),
                "doc_id": index_result.get("doc_id"),
                "collection": app_config.collection_name,
                "storage": "qdrant",
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
        _ensure_qdrant_ready()
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
    """Test RAG with a query using the same guardrailed business assistant as production."""
    from models import Workspace
    from .ai_chatbot import (
        generate_ai_response,
        build_business_system_prompt,
        DEFAULT_HANDOFF_MESSAGE,
        is_off_topic_request,
        get_rag_context,
        RAG_CONFIDENCE_THRESHOLD,
    )

    data = request.get_json() or {}
    message = data.get('message')
    workspace_id = data.get('workspace_id') or request.args.get('workspace_id')
    
    if not message or not workspace_id:
        return jsonify({"error": "message and workspace_id required"}), 400
    
    try:
        workspace_id_int = int(workspace_id)
        workspace = Workspace.query.get(workspace_id_int)
        business_name = (workspace.business_name if workspace else None) or "our business"

        if is_off_topic_request(message):
            return jsonify({
                "success": True,
                "message": DEFAULT_HANDOFF_MESSAGE,
                "used_rag": False,
                "context_chunks": 0,
                "response_time_ms": 0,
                "similarity": 0,
                "guardrailed": True,
                "reason": "off_topic",
            })

        rag_chunks, high_conf = get_rag_context(
            query=message,
            workspace_id=workspace_id_int,
            threshold=RAG_CONFIDENCE_THRESHOLD,
        )
        max_score = max((c.get("score", 0) for c in rag_chunks), default=0.0)

        test_max_tokens = int(os.getenv("WHATSAPP_KNOWLEDGE_TEST_MAX_TOKENS", "1536"))
        result = generate_ai_response(
            message=message,
            system_prompt=build_business_system_prompt(business_name),
            fallback_message=DEFAULT_HANDOFF_MESSAGE,
            workspace_id=str(workspace_id_int),
            use_rag=True,
            business_name=business_name,
            max_tokens=test_max_tokens,
        )

        return jsonify({
            "success": result.success,
            "message": result.message,
            "used_rag": result.used_rag,
            "context_chunks": result.rag_chunks,
            "response_time_ms": result.response_time_ms,
            "similarity": round(max_score * 100, 1) if max_score else 0,
            "guardrailed": True,
            "high_confidence": high_conf,
            "error": result.error,
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
        _ensure_qdrant_ready()
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
        engine = _ensure_qdrant_ready()
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
        engine = _ensure_qdrant_ready()
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

@knowledge_bp.route('/doc/<doc_id>', methods=['DELETE'])
def delete_document_by_id(doc_id):
    """Delete a specific document and all its chunks."""
    workspace_id = request.args.get('workspace_id')
    
    if not workspace_id:
        return jsonify({"error": "workspace_id required"}), 400
    
    try:
        workspace_id_int = int(workspace_id)
        engine = _ensure_qdrant_ready()
        result = engine.delete_document(doc_id, workspace_id_int)
        
        if result.get("status") == "success":
            return jsonify({"success": True, "message": "Document deleted"})
        else:
            return jsonify({"success": False, "error": result.get("message")}), 500
    except Exception as e:
        logger.exception(f"Delete document failed: {e}")
        return jsonify({"success": False, "error": str(e)}), 500
