"""
Core RAG Engine for Multilingual Text Processing.
=================================================
Handles embeddings, vector storage, and retrieval.
Based on proven multilingual_rag implementation.

Uses workspace_id for multi-tenant isolation.
"""
import uuid
import time
import re
import logging
import builtins
from typing import List, Dict, Optional, Tuple
from dotenv import load_dotenv
import os

logger = logging.getLogger(__name__)


def _safe_print(*args, **kwargs):
    """Avoid UnicodeEncodeError on Windows consoles (cp1252) when logging uses emoji."""
    try:
        builtins.print(*args, **kwargs)
    except UnicodeEncodeError:
        text = " ".join(str(a) for a in args)
        logger.info(text.encode("ascii", "replace").decode("ascii"))


print = _safe_print  # noqa: A001

# Load env variables
load_dotenv()

# Import configuration
from rag_config import qdrant_config, google_embed_config, gemini_config, app_config

# Qdrant imports
try:
    from qdrant_client import QdrantClient
    from qdrant_client.http import models
    QDRANT_AVAILABLE = True
except ImportError:
    QDRANT_AVAILABLE = False
    print("⚠️ qdrant-client not installed. Run: pip install qdrant-client")

# Google GenAI imports (New SDK)
try:
    from google import genai
    from google.genai.types import HttpOptions, GenerateContentConfig, EmbedContentConfig
    GENAI_AVAILABLE = True
except ImportError:
    GENAI_AVAILABLE = False
    EmbedContentConfig = None  # type: ignore
    print("⚠️ google-genai not installed. Run: pip install google-genai")


class RAGEngine:
    """
    Multilingual RAG Engine (Powered by Google Gemini & Embeddings).
    Handles text ingestion, embedding generation, and semantic search.
    
    Uses workspace_id for multi-tenant isolation.
    """

    def __init__(self):
        """Initialize Qdrant and Gemini clients."""
        self.qdrant_client = None
        self.gemini_client = None
        self._use_vertex = False
        self._embedding_model = google_embed_config.api_model_name
        self._initialize_clients()

    def _initialize_clients(self):
        """Initialize API clients with error handling."""
        try:
            # Initialize Qdrant Cloud
            if qdrant_config.endpoint and qdrant_config.api_key and QDRANT_AVAILABLE:
                self.qdrant_client = QdrantClient(
                    url=qdrant_config.endpoint,
                    api_key=qdrant_config.api_key,
                    timeout=120,
                    check_compatibility=False,
                )
                logger.info(
                    "Connected to Qdrant Cloud: %s (collection=%s)",
                    qdrant_config.endpoint,
                    app_config.collection_name,
                )
            else:
                logger.warning("Qdrant credentials not found or client not available.")
            
            # Initialize Gemini: API key (dev/Docker) or Vertex AI (GCP production)
            if GENAI_AVAILABLE:
                api_key = (gemini_config.api_key or "").strip()
                if api_key and not gemini_config.use_vertex:
                    self.gemini_client = genai.Client(api_key=api_key)
                    self._use_vertex = False
                    self._embedding_model = google_embed_config.api_model_name
                    logger.info("Google GenAI client initialized (API key mode).")
                else:
                    project = (
                        os.environ.get("GCP_PROJECT")
                        or os.environ.get("PROJECT_ID")
                        or os.environ.get("GOOGLE_CLOUD_PROJECT")
                    )
                    if not project:
                        logger.warning(
                            "No GEMINI_API_KEY and no GCP_PROJECT for Vertex AI. "
                            "Knowledge base embeddings will fail until an API key is set in .env"
                        )
                    else:
                        location = os.environ.get("GOOGLE_CLOUD_LOCATION") or "us-central1"
                        self.gemini_client = genai.Client(
                            http_options=HttpOptions(api_version="v1"),
                            project=project,
                            location=location,
                            vertexai=True,
                        )
                        self._use_vertex = True
                        self._embedding_model = google_embed_config.vertex_model_name
                        logger.info("Vertex AI client initialized (project=%s).", project)
                if self.gemini_client:
                    logger.info("Using Google Embedding Model: %s", self._embedding_model)
            else:
                logger.warning("google-genai SDK not found. AI operations will fail.")

        except Exception as e:
            logger.exception("Client initialization failed: %s", e)
            raise

    def _normalize_task_type(self, task_type: str) -> str:
        """Map legacy lowercase task types to Gemini API enum values."""
        normalized = (task_type or "RETRIEVAL_DOCUMENT").strip().upper()
        aliases = {
            "RETRIEVAL_DOCUMENT": "RETRIEVAL_DOCUMENT",
            "RETRIEVAL_QUERY": "RETRIEVAL_QUERY",
            "RETRIEVAL_DOC": "RETRIEVAL_DOCUMENT",
        }
        return aliases.get(normalized, normalized)

    def get_embedding(self, text: str, task_type: str = "RETRIEVAL_DOCUMENT") -> List[float]:
        """
        Generate embeddings (Gemini API: gemini-embedding-001, Vertex: text-embedding-004).
        task_type: RETRIEVAL_DOCUMENT | RETRIEVAL_QUERY
        """
        if not text or not text.strip():
            return []

        if not self.gemini_client:
            raise RuntimeError("GenAI client not initialized")

        task = self._normalize_task_type(task_type)
        models_to_try = [self._embedding_model]
        if self._embedding_model != google_embed_config.api_model_name:
            models_to_try.append(google_embed_config.api_model_name)
        if self._embedding_model != google_embed_config.vertex_model_name:
            models_to_try.append(google_embed_config.vertex_model_name)

        last_error: Optional[Exception] = None
        for model_name in models_to_try:
            try:
                embed_config = None
                if EmbedContentConfig is not None:
                    embed_config = EmbedContentConfig(
                        task_type=task,
                        output_dimensionality=google_embed_config.vector_size,
                    )
                result = self.gemini_client.models.embed_content(
                    model=model_name,
                    contents=text,
                    config=embed_config or {"task_type": task},
                )
                if model_name != self._embedding_model:
                    self._embedding_model = model_name
                    print(f"ℹ️ Embedding model fallback succeeded: {model_name}")
                return result.embeddings[0].values
            except Exception as e:
                last_error = e
                err_text = str(e)
                if "404" in err_text or "NOT_FOUND" in err_text:
                    continue
                break

        err = str(last_error) if last_error else "Unknown embedding error"
        if "DefaultCredentialsError" in err or "credentials were not found" in err.lower():
            err = (
                "Google AI credentials missing. Set GOOGLE_GENAI_API_KEY (or GEMINI_API_KEY) "
                "in whatsapp-service/.env and restart the API container."
            )
        elif "404" in err or "NOT_FOUND" in err:
            err = (
                f"Embedding model not available ({self._embedding_model}). "
                "Set EMBEDDING_MODEL=gemini-embedding-001 for API keys or use Vertex with "
                "VERTEX_EMBEDDING_MODEL=text-embedding-004."
            )
        print(f"❌ Embedding Error: {err}")
        raise RuntimeError(err) from last_error

    def ingest_text(self, text: str, workspace_id: int, metadata: Dict = None, collection_name: str = None) -> Dict[str, any]:
        """
        Chunk text, generate Google embeddings, and store in Qdrant.
        Uses workspace_id for multi-tenant isolation.
        """
        if collection_name is None:
            collection_name = app_config.collection_name

        if not text or len(text.strip()) < 50:
            return {"status": "error", "message": "Text too short to ingest (minimum 50 characters)"}

        try:
            # Ensure collection exists
            self.create_collection(collection_name)
            
            # 1. Chunk Text
            print(f"📄 Chunking text of length {len(text)}")
            chunks = self.chunk_text(text)
            print(f"📄 Generated {len(chunks)} chunks.")
            
            if not chunks:
                return {"status": "error", "message": "No chunks generated from text"}

            points = []
            doc_id = str(uuid.uuid4())
            
            for i, chunk in enumerate(chunks):
                # 2. Generate Embedding
                embedding = self.get_embedding(chunk, task_type="RETRIEVAL_DOCUMENT")
                
                if not embedding:
                    print(f"⚠️ Skipping chunk {i} - no embedding generated")
                    continue
                
                # 3. Prepare Point
                point_id = str(uuid.uuid4())
                payload = {
                    "doc_id": doc_id,
                    "text_content": chunk,
                    "text": chunk,  # Alias
                    "workspace_id": workspace_id,  # Multi-tenant isolation
                    "metadata": metadata or {},
                    "chunk_index": i,
                    "total_chunks": len(chunks)
                }
                
                points.append(models.PointStruct(
                    id=point_id,
                    vector=embedding,
                    payload=payload
                ))
            
            if not points:
                return {"status": "error", "message": "No valid embeddings generated"}
            
            print(f"🚀 Upserting {len(points)} points to '{collection_name}'...")

            # 4. Upload to Qdrant with auto-heal for dimension mismatch
            try:
                self.qdrant_client.upsert(
                    collection_name=collection_name,
                    points=points,
                    wait=True
                )
            except Exception as e:
                error_msg = str(e)
                if hasattr(e, 'content') and e.content:
                    try:
                        error_msg += " " + e.content.decode('utf-8', errors='ignore')
                    except:
                        pass
                
                if "dimension" in error_msg.lower():
                    print(f"⚠️ Dimension mismatch. Recreating collection...")
                    self.qdrant_client.delete_collection(collection_name)
                    self.create_collection(collection_name)
                    self.qdrant_client.upsert(
                        collection_name=collection_name,
                        points=points,
                        wait=True
                    )
                else:
                    raise e
            
            print(f"✅ Successfully ingested {len(points)} chunks for workspace {workspace_id}")
            
            return {
                "status": "success",
                "chunks_processed": len(points),
                "collection_name": collection_name,
                "workspace_id": workspace_id,
                "doc_id": doc_id,
                "message": f"Successfully ingested {len(points)} chunks."
            }

        except Exception as e:
            print(f"❌ Ingestion failed: {e}")
            import traceback
            traceback.print_exc()
            return {"status": "error", "message": str(e)}

    def retrieve(
        self,
        query: str,
        workspace_id: int,
        top_k: int = 5,
        collection_name: str = None,
        score_threshold: float | None = None,
    ) -> Tuple[List[Dict], Dict]:
        """
        Retrieve relevant context for a query.
        Returns (results, stats).
        
        Enhanced with:
        - Multilingual query preprocessing
        - Score normalization for better confidence assessment
        """
        stats = {"embed_ms": 0, "search_ms": 0, "preprocess_ms": 0}
        
        if collection_name is None:
            collection_name = app_config.collection_name

        try:
            # 0. Preprocess query for better retrieval
            t_pre = time.time()
            processed_query = self.preprocess_query(query)
            stats["preprocess_ms"] = int((time.time() - t_pre) * 1000)
            
            # 1. Generate Query Embedding
            t0 = time.time()
            query_embedding = self.get_embedding(processed_query, task_type="RETRIEVAL_QUERY")
            stats["embed_ms"] = int((time.time() - t0) * 1000)

            if not query_embedding:
                return [], stats

            # 2. Search in Qdrant with workspace filter
            # Retrieve more candidates for reranking
            t1 = time.time()
            qdrant_threshold = (
                score_threshold if score_threshold is not None else app_config.score_threshold
            )
            search_result = self.qdrant_client.query_points(
                collection_name=collection_name,
                query=query_embedding,
                limit=top_k * 2,  # Get more for reranking
                score_threshold=qdrant_threshold,
                query_filter=models.Filter(
                    must=[
                        models.FieldCondition(
                            key="workspace_id",
                            match=models.MatchValue(value=workspace_id)
                        )
                    ]
                )
            ).points
            stats["search_ms"] = int((time.time() - t1) * 1000)

            # 3. Rerank results based on keyword overlap + semantic score
            reranked_results = self._rerank_results(search_result, query, top_k)

            # 4. Format Results with Context Window Expansion
            results = []
            for hit in reranked_results:
                text_content = hit.payload.get("text_content") or hit.payload.get("text", "")
                
                # Context Window: Fetch next chunk for continuity
                try:
                    doc_id = hit.payload.get("doc_id")
                    chunk_index = hit.payload.get("chunk_index")
                    total_chunks = hit.payload.get("total_chunks")
                    
                    if doc_id and chunk_index is not None and total_chunks and (chunk_index + 1) < total_chunks:
                        next_filter = models.Filter(
                            must=[
                                models.FieldCondition(key="doc_id", match=models.MatchValue(value=doc_id)),
                                models.FieldCondition(key="chunk_index", match=models.MatchValue(value=chunk_index + 1))
                            ]
                        )
                        next_points = self.qdrant_client.scroll(
                            collection_name=collection_name,
                            scroll_filter=next_filter,
                            limit=1
                        )[0]
                        
                        if next_points:
                            next_text = next_points[0].payload.get("text_content", "")
                            text_content += " " + next_text
                except Exception as e:
                    print(f"⚠️ Context expansion failed: {e}")

                results.append({
                    "text": text_content,
                    "score": hit.score,
                    "metadata": hit.payload.get("metadata", {}),
                    "source": hit.payload.get("metadata", {}).get("source", "unknown")
                })

            return results, stats

        except Exception as e:
            print(f"❌ Retrieval failed: {e}")
            import traceback
            traceback.print_exc()
            return [], stats

    def search(
        self,
        query: str,
        workspace_id: int,
        top_k: int = 5,
        collection_name: str = None,
        score_threshold: float | None = None,
    ) -> List[Dict]:
        """Alias for retrieve() — returns result list only (stub compatibility)."""
        results, _stats = self.retrieve(
            query,
            int(workspace_id),
            top_k=top_k,
            collection_name=collection_name,
            score_threshold=score_threshold,
        )
        return results

    def add_document(self, text: str, workspace_id: int, metadata: Dict = None, **kwargs) -> Dict:
        """Alias for ingest_text() — stub compatibility."""
        return self.ingest_text(text, int(workspace_id), metadata or {}, kwargs.get("collection_name"))

    def _rerank_results(self, search_results: list, query: str, top_k: int) -> list:
        """
        Rerank search results using keyword overlap boost.
        
        Combines semantic similarity score with keyword overlap for
        better relevance ranking, especially for exact term matches.
        """
        if not search_results:
            return []
        
        # Extract query keywords (lowercase, filtered)
        stop_words = {'a', 'an', 'the', 'is', 'are', 'was', 'were', 'be', 'been', 
                      'have', 'has', 'had', 'do', 'does', 'did', 'will', 'would',
                      'could', 'should', 'may', 'might', 'must', 'to', 'of', 'in',
                      'for', 'on', 'with', 'at', 'by', 'from', 'what', 'where',
                      'when', 'how', 'why', 'who', 'which', 'this', 'that', 'it',
                      'i', 'me', 'my', 'we', 'our', 'you', 'your', 'he', 'she',
                      'they', 'them', 'hi', 'hello', 'hey', 'please', 'thanks'}
        
        query_words = set(
            word.lower() for word in re.findall(r'\w+', query)
            if len(word) >= 2 and word.lower() not in stop_words
        )
        
        query_lower = query.strip().lower()
        reranked = []
        for hit in search_results:
            text = (hit.payload.get("text_content") or hit.payload.get("text", "")).lower()
            text_words = set(re.findall(r'\w+', text))

            # Calculate keyword overlap
            overlap = len(query_words.intersection(text_words))
            keyword_boost = min(overlap * 0.05, 0.2)  # Max 20% boost

            # Strong boost when the full query appears in chunk (e.g. "Puraniks", "Mahindra")
            if query_lower and len(query_lower) >= 3 and query_lower in text:
                keyword_boost = max(keyword_boost, 0.18)

            # Combined score: semantic + keyword boost
            combined_score = hit.score + keyword_boost
            
            reranked.append({
                'hit': hit,
                'original_score': hit.score,
                'combined_score': combined_score,
                'keyword_matches': overlap
            })
        
        # Sort by combined score
        reranked.sort(key=lambda x: x['combined_score'], reverse=True)
        
        # Return top_k original hits with updated scores
        top_hits = reranked[:top_k]
        for item in top_hits:
            item["hit"].score = item["combined_score"]
        return [r["hit"] for r in top_hits]

    def generate_answer_with_gemini(self, query: str, workspace_id: int, collection_name: str = None) -> Dict:
        """Generate a guardrailed business-assistant answer (delegates to ai_chatbot)."""
        start_time = time.time()
        try:
            from whatsapp.ai_chatbot import (
                generate_ai_response,
                build_business_system_prompt,
                DEFAULT_HANDOFF_MESSAGE,
                get_rag_context,
                RAG_CONFIDENCE_THRESHOLD,
            )

            rag_chunks, _ = get_rag_context(
                query=query,
                workspace_id=int(workspace_id),
                threshold=RAG_CONFIDENCE_THRESHOLD,
            )
            max_score = max((c.get("score", 0) for c in rag_chunks), default=0.0)

            result = generate_ai_response(
                message=query,
                system_prompt=build_business_system_prompt(),
                fallback_message=DEFAULT_HANDOFF_MESSAGE,
                workspace_id=str(workspace_id),
                use_rag=True,
            )
            total_ms = int((time.time() - start_time) * 1000)
            return {
                "answer": result.message,
                "used_rag": result.used_rag,
                "chunks_used": result.rag_chunks,
                "timing": {"total_ms": result.response_time_ms or total_ms},
                "max_score": max_score,
                "fallback_retrieval_used": False,
                "guardrailed": True,
                "error": result.error,
            }
        except Exception as e:
            print(f"❌ Gemini Generation Error: {e}")
            handoff = (
                "I don't have enough information on this. "
                "I'll connect you with our team who can assist you further."
            )
            return {
                "answer": handoff,
                "used_rag": False,
                "timing": {"total_ms": int((time.time() - start_time) * 1000)},
                "max_score": 0,
                "chunks_used": 0,
                "fallback_retrieval_used": False,
                "error": str(e),
            }

    def create_collection(self, collection_name: str = None) -> Dict[str, str]:
        """Create Qdrant collection with proper indexes."""
        if collection_name is None:
            collection_name = app_config.collection_name
        
        if not self.qdrant_client:
            return {"status": "error", "message": "Qdrant client not available"}
        
        try:
            if not self.qdrant_client.collection_exists(collection_name):
                self.qdrant_client.create_collection(
                    collection_name=collection_name,
                    vectors_config=models.VectorParams(
                        size=google_embed_config.vector_size,  # 768
                        distance=models.Distance.COSINE
                    )
                )
                # Create indexes for filtering
                self.qdrant_client.create_payload_index(
                    collection_name=collection_name,
                    field_name="workspace_id",
                    field_schema=models.PayloadSchemaType.INTEGER
                )
                self.qdrant_client.create_payload_index(
                    collection_name=collection_name,
                    field_name="doc_id",
                    field_schema=models.PayloadSchemaType.KEYWORD
                )
                self.qdrant_client.create_payload_index(
                    collection_name=collection_name,
                    field_name="chunk_index",
                    field_schema=models.PayloadSchemaType.INTEGER
                )
                print(f"✅ Collection '{collection_name}' created with indexes!")
                return {"status": "created", "collection_name": collection_name}
            else:
                return {"status": "exists", "collection_name": collection_name}
        except Exception as e:
            print(f"❌ Collection creation failed: {e}")
            return {"status": "error", "message": str(e)}

    # ============================================================
    # Multilingual Text Preprocessing
    # ============================================================
    
    def preprocess_multilingual_text(self, text: str) -> str:
        """
        Normalize multilingual text for better embedding quality.
        Handles: Hindi, Hinglish (Roman Hindi), Telugu, Tinglish (Roman Telugu), English
        """
        if not text:
            return ""
        
        # Common Hinglish/Tinglish abbreviations and their expansions
        abbreviation_map = {
            # Hinglish common
            r'\bkya\b': 'क्या kya what',
            r'\bhai\b': 'है hai is',
            r'\bhain\b': 'हैं hain are',
            r'\bkaise\b': 'कैसे kaise how',
            r'\bkab\b': 'कब kab when',
            r'\bkahan\b': 'कहाँ kahan where',
            r'\bkyun\b': 'क्यों kyun why',
            r'\bkitna\b': 'कितना kitna how much',
            r'\baap\b': 'आप aap you',
            r'\bmujhe\b': 'मुझे mujhe me',
            r'\bhum\b': 'हम hum we',
            r'\bthik\b': 'ठीक thik okay',
            r'\bachha\b': 'अच्छा achha good okay',
            r'\bnahi\b': 'नहीं nahi no not',
            r'\bji\b': 'जी ji yes sir',
            r'\bdhanyawad\b': 'धन्यवाद dhanyawad thank you',
            r'\bshukriya\b': 'शुक्रिया shukriya thank you',
            r'\bkaro\b': 'करो karo do',
            r'\bkarna\b': 'करना karna to do',
            r'\bbolna\b': 'बोलना bolna to speak',
            r'\bbatao\b': 'बताओ batao tell',
            r'\bprice\b': 'price कीमत kimat cost',
            r'\brate\b': 'rate दर dar price',
            # Tinglish common
            r'\bemi\b': 'ఏమి emi what',
            r'\bencheppandi\b': 'ఎంచెప్పండి encheppandi tell me',
            r'\bela\b': 'ఎలా ela how',
            r'\beppudu\b': 'ఎప్పుడు eppudu when',
            r'\bekkada\b': 'ఎక్కడ ekkada where',
            r'\benduku\b': 'ఎందుకు enduku why',
            r'\benta\b': 'ఎంత enta how much',
            r'\bmeeru\b': 'మీరు meeru you',
            r'\bnenu\b': 'నేను nenu I',
            r'\bmanchi\b': 'మంచి manchi good',
            r'\bkadu\b': 'కాదు kadu no not',
            r'\bavunu\b': 'అవును avunu yes',
            r'\bcheyandi\b': 'చేయండి cheyandi do please',
            r'\bcheppandi\b': 'చెప్పండి cheppandi tell please',
            # Common SMS/chat abbreviations
            r'\bu\b': 'you',
            r'\br\b': 'are',
            r'\bur\b': 'your',
            r'\bpls\b': 'please',
            r'\bplz\b': 'please',
            r'\bthx\b': 'thanks thank you',
            r'\bthnx\b': 'thanks thank you',
            r'\bthnks\b': 'thanks thank you',
            r'\bty\b': 'thank you thanks',
            r'\bwthr\b': 'weather',
            r'\bbt\b': 'but',
            r'\bwht\b': 'what',
            r'\bhw\b': 'how',
            r'\basap\b': 'as soon as possible urgent',
            r'\bidk\b': 'i don\'t know',
            r'\bimo\b': 'in my opinion',
            r'\bbtw\b': 'by the way',
            r'\bfyi\b': 'for your information',
        }
        
        # Apply expansions (case-insensitive)
        processed = text
        for pattern, expansion in abbreviation_map.items():
            processed = re.sub(pattern, f'{expansion}', processed, flags=re.IGNORECASE)
        
        # Normalize whitespace
        processed = re.sub(r'\s+', ' ', processed).strip()
        
        return processed

    def preprocess_query(self, query: str) -> str:
        """
        Preprocess a user query for better retrieval.
        - Expands abbreviations
        - Adds semantic context for common query patterns
        """
        query = self.preprocess_multilingual_text(query)
        
        # Intent-based query expansion
        intent_expansions = {
            r'(price|cost|rate|kimat|daam|rent)': 'price cost pricing rate fees charges कीमत दाम',
            r'(time|timing|hours|working|open)': 'time timing hours schedule open close working',
            r'(location|address|where|kahan)': 'location address where place directions map',
            r'(contact|phone|call|number)': 'contact phone number call reach mobile',
            r'(help|support|assist|problem)': 'help support assistance problem issue',
            r'(book|booking|reserve|appointment)': 'book booking reservation appointment schedule',
            r'(cancel|refund|return)': 'cancel cancellation refund return policy',
            r'(delivery|shipping|dispatch)': 'delivery shipping dispatch courier tracking',
            r'(payment|pay|method)': 'payment pay methods upi card cash online',
            r'(offer|discount|deal|promo)': 'offer discount deal promo coupon sale',
        }
        
        expanded = query
        for pattern, expansion in intent_expansions.items():
            if re.search(pattern, query, re.IGNORECASE):
                expanded = f"{query} {expansion}"
                break
        
        return expanded

    def chunk_text(self, text: str) -> List[str]:
        """
        Split text into overlapping chunks using sliding window approach.
        
        Strategy: 50% overlap ensures context is preserved across chunk boundaries.
        Example with chunk_size=400, overlap=200:
        - Chunk 1: chars 0-400
        - Chunk 2: chars 200-600 (overlaps 200-400 with Chunk 1)
        - Chunk 3: chars 400-800 (overlaps 400-600 with Chunk 2)
        
        This prevents information loss at boundaries that simple sequential
        chunking causes.
        """
        # Preprocess for multilingual
        text = self.preprocess_multilingual_text(text)
        
        # Clean whitespace
        text = re.sub(r'\s+', ' ', text.strip())
        
        if not text:
            return []
        
        max_size = app_config.chunk_size  # e.g., 600
        overlap_size = app_config.chunk_overlap  # e.g., 150 (25% overlap)
        step_size = max_size - overlap_size  # e.g., 450
        
        # If text is shorter than max_size, return as single chunk
        if len(text) <= max_size:
            return [text]
        
        chunks = []
        start = 0
        
        while start < len(text):
            end = start + max_size
            chunk = text[start:end]
            
            # Try to end at a sentence boundary for cleaner chunks
            if end < len(text):
                # Look for sentence ending in last 100 chars
                sentence_endings = ['.', '!', '?', '।', '\n']
                best_end = None
                
                for i in range(min(100, len(chunk) - 50), 0, -1):
                    if len(chunk) - i > 50 and chunk[-(i)] in sentence_endings:
                        best_end = len(chunk) - i + 1
                        break
                
                if best_end:
                    chunk = chunk[:best_end]
            
            chunk = chunk.strip()
            if chunk and len(chunk) > 30:  # Minimum meaningful chunk
                chunks.append(chunk)
            
            start += step_size
            
            # Safety: prevent infinite loop
            if start >= len(text) - 30:
                break
        
        # Handle any remaining text
        if start < len(text):
            final_chunk = text[max(0, len(text) - max_size):].strip()
            if final_chunk and len(final_chunk) > 30:
                # Avoid duplicate of last chunk
                if not chunks or final_chunk != chunks[-1]:
                    chunks.append(final_chunk)
        
        return chunks if chunks else [text]

    def delete_workspace_data(self, workspace_id: int, collection_name: str = None) -> Dict[str, str]:
        """Delete all data for a specific workspace."""
        if collection_name is None:
            collection_name = app_config.collection_name
            
        try:
            result = self.qdrant_client.delete(
                collection_name=collection_name,
                points_selector=models.FilterSelector(
                    filter=models.Filter(
                        must=[
                            models.FieldCondition(
                                key="workspace_id",
                                match=models.MatchValue(value=workspace_id)
                            )
                        ]
                    )
                )
            )
            print(f"🗑️ Deleted all data for workspace_id={workspace_id}")
            return {"status": "success", "message": f"Deleted all data for workspace {workspace_id}"}
        except Exception as e:
            print(f"❌ Delete error: {e}")
            return {"status": "error", "message": str(e)}

    def get_collection_info(self, collection_name: str = None) -> Dict:
        """Get collection statistics."""
        if collection_name is None:
            collection_name = app_config.collection_name
        try:
            if not self.qdrant_client.collection_exists(collection_name):
                return {"status": "not_found", "points_count": 0}
            
            info = self.qdrant_client.get_collection(collection_name)
            return {
                "status": "exists",
                "collection_name": collection_name,
                "points_count": info.points_count
            }
        except Exception as e:
            return {"status": "error", "message": str(e), "points_count": 0}

    def get_workspace_stats(self, workspace_id: int, collection_name: str = None) -> Dict:
        """Get statistics for a specific workspace."""
        if collection_name is None:
            collection_name = app_config.collection_name
        
        try:
            if not self.qdrant_client.collection_exists(collection_name):
                return {"total_chunks": 0, "indexed_documents": 0, "total_documents": 0, "documents": []}
            
            # Count points for this workspace
            result = self.qdrant_client.count(
                collection_name=collection_name,
                count_filter=models.Filter(
                    must=[
                        models.FieldCondition(
                            key="workspace_id",
                            match=models.MatchValue(value=workspace_id)
                        )
                    ]
                )
            )
            
            chunk_count = result.count
            
            # Get document list with chunk counts
            documents = self.get_workspace_documents(workspace_id, collection_name)
            
            return {
                "total_chunks": chunk_count,
                "indexed_documents": len(documents),
                "total_documents": len(documents),
                "documents": documents
            }
        except Exception as e:
            print(f"❌ Stats error: {e}")
            return {"total_chunks": 0, "indexed_documents": 0, "total_documents": 0, "documents": []}

    def get_workspace_documents(self, workspace_id: int, collection_name: str = None) -> List[Dict]:
        """Get all unique documents for a workspace with their chunk counts."""
        if collection_name is None:
            collection_name = app_config.collection_name
        
        try:
            if not self.qdrant_client.collection_exists(collection_name):
                return []
            
            # Scroll through all points for this workspace
            all_points = []
            offset = None
            
            while True:
                result, next_offset = self.qdrant_client.scroll(
                    collection_name=collection_name,
                    scroll_filter=models.Filter(
                        must=[
                            models.FieldCondition(
                                key="workspace_id",
                                match=models.MatchValue(value=workspace_id)
                            )
                        ]
                    ),
                    limit=100,
                    offset=offset,
                    with_payload=True,
                    with_vectors=False
                )
                
                all_points.extend(result)
                
                if next_offset is None or len(result) == 0:
                    break
                offset = next_offset
            
            # Group by doc_id
            doc_map = {}
            for point in all_points:
                doc_id = point.payload.get("doc_id", "unknown")
                metadata = point.payload.get("metadata", {})
                
                if doc_id not in doc_map:
                    doc_map[doc_id] = {
                        "doc_id": doc_id,
                        "title": metadata.get("title", metadata.get("source", "Unknown")),
                        "source": metadata.get("source", ""),
                        "source_type": metadata.get("type", "unknown"),
                        "chunk_count": 0,
                        "total_chunks": point.payload.get("total_chunks", 0),
                        "url": metadata.get("url", ""),
                        "filename": metadata.get("filename", "")
                    }
                
                doc_map[doc_id]["chunk_count"] += 1
            
            return list(doc_map.values())
            
        except Exception as e:
            print(f"❌ Get documents error: {e}")
            return []

    def get_document_chunks(self, doc_id: str, workspace_id: int, collection_name: str = None) -> List[Dict]:
        """Get all chunks for a specific document."""
        if collection_name is None:
            collection_name = app_config.collection_name
        
        try:
            results = []
            offset = None
            
            while True:
                result, next_offset = self.qdrant_client.scroll(
                    collection_name=collection_name,
                    scroll_filter=models.Filter(
                        must=[
                            models.FieldCondition(
                                key="workspace_id",
                                match=models.MatchValue(value=workspace_id)
                            ),
                            models.FieldCondition(
                                key="doc_id",
                                match=models.MatchValue(value=doc_id)
                            )
                        ]
                    ),
                    limit=100,
                    offset=offset,
                    with_payload=True,
                    with_vectors=False
                )
                
                for point in result:
                    results.append({
                        "id": str(point.id),
                        "chunk_index": point.payload.get("chunk_index", 0),
                        "total_chunks": point.payload.get("total_chunks", 0),
                        "text": point.payload.get("text_content", point.payload.get("text", "")),
                        "metadata": point.payload.get("metadata", {})
                    })
                
                if next_offset is None or len(result) == 0:
                    break
                offset = next_offset
            
            # Sort by chunk_index
            results.sort(key=lambda x: x.get("chunk_index", 0))
            return results
            
        except Exception as e:
            print(f"❌ Get chunks error: {e}")
            return []

    def delete_document(self, doc_id: str, workspace_id: int, collection_name: str = None) -> Dict:
        """Delete a specific document and all its chunks."""
        if collection_name is None:
            collection_name = app_config.collection_name
        
        try:
            result = self.qdrant_client.delete(
                collection_name=collection_name,
                points_selector=models.FilterSelector(
                    filter=models.Filter(
                        must=[
                            models.FieldCondition(
                                key="workspace_id",
                                match=models.MatchValue(value=workspace_id)
                            ),
                            models.FieldCondition(
                                key="doc_id",
                                match=models.MatchValue(value=doc_id)
                            )
                        ]
                    )
                )
            )
            print(f"🗑️ Deleted document {doc_id} for workspace {workspace_id}")
            return {"status": "success", "message": f"Document deleted successfully"}
        except Exception as e:
            print(f"❌ Delete document error: {e}")
            return {"status": "error", "message": str(e)}

    def browse_all_chunks(self, workspace_id: int, limit: int = 100, collection_name: str = None) -> List[Dict]:
        """Browse all chunks for a workspace with source info."""
        if collection_name is None:
            collection_name = app_config.collection_name
        
        try:
            result, _ = self.qdrant_client.scroll(
                collection_name=collection_name,
                scroll_filter=models.Filter(
                    must=[
                        models.FieldCondition(
                            key="workspace_id",
                            match=models.MatchValue(value=workspace_id)
                        )
                    ]
                ),
                limit=limit,
                with_payload=True,
                with_vectors=False
            )
            
            chunks = []
            for point in result:
                metadata = point.payload.get("metadata", {})
                chunks.append({
                    "id": str(point.id),
                    "doc_id": point.payload.get("doc_id", ""),
                    "chunk_index": point.payload.get("chunk_index", 0),
                    "total_chunks": point.payload.get("total_chunks", 0),
                    "text_preview": (point.payload.get("text_content", "")[:200] + "...") if len(point.payload.get("text_content", "")) > 200 else point.payload.get("text_content", ""),
                    "source": metadata.get("source", metadata.get("title", "Unknown")),
                    "source_type": metadata.get("type", "unknown")
                })
            
            return chunks
            
        except Exception as e:
            print(f"❌ Browse chunks error: {e}")
            return []


# Global RAG engine singleton
_rag_engine: Optional[RAGEngine] = None


def get_rag_engine() -> RAGEngine:
    """Get or create the RAG engine singleton."""
    global _rag_engine
    if _rag_engine is None:
        _rag_engine = RAGEngine()
    return _rag_engine
