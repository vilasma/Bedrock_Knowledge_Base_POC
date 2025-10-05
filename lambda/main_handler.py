import os
import asyncio
import aioboto3
from ingest import async_handler as ingest_async_handler  # updated ingest Lambda

# ------------------ ENV ------------------
REGION = os.environ.get("REGION", "us-east-1")

# ---------- KB ID cache ----------
KB_ID_CACHE = None

async def get_kb_id(name="poc-bedrock-kb"):
    """Fetch Knowledge Base ID dynamically"""
    global KB_ID_CACHE
    if KB_ID_CACHE:
        return KB_ID_CACHE

    async with aioboto3.client("bedrock", region_name=REGION) as client:
        paginator = client.get_paginator("list_knowledge_bases")
        async for page in paginator.paginate():
            for kb in page.get("KnowledgeBases", []):
                if kb["Name"] == name:
                    KB_ID_CACHE = kb["KnowledgeBaseId"]
                    return KB_ID_CACHE
    raise Exception(f"Knowledge Base '{name}' not found")

async def start_kb_sync(kb_id: str):
    """
    Start Bedrock Knowledge Base sync after ingestion.
    """
    async with aioboto3.client("bedrock-runtime", region_name=REGION) as client:
        await client.start_knowledge_base_sync(KnowledgeBaseId=kb_id)
        print(f"[INFO] Knowledge Base sync started for KB ID: {kb_id}")

async def async_main_handler(event, context):
    """
    1. Call ingestion Lambda for document(s)
    2. Trigger KB sync automatically
    """
    ingest_response = await ingest_async_handler(event, context)

    # Dynamically fetch KB ID and start sync
    kb_id = await get_kb_id()
    await start_kb_sync(kb_id)

    return ingest_response

def lambda_handler(event, context):
    return asyncio.run(async_main_handler(event, context))
