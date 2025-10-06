import os
import io
import json
import uuid
import time
import math
import ast
import logging
import warnings
from concurrent.futures import ThreadPoolExecutor, as_completed
import boto3
import psycopg2
import pdfplumber
import docx
from datetime import datetime
from botocore.exceptions import ClientError

# ---------------- Environment ----------------
DB_HOST = os.environ['DB_HOST']
DB_PORT = int(os.environ['DB_PORT'])
DB_NAME = os.environ['DB_NAME']
DB_USER = os.environ.get('DB_USER')
DB_PASSWORD = os.environ.get('DB_PASSWORD')
DB_SECRET_ARN = os.environ.get('DB_SECRET_ARN')
REGION = os.environ.get('REGION', 'us-east-1')
CHUNK_SIZE = int(os.environ.get('CHUNK_SIZE', 500))
BATCH_SIZE = int(os.environ.get('BATCH_SIZE', 20))
TOP_K = int(os.environ.get('TOP_K', 5))
MAX_WORKERS = int(os.environ.get('MAX_WORKERS', 5))
KB_ID = os.environ.get('KB_ID')
DATA_SOURCE_ID = os.environ.get('DataSourceId')

# ---------------- AWS Clients ----------------
s3_client = boto3.client('s3', region_name=REGION)
secrets_client = boto3.client('secretsmanager', region_name=REGION)
bedrock_client = boto3.client('bedrock-runtime', region_name=REGION)
bedrock_agent_client = boto3.client('bedrock-agent', region_name=REGION)

# ---------------- Logging & Warnings ----------------
logging.basicConfig(level=logging.INFO)
warnings.filterwarnings("ignore", category=UserWarning, message=".*Cannot set gray.*")

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
    try:
        obj = s3_client.get_object(Bucket=bucket, Key=key)
        raw_bytes = obj['Body'].read()
        ext = key.split('.')[-1].lower()
        if ext == 'pdf':
            with pdfplumber.open(io.BytesIO(raw_bytes)) as pdf:
                return "\n".join([page.extract_text() or "" for page in pdf.pages]).strip()
        elif ext == 'docx':
            doc = docx.Document(io.BytesIO(raw_bytes))
            return "\n".join([p.text for p in doc.paragraphs])
        else:
            return raw_bytes.decode('utf-8', errors='ignore')
    except Exception as e:
        raise RuntimeError(f"Failed to extract text from {key}: {e}")

# ---------------- Text Chunking ----------------
def chunk_text(text, chunk_size=CHUNK_SIZE):
    words = text.split()
    for i in range(0, len(words), chunk_size):
        yield i // chunk_size, " ".join(words[i:i + chunk_size])

# ---------------- Embeddings ----------------
def get_chunk_embedding(text):
    """Titan embedding for single chunk"""
    try:
        response = bedrock_client.invoke_model(
            modelId="amazon.titan-embed-text-v1",
            body=json.dumps({"inputText": text}),
            contentType="application/json",
            accept="application/json"
        )
        return json.loads(response['body'].read())['embedding']
    except Exception as e:
        logging.warning(f"Embedding failed: {e}")
        return []

def get_batch_embeddings(chunks):
    """Generate embeddings in parallel for chunks"""
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        return list(executor.map(lambda c: get_chunk_embedding(c['chunk_text']), chunks))

def get_query_embedding(query):
    """Titan embedding for single query"""
    try:
        response = bedrock_client.invoke_model(
            modelId="amazon.titan-embed-text-v1",
            body=json.dumps({"inputText": query}),
            contentType="application/json",
            accept="application/json"
        )
        return json.loads(response['body'].read())['embedding']
    except Exception as e:
        logging.warning(f"Embedding failed for query '{query}': {e}")
        return []

def get_batch_query_embeddings(queries):
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        return list(executor.map(get_query_embedding, queries))

# ---------------- Similarity ----------------
def parse_embedding(embedding):
    if isinstance(embedding, str):
        embedding = ast.literal_eval(embedding)
    return [float(x) for x in embedding]

def cosine_similarity(a, b):
    dot = sum(x*y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x*x for x in a))
    norm_b = math.sqrt(sum(y*y for y in b))
    return dot / (norm_a * norm_b) if norm_a and norm_b else 0.0

# ---------------- KB Sync ----------------
def start_kb_sync():
    if not KB_ID or not DATA_SOURCE_ID:
        logging.warning("KB_ID or DataSourceId not set, skipping KB sync")
        return
    try:
        resp = bedrock_agent_client.start_ingestion_job(
            knowledgeBaseId=KB_ID,
            dataSourceId=DATA_SOURCE_ID
        )
        logging.info(f"Started KB ingestion job: {resp['ingestionJob']['ingestionJobId']}")
        return resp
    except Exception as e:
        logging.warning(f"KB sync failed: {e}")

# ---------------- Document Ingestion ----------------
def ingest_s3_record(record):
    bucket = record['s3']['bucket']['name']
    key = record['s3']['object']['key']
    result = {"file": key}
    conn = get_db_connection()
    try:
        text = extract_text_from_s3(bucket, key)
        if not text.strip():
            result["status"] = "skipped"
            result["error"] = "Empty document"
            return result

        document_id = str(uuid.uuid4())
        metadata_id = str(uuid.uuid4())
        tenant_id = f"tenant_{uuid.uuid4().hex[:6]}"
        user_id = f"user_{uuid.uuid4().hex[:6]}"
        project_id = f"project_{uuid.uuid4().hex[:6]}"
        thread_id = f"thread_{uuid.uuid4().hex[:6]}"

        with conn:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO documents
                    (document_id, document_name, tenant_id, user_id, project_id, thread_id, status, created_at)
                    VALUES (%s, %s, %s, %s, %s, %s, 'in-progress', %s)
                """, (document_id, key, tenant_id, user_id, project_id, thread_id, datetime.utcnow()))
                cur.execute("""
                    INSERT INTO metadata
                    (metadata_id, document_id, tenant_id, user_id, project_id, thread_id, created_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                """, (metadata_id, document_id, tenant_id, user_id, project_id, thread_id, datetime.utcnow()))

        # Chunk and embed
        chunks = []
        for idx, chunk_text_content in chunk_text(text):
            chunks.append({
                "document_id": document_id,
                "document_name": key,
                "chunk_index": idx,
                "chunk_text": chunk_text_content,
                "metadata_id": metadata_id,
                "metadata": {
                    "metadata_id": metadata_id,
                    "tenant_id": tenant_id,
                    "user_id": user_id,
                    "project_id": project_id,
                    "thread_id": thread_id,
                    "chunk_index": idx
                }
            })

        embeddings = get_batch_embeddings(chunks)

        with conn:
            with conn.cursor() as cur:
                for chunk, embedding in zip(chunks, embeddings):
                    cur.execute("""
                        INSERT INTO document_chunks
                        (document_id, document_name, chunk_index, chunk_text, embedding_vector, metadata_id, metadata, status, created_at)
                        VALUES (%s, %s, %s, %s, %s::vector, %s, %s, %s, %s)
                    """, (
                        chunk['document_id'], chunk['document_name'], chunk['chunk_index'],
                        chunk['chunk_text'], embedding or [], chunk['metadata_id'],
                        json.dumps(chunk['metadata']), 'completed', datetime.utcnow()
                    ))

        with conn:
            with conn.cursor() as cur:
                cur.execute("""
                    UPDATE documents
                    SET status='completed', updated_at=NOW()
                    WHERE document_id = %s
                """, (document_id,))

        result["status"] = "success"
    except Exception as e:
        logging.error(f"Ingest failed for {key}: {e}")
        result["status"] = "failed"
        result["error"] = str(e)
    finally:
        conn.close()
    return result

def ingest_s3_records_parallel(records):
    results = []
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {executor.submit(ingest_s3_record, r): r for r in records}
        for future in as_completed(futures):
            results.append(future.result())
    return results

# ---------------- Query Top Chunks ----------------
def query_top_chunks_batch(query_embeddings, queries):
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
    for embedding, query_text in zip(query_embeddings, queries):
        top_results = []
        for chunk_text, chunk_vector, metadata in chunk_vectors:
            sim = cosine_similarity(embedding, chunk_vector)
            top_results.append({
                "chunk_text": chunk_text,
                "metadata": metadata,
                "similarity": sim
            })
        top_results.sort(key=lambda x: x['similarity'], reverse=True)
        results[query_text] = top_results[:TOP_K]
    return results

# ---------------- Main Lambda Handler ----------------
def lambda_handler(event, context):
    s3_records = event.get('Records', [])
    queries = event.get('queries') or ([event.get('query')] if event.get('query') else [])
    ingest_results = {}
    kb_results = {}

    # 1️⃣ Ingest S3 documents
    if s3_records:
        logging.info(f"Processing {len(s3_records)} S3 records...")
        ingest_results = ingest_s3_records_parallel(s3_records)

    # 2️⃣ Generate embeddings for queries and retrieve top chunks
    if queries:
        query_embeddings = get_batch_query_embeddings(queries)
        try:
            kb_results = query_top_chunks_batch(query_embeddings, queries)
        except Exception as e:
            logging.error(f"KB query failed: {e}")
            kb_results = {"error": str(e)}

    # 3️⃣ Trigger KB sync
    try:
        start_kb_sync()
    except Exception as e:
        logging.warning(f"KB ingestion failed: {e}")

    return {
        "statusCode": 200,
        "body": {
            "ingest": ingest_results,
            "knowledgebase": kb_results
        }
    }
