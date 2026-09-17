"""
Migrate Qdrant Cloud → Local Qdrant with FastEmbed
===================================================

This script:
1. Connects to Qdrant Cloud (old: 768-dim Gemini vectors)
2. Reads ALL text chunks + metadata for every workspace
3. Re-embeds each chunk using FastEmbed locally (384-dim, no API)
4. Stores in local Qdrant (self-hosted)

Run inside the container:
    python migrate_qdrant_to_local.py

Safe to run multiple times — it recreates the local collection fresh each run.
"""

import os
import time
from dotenv import load_dotenv

load_dotenv()

# ── Step 1: Connect to BOTH Qdrant instances ────────────────────────────────

print("=" * 60)
print("STEP 1: Connecting to Qdrant Cloud (source) + Local (target)")
print("=" * 60)

from qdrant_client import QdrantClient
from qdrant_client.http import models

# Source: Qdrant Cloud (old vectors)
CLOUD_URL = os.getenv("QDRANT_ENDPOINT")
CLOUD_KEY = os.getenv("QDRANT_KEY")
if not CLOUD_URL or not CLOUD_KEY:
    print("❌ QDRANT_ENDPOINT and QDRANT_KEY must be set (source Cloud)")
    exit(1)

cloud_client = QdrantClient(url=CLOUD_URL, api_key=CLOUD_KEY, timeout=120)
print(f"  ✅ Connected to Cloud: {CLOUD_URL}")

# Target: Local Qdrant (new vectors)
LOCAL_URL = os.getenv("QDRANT_LOCAL_URL", "http://qdrant:6333")
local_client = QdrantClient(url=LOCAL_URL, timeout=120)
print(f"  ✅ Connected to Local: {LOCAL_URL}")

# Collection name
COLLECTION = os.getenv("RAG_COLLECTION_NAME") or os.getenv("QDRANT_CLUSTER_NAME") or "sociochat"
print(f"  📦 Collection: {COLLECTION}")

# ── Step 2: Initialize FastEmbed ─────────────────────────────────────────────

print()
print("=" * 60)
print("STEP 2: Initializing FastEmbed (local embedding model)")
print("=" * 60)

from fastembed import TextEmbedding

MODEL_NAME = os.getenv("FASTEMBED_MODEL", "BAAI/bge-small-en-v1.5")
VECTOR_SIZE = int(os.getenv("FASTEMBED_VECTOR_SIZE", "384"))

print(f"  Loading model: {MODEL_NAME} (dim={VECTOR_SIZE})")
embed_model = TextEmbedding(model_name=MODEL_NAME)
print("  ✅ FastEmbed ready")

# ── Step 3: Read all data from Cloud ─────────────────────────────────────────

print()
print("=" * 60)
print("STEP 3: Reading all chunks from Qdrant Cloud")
print("=" * 60)

if not cloud_client.collection_exists(COLLECTION):
    print(f"  ❌ Collection '{COLLECTION}' not found on Cloud. Nothing to migrate.")
    exit(0)

cloud_info = cloud_client.get_collection(COLLECTION)
total_points = cloud_info.points_count
print(f"  📊 Total points in Cloud: {total_points}")

# Scroll through all points
all_points = []
offset = None
batch_size = 100
while True:
    result, next_offset = cloud_client.scroll(
        collection_name=COLLECTION,
        limit=batch_size,
        offset=offset,
        with_payload=True,
        with_vectors=False,  # We don't need old vectors — we'll re-embed
    )
    all_points.extend(result)
    print(f"  📥 Read {len(all_points)}/{total_points} points...", end="\r")
    if next_offset is None:
        break
    offset = next_offset

print(f"  ✅ Read {len(all_points)} points from Cloud              ")

# Count workspaces
workspace_ids = set()
for p in all_points:
    ws = p.payload.get("workspace_id")
    if ws is not None:
        workspace_ids.add(ws)
print(f"  👥 Workspaces found: {len(workspace_ids)} — {sorted(workspace_ids)}")

# ── Step 4: Create fresh collection in Local Qdrant ──────────────────────────

print()
print("=" * 60)
print("STEP 4: Creating collection in Local Qdrant")
print("=" * 60)

# Delete if exists (fresh start)
if local_client.collection_exists(COLLECTION):
    local_client.delete_collection(COLLECTION)
    print(f"  🗑️  Deleted existing local collection '{COLLECTION}'")

local_client.create_collection(
    collection_name=COLLECTION,
    vectors_config=models.VectorParams(
        size=VECTOR_SIZE,
        distance=models.Distance.COSINE,
    ),
)
print(f"  ✅ Created collection '{COLLECTION}' (dim={VECTOR_SIZE})")

# Create indexes (same as rag_engine.create_collection)
local_client.create_payload_index(
    collection_name=COLLECTION,
    field_name="workspace_id",
    field_schema=models.PayloadSchemaType.INTEGER,
)
local_client.create_payload_index(
    collection_name=COLLECTION,
    field_name="doc_id",
    field_schema=models.PayloadSchemaType.KEYWORD,
)
local_client.create_payload_index(
    collection_name=COLLECTION,
    field_name="metadata.source",
    field_schema=models.PayloadSchemaType.KEYWORD,
)
print("  ✅ Indexes created (workspace_id, doc_id, metadata.source)")

# ── Step 5: Re-embed and store ───────────────────────────────────────────────

print()
print("=" * 60)
print("STEP 5: Re-embedding all chunks with FastEmbed → Local Qdrant")
print("=" * 60)

start_time = time.time()
success = 0
skipped = 0
failed = 0
batch = []
BATCH_SIZE = 50

for i, point in enumerate(all_points):
    # Get the text content from the payload
    text = (
        point.payload.get("text_content")
        or point.payload.get("text")
        or ""
    ).strip()

    if not text:
        skipped += 1
        continue

    try:
        # Generate new embedding locally
        embeddings = list(embed_model.embed([text]))
        vector = embeddings[0].tolist()

        # Keep the same point ID and payload
        batch.append(models.PointStruct(
            id=point.id,
            vector=vector,
            payload=point.payload,
        ))

        # Upsert in batches
        if len(batch) >= BATCH_SIZE:
            local_client.upsert(
                collection_name=COLLECTION,
                points=batch,
                wait=True,
            )
            success += len(batch)
            batch = []

        elapsed = time.time() - start_time
        rate = (i + 1) / elapsed if elapsed > 0 else 0
        print(f"  ⚡ {i+1}/{len(all_points)} | ✅ {success} | ⏭️ {skipped} | ❌ {failed} | {rate:.1f} pts/s", end="\r")

    except Exception as e:
        failed += 1
        print(f"\n  ❌ Failed point {point.id}: {e}")

# Flush remaining batch
if batch:
    local_client.upsert(
        collection_name=COLLECTION,
        points=batch,
        wait=True,
    )
    success += len(batch)

elapsed = time.time() - start_time

# ── Step 6: Verify ───────────────────────────────────────────────────────────

print()
print()
print("=" * 60)
print("STEP 6: Verification")
print("=" * 60)

local_info = local_client.get_collection(COLLECTION)
print(f"  Cloud points:  {total_points}")
print(f"  Local points:  {local_info.points_count}")
print(f"  Migrated:      {success}")
print(f"  Skipped:       {skipped} (no text content)")
print(f"  Failed:        {failed}")
print(f"  Time:          {elapsed:.1f}s")
print(f"  Vector dim:    {VECTOR_SIZE} (FastEmbed {MODEL_NAME})")

if local_info.points_count == total_points - skipped:
    print()
    print("  ✅ MIGRATION COMPLETE — all points transferred!")
    print()
    print("  Next steps:")
    print("  1. Test RAG search on a workspace")
    print("  2. Remove QDRANT_ENDPOINT and QDRANT_KEY from .env")
    print("  3. Cancel Qdrant Cloud subscription")
else:
    print()
    print(f"  ⚠️  Point count mismatch: expected {total_points - skipped}, got {local_info.points_count}")
    print("  Check failed points above and re-run if needed.")
