"""
RAG Configuration Module
========================
Configuration for Qdrant, Google Embeddings, and RAG settings.
"""
import os
from pathlib import Path
from dotenv import load_dotenv

backend_env = Path(__file__).parent / ".env"
if backend_env.exists():
    load_dotenv(backend_env)
else:
    load_dotenv()


class QdrantConfig:
    """Qdrant database configuration."""
    # Support both QDRANT_* and legacy QUADRANT_* env var spellings
    endpoint: str = os.getenv("QDRANT_ENDPOINT") or os.getenv("QUADRANT_ENDPOINT", "")
    api_key: str = os.getenv("QDRANT_KEY") or os.getenv("QUADRANT_KEY", "")
    cluster_id: str = os.getenv("QDRANT_CLUSTER_ID") or os.getenv("QUADRANT_CLUSTER_ID", "")
    cluster_name: str = os.getenv("QDRANT_CLUSTER_NAME") or os.getenv("QUADRANT_CLUSTER_NAME", "")


class GoogleEmbeddingConfig:
    """Google Embedding configuration."""
    api_model_name: str = os.getenv("EMBEDDING_MODEL", "gemini-embedding-001")
    vertex_model_name: str = os.getenv("VERTEX_EMBEDDING_MODEL", "text-embedding-004")
    vector_size: int = int(os.getenv("EMBEDDING_VECTOR_SIZE", "768"))


class GeminiConfig:
    """Google Gemini AI configuration."""
    api_key: str = (
        os.getenv("GOOGLE_GENAI_API_KEY", "")
        or os.getenv("GEMINI_API_KEY", "")
        or os.getenv("GOOGLE_API_KEY", "")
    )
    model_name: str = (
        os.getenv("GEMINI_MODEL")
        or os.getenv("TEXT_MODEL")
        or "gemini-2.0-flash"
    )
    use_vertex: bool = os.getenv("GEMINI_USE_VERTEX", "").lower() in ("1", "true", "yes")


class AppConfig:
    """Application configuration."""
    collection_name: str = (
        os.getenv("RAG_COLLECTION_NAME")
        or os.getenv("QUADRANT_CLUSTER_NAME")
        or os.getenv("QDRANT_CLUSTER_NAME")
        or "sociochat"
    )
    chunk_size: int = int(os.getenv("RAG_CHUNK_SIZE", "500"))
    chunk_overlap: int = int(os.getenv("RAG_CHUNK_OVERLAP", "125"))
    top_k_results: int = int(os.getenv("RAG_TOP_K", "5"))
    score_threshold: float = float(os.getenv("RAG_SCORE_THRESHOLD", "0.05"))


qdrant_config = QdrantConfig()
google_embed_config = GoogleEmbeddingConfig()
gemini_config = GeminiConfig()
app_config = AppConfig()
