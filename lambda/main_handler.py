import os
import io
import json
import uuid
import math
import time
import logging
import warnings
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
import boto3
import psycopg2
import pdfplumber
import docx
from botocore.exceptions import ClientError

# ---------------- Environment ----------------
DB_HOST = os.environ['DB_HOST']
DB_PORT = int(os.environ['DB_PORT'])
DB_NAME = os.environ['DB_NAME']
DB_USER = os.environ.get('DB_USER')
DB_PASSWORD = os.environ.get('DB_PASSWORD')
DB_SECRET_ARN = os.environ.get('DB_SECRET_ARN')
REGION = os.environ.get('REGION', 'us-east-1')
CHUNK_SIZE = 500, BATCH_SIZE = 20
TOP_K = int(os.environ.get('TOP_K', 5))
KB_ID = os.environ.get('KB_ID')
DATA_SOURCE_ID = os.environ.get('DataSourceId')
MAX_WORKERS = 5, MAX_RETRIES = 3, RETRY_DELAY = 5  # seconds

# ---------------- Logging & Warnings ----------------
logging.basicConfig(level=logging.INFO)
# Filter pdfplumber warnings
warnings.filterwarnings("ignore", category=UserWarning, message=".*Cannot set gray.*")
warnings.filterwarnings("ignore", category=UserWarning, message=".*FontBBox.*")

# ---------------- AWS Clients ----------------
s3_client = boto3.client('s3', region_name=REGION)
secrets_client = boto3.client('secretsmanager', region_name=REGION)
bedrock_client = boto3.client('bedrock-runtime', region_name=REGION)
bedrock_agent_client = boto3.client('bedrock-agent', region_name=REGION)

# ---------------- DB Helpers ----------------
def get_db_credentials(secret_arn):
    secret = secrets_client.get_secret_value(SecretId=secret_arn)
    creds = json.loads(secret['SecretString'])
    return creds['username'], creds['password']

def get_db_connection():
    if DB_SECRET_ARN:
        username, password = get_db_credentials(DB_SECRET_ARN)
    else:
        username = DB_USER
        password = DB_PASSWORD
    return psycopg2.connect(
        host=DB_HOST,
        port=DB_PORT,
        dbname=DB_NAME,
        user=username,
        password=password
    )

# ---------------- Text Extraction ----------------
def extract_text_from_s3(bucket, key):
    s3_obj = s3_client.get_object(Bucket=bucket, Key=key)
    raw_bytes = s3_obj['Body'].read()
    ext = key.split('.')[-1].lower()
    if ext == 'pdf':
        with pdfplumber.open(io.BytesIO(raw_bytes)) as pdf:
            return "\n".join([page.extract_text() or "" for page in pdf.pages]).strip()
    elif ext == 'docx':
        doc = docx.Document(io.BytesIO(raw_bytes))
        return "\n".join([para.text for para in doc.paragraphs])
    else:
        return raw_bytes.decode('utf-8', errors='ignore')

# ---------------- Text Chunking ----------------
def chunk_text(text, chunk_size=CHUNK_SIZE):
    words = text.split()
    for i in range(0, len(words), chunk_size):
        yield i // chunk_size, " ".join(words[i:i + chunk_size])

# ---------------- Embeddings ----------------
def get_chunk_embedding(chunk_text, retries=MAX_RETRIES):
    for attempt in range(retries):
        try:
            response = bedrock_client.invoke_model(
                modelId="amazon.titan-embed-text-v1",
                body=json.dumps({"inputText": chunk_text}),
                contentType="application/json",
                accept="application/json"
            )
            return json.loads(response['body'].read())['embedding']
        except Exception as e:
            logging.warning(f"Embedding attempt {attempt+1} failed: {e}")
            time.sleep(RETRY_DELAY)
    logging.error("Max retries reached for embedding.")
    return []

def get_batch_embeddings(chunks):
    embeddings = []
    for chunk in chunks:
        embedding = get_chunk_embedding(chunk['chunk_text'])
        embeddings.append(embedding)
    return embeddings

# ---------------- KB Sync ----------------
def start_kb_sync():
    if not KB_ID or not DATA_SOURCE_ID:
        logging.warning("KB_ID or DataSourceId not set, skipping KB sync")
        return
    for attempt in range(MAX_RETRIES):
        try:
            resp = bedrock_agent_client.start_ingestion_job(
                knowledgeBaseId=KB_ID,
                dataSourceId=DATA_SOURCE_ID
            )
            logging.info(f"Started KB ingestion job: {resp['ingestionJob']['ingestionJobId']}")
            return resp
        except ClientError as e:
            if e.response['Error']['Code'] == 'ConflictException':
                logging.info("Ingestion already running. Retrying in 60s...")
                time.sleep(60)
            else:
                logging.error(f"KB sync failed: {e}")
                time.sleep(RETRY_DELAY)
    logging.error("Max retries reached for KB sync.")

# ---------------- Cosine Similarity ----------------
def parse_embedding(embedding):
    if isinstance(embedding, str):
        import ast
        embedding = ast.literal_eval(embedding)
    return [float(x) for x in embedding]

def cosine_similarity(a, b):
    dot = sum(x*y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x*x for x in a))
    norm_b = math.sqrt(sum(y*y for y in b))
    return dot / (norm_a * norm_b) if norm_a and norm_b else 0.0

def query_top_chunks(query_texts):
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("""
        SELECT chunk_text, embedding_vector, metadata
        FROM document_chunks
        WHERE status='completed'
    """)
    rows = cur.fetchall()
    cur.close()
    conn.close()

    chunk_vectors = [(row[0], parse_embedding(row[1]), row[2]) for row in rows]
    results = {}

    for query_text in query_texts:
        query_embedding = get_chunk_embedding(query_text)
        top_results = []
        for chunk_text, chunk_vector, metadata in chunk_vectors:
            sim = cosine_similarity(query_embedding, chunk_vector)
            top_results.append({"chunk_text": chunk_text, "metadata": metadata, "similarity": sim})
        top_results.sort(key=lambda x: x['similarity'], reverse=True)
        results[query_text] = top_results[:TOP_K]

    return results

# ---------------- Parallel Ingest ----------------
def ingest_s3_records_parallel(s3_records):
    results = []

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        future_to_record = {
            executor.submit(ingest_single_file_with_retry_logging, record): record
            for record in s3_records
        }
        for future in as_completed(future_to_record):
            record = future_to_record[future]
            key = record['s3']['object']['key']
            try:
                res = future.result()
                results.append(res)
            except Exception as e:
                logging.error(f"Ingest failed for {key}: {e}")
                results.append({"file": key, "status": "failed", "error": str(e)})
    return results

# ---------------- Single File Ingest with Stable UUID ----------------
def ingest_single_file_with_retry_logging(record):
    key = record['s3']['object']['key']
    # Generate document_id once per file
    document_id = str(uuid.uuid4())
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            logging.info(f"Attempt {attempt} to ingest file: {key}")
            result = ingest_single_file(record, document_id=document_id)
            logging.info(f"File {key} ingested successfully on attempt {attempt}")
            result['attempt'] = attempt
            return result
        except Exception as e:
            logging.warning(f"Attempt {attempt} failed for {key}: {e}")
            time.sleep(RETRY_DELAY)
    logging.error(f"Max retries reached for file {key}")
    return {"file": key, "status": "failed", "error": "Max retries reached", "attempt": MAX_RETRIES}

# ---------------- Single File Ingest ----------------
def ingest_single_file(record, document_id=None):
    bucket = record['s3']['bucket']['name']
    key = record['s3']['object']['key']
    conn = get_db_connection()
    file_result = {"file": key}

    if not document_id:
        document_id = str(uuid.uuid4())

    try:
        text = extract_text_from_s3(bucket, key)
        if not text.strip():
            return {"file": key, "status": "skipped", "error": "Empty document"}

        metadata_id = str(uuid.uuid4())
        tenant_id = f"tenant_{uuid.uuid4().hex[:6]}"
        user_id = f"user_{uuid.uuid4().hex[:6]}"
        project_id = f"project_{uuid.uuid4().hex[:6]}"
        thread_id = f"thread_{uuid.uuid4().hex[:6]}"

        with conn:
            with conn.cursor() as cur:
                # Insert document safely
                cur.execute("""
                    INSERT INTO documents
                    (document_id, document_name, tenant_id, user_id, project_id, thread_id, status, created_at)
                    VALUES (%s, %s, %s, %s, %s, %s, 'in-progress', %s)
                    ON CONFLICT (document_id) DO NOTHING
                """, (document_id, key, tenant_id, user_id, project_id, thread_id, datetime.utcnow()))

                # Insert metadata safely
                cur.execute("""
                    INSERT INTO metadata
                    (metadata_id, document_id, tenant_id, user_id, project_id, thread_id, created_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (metadata_id) DO NOTHING
                """, (metadata_id, document_id, tenant_id, user_id, project_id, thread_id, datetime.utcnow()))

                chunks = []
                for idx, chunk_text_content in chunk_text(text):
                    chunk_meta = {
                        "metadata_id": metadata_id,
                        "tenant_id": tenant_id,
                        "user_id": user_id,
                        "project_id": project_id,
                        "thread_id": thread_id,
                        "chunk_index": idx
                    }
                    chunks.append({
                        "document_id": document_id,
                        "document_name": key,
                        "chunk_index": idx,
                        "chunk_text": chunk_text_content,
                        "metadata_id": metadata_id,
                        "metadata": chunk_meta
                    })

                for i in range(0, len(chunks), BATCH_SIZE):
                    batch = chunks[i:i+BATCH_SIZE]
                    embeddings = get_batch_embeddings(batch)
                    chunk_records = [
                        (
                            c["document_id"],
                            c["document_name"],
                            c["chunk_index"],
                            c["chunk_text"],
                            e if e else [],
                            c["metadata_id"],
                            json.dumps(c["metadata"]),
                            'completed',
                            datetime.utcnow()
                        ) for c, e in zip(batch, embeddings)
                    ]
                    cur.executemany("""
                        INSERT INTO document_chunks
                        (document_id, document_name, chunk_index, chunk_text, embedding_vector, metadata_id, metadata, status, created_at)
                        VALUES (%s, %s, %s, %s, %s::vector, %s, %s, %s, %s)
                        ON CONFLICT (document_id, chunk_index) DO NOTHING
                    """, chunk_records)

                cur.execute("""
                    UPDATE documents
                    SET status='completed', updated_at=NOW()
                    WHERE document_id = %s::uuid
                """, (document_id,))

        file_result["status"] = "success"

    finally:
        conn.close()

    return file_result

# ---------------- Main Lambda ----------------
def lambda_handler(event, context):
    ingest_results = []
    kb_results = {}

    s3_records = event.get('Records', [])
    if s3_records:
        logging.info(f"Found {len(s3_records)} S3 files to ingest")
        ingest_results = ingest_s3_records_parallel(s3_records)

    queries = event.get('queries') or ([event.get('query')] if event.get('query') else [])
    if queries:
        try:
            kb_results = query_top_chunks(queries)
        except Exception as e:
            logging.error(f"KB query failed: {e}")
            kb_results = {"error": str(e)}

    try:
        start_kb_sync()
    except Exception as e:
        logging.warning(f"KB sync failed: {e}")

    return {
        "statusCode": 200,
        "body": {
            "ingest": ingest_results,
            "knowledgebase": kb_results
        }
    }
