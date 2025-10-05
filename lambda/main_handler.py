import os
import json
import logging
import boto3

# Import your existing modules
from ingest import lambda_handler as ingest_handler
from knowledgebase_handler import lambda_handler as kb_handler

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# Lambda entry point
def lambda_handler(event, context):
    logger.info("Received S3 event: %s", json.dumps(event))

    # You can loop through records if multiple S3 objects are uploaded
    responses = []

    for record in event.get("Records", []):
        try:
            bucket = record["s3"]["bucket"]["name"]
            key = record["s3"]["object"]["key"]
            logger.info(f"Processing S3 object: s3://{bucket}/{key}")

            # Inject S3 info into environment for each module if needed
            os.environ["CURRENT_S3_BUCKET"] = bucket
            os.environ["CURRENT_S3_KEY"] = key

            # Call ingest module
            ingest_response = ingest_handler(event, context)
            logger.info(f"Ingest response: {ingest_response}")

            # Call knowledge base handler module
            kb_response = kb_handler(event, context)
            logger.info(f"Knowledge Base response: {kb_response}")

            responses.append({
                "s3_key": key,
                "ingest_response": ingest_response,
                "kb_response": kb_response
            })

        except Exception as e:
            logger.error(f"Error processing {record}: {e}", exc_info=True)
            responses.append({
                "s3_key": record.get("s3", {}).get("object", {}).get("key", "unknown"),
                "error": str(e)
            })

    return {
        "statusCode": 200,
        "body": json.dumps(responses)
    }
