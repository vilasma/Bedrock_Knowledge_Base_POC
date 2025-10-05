import os
import logging
from ingest import lambda_handler as ingest_handler
from knowledgebase_handler import lambda_handler as kb_handler

logging.basicConfig(level=logging.INFO)

def lambda_handler(event, context):
    # Pass S3 bucket/key to environment for downstream modules
    if 'Records' in event and len(event['Records']) > 0:
        record = event['Records'][0]['s3']
        os.environ["CURRENT_S3_BUCKET"] = record['bucket']['name']
        os.environ["CURRENT_S3_KEY"] = record['object']['key']

    # Step 1: Ingest document
    try:
        ingest_response = ingest_handler(event, context)
        logging.info(f"Ingest response: {ingest_response}")
    except Exception as e:
        logging.error(f"Ingest failed: {e}")
        return {"statusCode": 500, "body": f"Ingest failed: {e}"}

    # Step 2: Query knowledge base (example)
    try:
        kb_response = kb_handler(event, context)
        logging.info(f"KB response: {kb_response}")
    except Exception as e:
        logging.error(f"KnowledgeBase query failed: {e}")
        return {"statusCode": 500, "body": f"KB query failed: {e}"}

    return {
        "statusCode": 200,
        "body": {
            "ingest": ingest_response,
            "knowledgebase": kb_response
        }
    }
