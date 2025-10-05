import os
import asyncio
import aioboto3
from ingest import async_handler as ingest_async_handler  # ingest Lambda

# ------------------ ENV ------------------
REGION = os.environ.get("REGION", "us-east-1")
BEDROCK_KB_ID = os.environ.get("BEDROCK_KB_ID")

bedrock_client = aioboto3.client("bedrock-runtime", region_name=REGION)

async def start_kb_sync(kb_id: str):
    """
    Start Bedrock Knowledge Base sync after ingestion.
    """
    async with bedrock_client as client:
        await client.start_knowledge_base_sync(KnowledgeBaseId=kb_id)
        print(f"[INFO] Knowledge Base sync started for KB ID: {kb_id}")

async def async_main_handler(event, context):
    """
    1. Ingest document(s)
    2. Trigger KB sync automatically
    """
    # 1️⃣ Ingest document(s)
    ingest_response = await ingest_async_handler(event, context)

    # 2️⃣ Trigger KB sync after ingestion
    if BEDROCK_KB_ID:
        await start_kb_sync(BEDROCK_KB_ID)
    else:
        print("[WARN] BEDROCK_KB_ID not set. Skipping KB sync.")

    return ingest_response

def lambda_handler(event, context):
    return asyncio.run(async_main_handler(event, context))
