
import os
import io
import json
import uuid
import math
import time
import logging
import random
import warnings
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed

import boto3
import psycopg2
import pdfplumber
import docx
from botocore.exceptions import ClientError

# ---------------- Environment & Constants ----------------
S3_BUCKET = os.environ['S3_BUCKET_NAME']
S3_INCOMING_PREFIX = os.environ.get('S3_INCOMING_PREFIX', 'bedrock-poc-docs/')
S3_CHUNKS_PREFIX = os.environ.get('S3_CHUNKS_PREFIX', 'chunks/')
REGION = os.environ.get('REGION', 'us-east-1')

DB_HOST = os.environ['DB_HOST']
DB_PORT = int(os.environ.get('DB_PORT', 5432))
DB_NAME = os.environ['DB_NAME']
DB_USER = os.environ.get('DB_USER')
DB_PASSWORD = os.environ.get('DB_PASSWORD')
DB_SECRET_ARN = os.environ.get('DB_SECRET_ARN')

KB_ID = os.environ.get('KB_ID')
DATA_SOURCE_ID = os.environ.get('DATA_SOURCE_ID')

CHUNK_SIZE = int(os.environ.get('CHUNK_SIZE', 500))
BATCH_SIZE = int(os.environ.get('BATCH_SIZE', 20))
MAX_WORKERS = int(os.environ.get('MAX_WORKERS', 4))
MAX_INGEST_RETRIES = int(os.environ.get('MAX_INGEST_RETRIES', 8))
INGEST_RETRY_BASE_SECONDS = int(os.environ.get('INGEST_RETRY_BASE_SECONDS', 5))
EMBED_RETRIES = int(os.environ.get('EMBED_RETRIES', 3))
EMBED_RETRY_SECONDS = int(os.environ.get('EMBED_RETRY_SECONDS', 2))
TOP_K = int(os.environ.get('TOP_K', 5))


# ---------------- Logging & warnings ----------------
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)
warnings.filterwarnings("ignore", category=UserWarning, message=".*FontBBox.*")

# ---------------- AWS Clients ----------------
s3 = boto3.client('s3', region_name=REGION)
secrets_client = boto3.client('secretsmanager', region_name=REGION)
bedrock_runtime = boto3.client('bedrock-runtime', region_name=REGION)
bedrock_agent = boto3.client('bedrock-agent', region_name=REGION)

# ---------------- DB helpers ----------------
def get_db_credentials(secret_arn):
    secret = secrets_client.get_secret_value(SecretId=secret_arn)
    creds = json.loads(secret['SecretString'])
    return creds.get('username'), creds.get('password')

def get_db_conn():
    if DB_SECRET_ARN:
        username, password = get_db_credentials(DB_SECRET_ARN)
    else:
        username, password = DB_USER, DB_PASSWORD
    return psycopg2.connect(
        host=DB_HOST,
        port=DB_PORT,
        dbname=DB_NAME,
        user=username,
        password=password,
        connect_timeout=10
    )

# ---------------- text extraction ----------------
def extract_text_from_s3(bucket, key):
    obj = s3.get_object(Bucket=bucket, Key=key)
    raw = obj['Body'].read()
    ext = key.split('.')[-1].lower()
    if ext == 'pdf':
        with pdfplumber.open(io.BytesIO(raw)) as pdf:
            return "\n".join([page.extract_text() or "" for page in pdf.pages]).strip()
    elif ext == 'docx':
        doc = docx.Document(io.BytesIO(raw))
        return "\n".join([p.text for p in doc.paragraphs]).strip()
    else:
        return raw.decode('utf-8', errors='ignore').strip()

# ---------------- chunking ----------------
def split_into_chunks(text, chunk_size=CHUNK_SIZE):
    words = text.split()
    chunks = []
    for i in range(0, len(words), chunk_size):
        chunk = " ".join(words[i:i + chunk_size])
        chunks.append(chunk)
    return chunks

# ---------------- embeddings ----------------
def get_chunk_embedding(text, retries=EMBED_RETRIES):
    for attempt in range(1, retries+1):
        try:
            resp = bedrock_runtime.invoke_model(
                modelId="amazon.titan-embed-text-v1",
                body=json.dumps({"inputText": text}),
                contentType="application/json",
                accept="application/json"
            )
            return json.loads(resp['body'].read())['embedding']
        except Exception as e:
            logger.warning(f"Embedding attempt {attempt} failed: {e}")
            if attempt < retries:
                time.sleep(EMBED_RETRY_SECONDS*attempt)
            else:
                logger.error("Embedding failed; returning empty embedding")
                return []

# ---------------- S3 export ----------------
def export_chunks_to_s3(document_id, chunks):
    base_prefix = S3_CHUNKS_PREFIX.rstrip('/') + f'/{document_id}/'
    for idx, chunk in enumerate(chunks):
        key = f"{base_prefix}chunk_{idx}.json"
        s3.put_object(
            Bucket=S3_BUCKET,
            Key=key,
            Body=json.dumps({
                "document_id": document_id,
                "chunk_id": idx,
                "text": chunk['chunk_text'],
                "metadata": chunk.get('metadata', {})
            })
        )

# ---------------- Bedrock ingestion ----------------
def list_active_ingestion_jobs(kb_id, data_source_id):
    try:
        resp = bedrock_agent.list_ingestion_jobs(
            knowledgeBaseId=kb_id,
            dataSourceId=data_source_id,
            maxResults=10
        )
        return resp.get('ingestionJobSummaries', [])
    except Exception as e:
        logger.warning(f"list_ingestion_jobs error: {e}")
        return []

def is_ingestion_running(kb_id, data_source_id):
    jobs = list_active_ingestion_jobs(kb_id, data_source_id)
    for j in jobs:
        if j.get('status', '').upper() not in ('COMPLETED','FAILED','CANCELLED','STOPPED'):
            return True
    return False

def wait_until_ingestion_free(kb_id, data_source_id, timeout=900, poll_interval=10):
    start = time.time()
    while is_ingestion_running(kb_id, data_source_id):
        if time.time()-start>timeout:
            return False
        time.sleep(poll_interval)
    return True

def start_ingestion_with_backoff(kb_id, data_source_id, max_retries=MAX_INGEST_RETRIES):
    for attempt in range(max_retries):
        try:
            if not wait_until_ingestion_free(kb_id, data_source_id, timeout=60, poll_interval=5):
                raise Exception("Ingestion still running; retrying...")
            resp = bedrock_agent.start_ingestion_job(
                knowledgeBaseId=kb_id,
                dataSourceId=data_source_id
            )
            job_id = resp.get('ingestionJob', {}).get('ingestionJobId')
            logger.info(f"Started ingestion job {job_id} (attempt {attempt+1})")
            return job_id
        except bedrock_agent.exceptions.ConflictException as e:
            backoff = INGEST_RETRY_BASE_SECONDS*(2**attempt)+random.uniform(0,3)
            time.sleep(backoff)
        except Exception as e:
            backoff = INGEST_RETRY_BASE_SECONDS*(2**attempt)+random.uniform(0,2)
            time.sleep(backoff)
    raise RuntimeError("Failed to start ingestion after retries")

# ---------------- DB helpers ----------------
def upsert_document_and_chunks(document_id, document_name, chunks, tenant_id, user_id, project_id, thread_id):
    conn = get_db_conn()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO documents (document_id, tenant_id, user_id, project_id, document_name, thread_id, status, created_at, updated_at)
                    VALUES (%s,%s,%s,%s,%s,%s,'in-progress',NOW(), NOW())
                """, (document_id, tenant_id, user_id, project_id, document_name, thread_id))

                records=[]
                for c in chunks:
                    records.append((
                        document_id,
                        document_name,
                        c['chunk_index'],
                        c['chunk_text'],
                        c.get('embedding', []),
                        json.dumps(c.get('metadata', {})),
                        'completed',
                        datetime.utcnow()
                    ))

                sql="""
                INSERT INTO document_chunks (document_id, document_name, chunk_index, chunk_text, embedding_vector, metadata, status, created_at)
                VALUES (%s,%s,%s,%s,%s::vector,%s,%s,%s)
                """
                for i in range(0,len(records),BATCH_SIZE):
                    cur.executemany(sql,records[i:i+BATCH_SIZE])
    finally:
        conn.close()

# ---------------- retrieval helpers ----------------
def parse_embedding(embedding):
    if isinstance(embedding,str):
        import ast
        embedding = ast.literal_eval(embedding)
    return [float(x) for x in embedding]

def cosine_similarity(a,b):
    dot = sum(x*y for x,y in zip(a,b))
    norm_a = math.sqrt(sum(x*x for x in a))
    norm_b = math.sqrt(sum(y*y for y in b))
    return dot/(norm_a*norm_b) if norm_a and norm_b else 0.0

def query_top_chunks(query_texts, top_k=TOP_K):
    conn = get_db_conn()
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT chunk_text, embedding_vector, metadata
            FROM document_chunks
            WHERE status='completed'
        """)
        rows = cur.fetchall()
        cur.close()
    finally:
        conn.close()

    chunk_vectors=[(row[0],parse_embedding(row[1]),row[2]) for row in rows]
    results={}
    for query in query_texts:
        query_embed = get_chunk_embedding(query)
        top=[]
        for text,vec,meta in chunk_vectors:
            sim = cosine_similarity(query_embed,vec)
            top.append({"chunk_text":text,"metadata":meta,"similarity":sim})
        top.sort(key=lambda x:x['similarity'],reverse=True)
        results[query]=top[:top_k]
    return results

# ---------------- single file processing ----------------
def process_s3_file(record):
    bucket = record['s3']['bucket']['name']
    key = record['s3']['object']['key']

    if not key.startswith(S3_INCOMING_PREFIX):
        return {"file": key, "status": "skipped", "reason": "not-incoming-prefix"}

    document_id = str(uuid.uuid4())

    try:
        # 1️⃣ Extract S3 object metadata for tenant/user/project
        s3_client = boto3.client('s3')
        head_obj = s3_client.head_object(Bucket=bucket, Key=key)
        metadata = head_obj.get('Metadata', {})

        tenant_id = f"tenant-{uuid.uuid4().hex[:8]}"
        user_id = f"user-{uuid.uuid4().hex[:8]}"
        project_id = f"project-{uuid.uuid4().hex[:8]}"
        thread_id = f"thread-{uuid.uuid4().hex[:8]}"

        # 2️⃣ Extract text
        text = extract_text_from_s3(bucket, key)
        if not text.strip():
            return {"file": key, "status": "skipped", "reason": "empty"}

        # 3️⃣ Prepare metadata
        metadata_id = str(uuid.uuid4())
        meta_common = {
            "metadata_id": metadata_id,
            "source_key": key,
            "tenant_id": tenant_id,
            "user_id": user_id,
            "project_id": project_id,
            "thread_id": thread_id
        }

        # 4️⃣ Chunk text
        chunks = []
        for idx, chunk in enumerate(split_into_chunks(text)):
            chunks.append({
                "chunk_index": idx,
                "chunk_text": chunk,
                "metadata": meta_common
            })

        # 5️⃣ Generate embeddings
        for c in chunks:
            c['embedding'] = get_chunk_embedding(c['chunk_text'])

        # 6️⃣ Store in Aurora PostgreSQL (pass IDs)
        upsert_document_and_chunks(document_id, key, chunks,
                                   tenant_id, user_id, project_id, thread_id)

        # 7️⃣ Export chunks and trigger ingestion
        export_chunks_to_s3(document_id, chunks)
        job_id = start_ingestion_with_backoff(KB_ID, DATA_SOURCE_ID)

        return {
            "file": key,
            "status": "success",
            "document_id": document_id,
            "tenant_id": tenant_id,
            "user_id": user_id,
            "ingestion_job_id": job_id
        }

    except Exception as e:
        logger.exception(f"Failed processing {key}: {e}")
        return {"file": key, "status": "error", "error": str(e)}

# ---------------- parallel S3 handler ----------------
def handle_records_parallel(s3_records):
    results=[]
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        futures={ex.submit(process_s3_file,r):r for r in s3_records}
        for f in as_completed(futures):
            try:
                results.append(f.result())
            except Exception as e:
                r=futures[f]
                key=r.get('s3',{}).get('object',{}).get('key','unknown')
                results.append({"file":key,"status":"failed","error":str(e)})
    return results

# ---------------- Lambda handler ----------------
def lambda_handler(event, context):
    records = event.get('Records',[])
    s3_results=[]
    if records:
        s3_results = handle_records_parallel(records)

    queries = event.get('queries') or ([event.get('query')] if event.get('query') else [])
    kb_results={}
    if queries:
        try:
            kb_results = query_top_chunks(queries)
        except Exception as e:
            logger.error(f"KB retrieval failed: {e}")
            kb_results={"error":str(e)}

    return {
        "statusCode":200,
        "body":{
            "ingest":s3_results,
            "knowledgebase":kb_results
        }
    }
