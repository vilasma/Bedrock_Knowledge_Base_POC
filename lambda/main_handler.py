import os
import asyncio
from ingest import async_handler as ingest_async
from knowledgebase_handler import async_handler as kb_async

async def async_main_handler(event, context):
    # Step 1: Ingest document if provided
    ingest_response = None
    if "bucket" in event and "key" in event:
        ingest_response = await ingest_async(event, context)

    # Step 2: Query knowledge base
    kb_response = await kb_async(event, context)

    return {
        "statusCode": 200,
        "body": {
            "ingest": ingest_response,
            "knowledgebase": kb_response
        }
    }

def lambda_handler(event, context):
    return asyncio.run(async_main_handler(event, context))
