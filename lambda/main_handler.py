import os
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from ingest import lambda_handler as ingest_handler
from knowledgebase_handler import lambda_handler as kb_handler

logging.basicConfig(level=logging.INFO)
MAX_WORKERS = 5  # Adjust based on Lambda memory and number of files

def lambda_handler(event, context):
    """
    Optimized Lambda:
    1️⃣ Ingest multiple S3 documents in parallel
    2️⃣ Query multiple queries after ingestion
    """

    ingest_results = []
    kb_results = {}

    # ------------------ 1️⃣ Prepare S3 records ------------------
    s3_records = event.get('Records', [])
    if s3_records:
        logging.info(f"Found {len(s3_records)} S3 records to process")

    # ------------------ 2️⃣ Ingest documents in parallel ------------------
    if s3_records:
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
            future_to_record = {
                executor.submit(ingest_handler, {"Records": [record]}, context): record
                for record in s3_records
            }
            for future in as_completed(future_to_record):
                record = future_to_record[future]
                key = record['s3']['object']['key']
                try:
                    response = future.result()
                    logging.info(f"Ingested {key} successfully")
                    ingest_results.append(response)
                except Exception as e:
                    logging.error(f"Ingest failed for {key}: {e}")
                    ingest_results.append({"error": str(e), "file": key})

    # ------------------ 3️⃣ Execute KB queries ------------------
    queries = event.get('queries') or ([event.get('query')] if event.get('query') else [])
    if queries:
        try:
            query_event = {"queries": queries}
            kb_results = kb_handler(query_event, context)
            logging.info(f"Retrieved top chunks for queries: {queries}")
        except Exception as e:
            logging.error(f"KnowledgeBase query failed: {e}")
            kb_results = {"error": str(e)}

    # ------------------ 4️⃣ Return combined result ------------------
    return {
        "statusCode": 200,
        "body": {
            "ingest": ingest_results,
            "knowledgebase": kb_results
        }
    }
