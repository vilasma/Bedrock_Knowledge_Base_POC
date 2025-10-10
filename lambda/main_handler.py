import os
import io
import json
import uuid
import time
import logging
import hashlib
import boto3
import psycopg2
import pdfplumber
import docx
from opensearchpy import OpenSearch, RequestsHttpConnection
from requests_aws4auth import AWS4Auth

# ---------------- Logger ----------------
logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)

# ---------------- Config ----------------
S3_BUCKET = os.environ['S3_BUCKET_NAME']
S3_INCOMING_PREFIX = "bedrock-poc-docs/"
REGION = os.environ.get('REGION', 'us-east-1')

DB_HOST = os.environ['DB_HOST']
DB_PORT = int(os.environ.get('DB_PORT', 5432))
DB_NAME = os.environ['DB_NAME']
DB_SECRET_ARN = os.environ['DB_SECRET_ARN']

KNOWLEDGE_BASE_ID = os.environ['KB_ID']
DATA_SOURCE_ID = os.environ['DATA_SOURCE_ID']

OPENSEARCH_ENDPOINT = os.environ['OPENSEARCH_ENDPOINT']
OPENSEARCH_INDEX = os.environ.get('OPENSEARCH_INDEX', 'kb-sync-data-index')

CHUNK_SIZE = int(os.environ.get('CHUNK_SIZE', 300))
VECTOR_DIM = 1536
TOP_K = int(os.environ.get('TOP_K', 5))
MAX_POLL_SECONDS = int(os.environ.get('MAX_POLL_SECONDS', 120))
POLL_INTERVAL = int(os.environ.get('POLL_INTERVAL', 5))

# ---------------- AWS Clients ----------------
boto_session = boto3.session.Session(region_name=REGION)
secrets_client = boto_session.client('secretsmanager')
s3 = boto_session.client('s3')
bedrock_agent = boto_session.client('bedrock-agent')
bedrock_runtime = boto_session.client('bedrock-runtime')

# ---------------- OpenSearch Client ----------------
credentials = boto3.Session().get_credentials()
awsauth = AWS4Auth(credentials.access_key, credentials.secret_key, credentials.token, REGION, 'es')
os_client = OpenSearch(
    hosts=[{'host': OPENSEARCH_ENDPOINT, 'port': 443}],
    http_auth=awsauth,
    use_ssl=True,
    verify_certs=True,
    connection_class=RequestsHttpConnection
)

# ---------------- DB Helpers ----------------
def get_db_credentials(secret_arn):
    secret = secrets_client.get_secret_value(SecretId=secret_arn)
    creds = json.loads(secret['SecretString'])
    return creds['username'], creds['password']

def get_db_conn():
    username, password = get_db_credentials(DB_SECRET_ARN)
    return psycopg2.connect(
        host=DB_HOST, port=DB_PORT, dbname=DB_NAME,
        user=username, password=password, connect_timeout=10
    )

# ---------------- Text Extraction ----------------
def extract_text_from_s3(bucket, key):
    obj = s3.get_object(Bucket=bucket, Key=key)
    raw = obj['Body'].read()
    ext = key.split('.')[-1].lower()
    if ext == 'pdf':
        with pdfplumber.open(io.BytesIO(raw)) as pdf:
            return "\n".join([p.extract_text() or "" for p in pdf.pages]).strip()
    elif ext == 'docx':
        doc = docx.Document(io.BytesIO(raw))
        return "\n".join([p.text for p in doc.paragraphs]).strip()
    else:
        return raw.decode('utf-8', errors='ignore').strip()

def split_into_chunks(text, chunk_size=CHUNK_SIZE):
    words = text.split()
    seen_texts = set()
    chunks = []
    for i in range(0, len(words), chunk_size):
        chunk = " ".join(words[i:i+chunk_size])
        if chunk not in seen_texts:
            chunks.append(chunk)
            seen_texts.add(chunk)
    return chunks

def compute_chunk_hash(text):
    return hashlib.md5(text.encode('utf-8')).hexdigest()

# ---------------- Embeddings ----------------
def get_chunk_embedding(text, retries=3, retry_seconds=2):
    for attempt in range(1, retries + 1):
        try:
            resp = bedrock_runtime.invoke_model(
                modelId="amazon.titan-embed-text-v1",
                body=json.dumps({"inputText": text}),
                contentType="application/json",
                accept="application/json"
            )
            emb = json.loads(resp['body'].read()).get('embedding', [])
            if len(emb) != VECTOR_DIM:
                raise ValueError(f"Embedding dimension mismatch: {len(emb)} != {VECTOR_DIM}")
            return [float(x) for x in emb]
        except Exception as e:
            logger.warning(f"Embedding attempt {attempt} failed: {e}")
            if attempt < retries:
                time.sleep(retry_seconds * attempt)
    return None

# ---------------- Aurora Inserts ----------------
def insert_document_and_chunks(s3_key, chunks):
    conn = get_db_conn()
    doc_id = str(uuid.uuid4())
    try:
        with conn:
            with conn.cursor() as cur:
                metadata_dict = {
                    "tenant_id": f"tenant-{uuid.uuid4().hex[:8]}",
                    "user_id": f"user-{uuid.uuid4().hex[:8]}",
                    "project_id": f"project-{uuid.uuid4().hex[:8]}",
                    "thread_id": f"thread-{uuid.uuid4().hex[:8]}"
                }
                # Insert document
                cur.execute("""
                    INSERT INTO documents (document_id, document_name, s3_key, status)
                    VALUES (%s,%s,%s,'PROCESSING')
                """, (doc_id, os.path.basename(s3_key), s3_key))
                # Insert metadata
                cur.execute("""
                    INSERT INTO metadata (metadata_id, document_id, tenant_id, user_id, project_id, thread_id, extra_metadata)
                    VALUES (gen_random_uuid(), %s, %s, %s, %s, %s, %s)
                """, (doc_id, metadata_dict['tenant_id'], metadata_dict['user_id'],
                      metadata_dict['project_id'], metadata_dict['thread_id'], json.dumps({})))
                # Insert chunks
                for idx, chunk_text in enumerate(chunks):
                    if not chunk_text.strip():
                        continue
                    chunk_hash = compute_chunk_hash(chunk_text)
                    cur.execute("SELECT 1 FROM document_chunks WHERE document_id=%s AND chunk_hash=%s", (doc_id, chunk_hash))
                    if cur.fetchone():
                        continue
                    emb = get_chunk_embedding(chunk_text)
                    if not emb:
                        continue
                    vec_str = "[" + ",".join(str(x) for x in emb) + "]"
                    cur.execute("""
                        INSERT INTO document_chunks
                        (chunk_id, document_id, chunk_index, chunk_text, embedding_vector, chunk_hash, status, metadata)
                        VALUES
                        (gen_random_uuid(), %s, %s, %s, %s::vector, %s, 'COMPLETED', %s)
                    """, (doc_id, idx, chunk_text, vec_str, chunk_hash, json.dumps(metadata_dict)))
        conn.commit()
        return doc_id
    finally:
        conn.close()

# ---------------- Fetch Chunks from Aurora ----------------
def fetch_chunks_from_aurora(doc_id):
    conn = get_db_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT chunk_id, document_id, chunk_index, chunk_text, embedding_vector, metadata
                FROM document_chunks
                WHERE document_id=%s AND status='COMPLETED'
            """, (doc_id,))
            return cur.fetchall()
    finally:
        conn.close()

# ---------------- Index Chunks to OpenSearch ----------------
def index_chunks_to_opensearch(chunks):
    """
    Index chunks to OpenSearch.
    Note: Index must be created via CloudFormation custom resource before this runs.
    """
    # Verify index exists
    if not os_client.indices.exists(OPENSEARCH_INDEX):
        logger.error(f"Index {OPENSEARCH_INDEX} does not exist. It should be created by CloudFormation.")
        raise Exception(f"OpenSearch index {OPENSEARCH_INDEX} not found")

    for chunk in chunks:
        doc = {
            "chunk_id": chunk[0],
            "document_id": chunk[1],
            "chunk_index": chunk[2],
            "chunk_text": chunk[3],
            "embedding_vector": json.loads(chunk[4]),
            "metadata": chunk[5]
        }
        os_client.index(index=OPENSEARCH_INDEX, id=chunk[0], body=doc)

# ---------------- Bedrock KB Ingestion ----------------
def trigger_bedrock_ingestion():
    try:
        resp = bedrock_agent.start_ingestion_job(
            knowledgeBaseId=KNOWLEDGE_BASE_ID,
            dataSourceId=DATA_SOURCE_ID
        )
        return resp.get("ingestionJob", {}).get("ingestionJobId")
    except Exception as e:
        logger.exception(f"Error triggering Bedrock ingestion: {e}")
        return None

def wait_for_bedrock_job(job_id):
    elapsed = 0
    while elapsed < MAX_POLL_SECONDS:
        try:
            resp = bedrock_agent.get_ingestion_job(
                ingestionJobId=job_id,
                knowledgeBaseId=KNOWLEDGE_BASE_ID,
                dataSourceId=DATA_SOURCE_ID
            )
            job_info = resp.get("ingestionJob", {})
            status = job_info.get("status")
            if status in ("COMPLETED", "FAILED"):
                return status, job_info.get("failureReason", "N/A")
        except Exception as e:
            return "FAILED", str(e)
        time.sleep(POLL_INTERVAL)
        elapsed += POLL_INTERVAL
    return "TIMEOUT", "Job polling exceeded max seconds"

# ---------------- Update Document & Chunk Status ----------------
def update_document_status(doc_id, status):
    conn = get_db_conn()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute("UPDATE documents SET status=%s, updated_at=NOW() WHERE document_id=%s", (status.lower(), doc_id))
                cur.execute("UPDATE document_chunks SET status=%s, updated_at=NOW() WHERE document_id=%s", (status.lower(), doc_id))
        conn.commit()
    finally:
        conn.close()

def query_top_k_chunks(query_text, k=TOP_K):
    """
    Computes embedding for the query text and returns top-k chunks from OpenSearch.
    """
    query_emb = get_chunk_embedding(query_text)
    if not query_emb:
        logger.warning("Failed to get query embedding")
        return []

    # OpenSearch kNN query
    query_body = {
        "size": k,
        "query": {
            "knn": {
                "embedding_vector": {
                    "vector": query_emb,
                    "k": k
                }
            }
        }
    }

    try:
        response = os_client.search(index=OPENSEARCH_INDEX, body=query_body)
        hits = response.get("hits", {}).get("hits", [])
        results = []
        for hit in hits:
            src = hit["_source"]
            results.append({
                "chunk_id": src["chunk_id"],
                "document_id": src["document_id"],
                "chunk_index": src["chunk_index"],
                "chunk_text": src["chunk_text"],
                "score": hit["_score"]
            })
        return results
    except Exception as e:
        logger.exception(f"OpenSearch top-k query failed: {e}")
        return []

# ---------------- Lambda Handler ----------------
def lambda_handler(event, context):
    results = []
    queries = event.get("queries", [])

    for record in event.get("Records", []):
        s3_key = record["s3"]["object"]["key"]
        if not s3_key.startswith(S3_INCOMING_PREFIX):
            continue
        try:
            text = extract_text_from_s3(S3_BUCKET, s3_key)
            if not text.strip():
                results.append({"file": s3_key, "status": "empty"})
                continue

            # Insert chunks in Aurora and OpenSearch
            chunks = split_into_chunks(text)
            doc_id = insert_document_and_chunks(s3_key, chunks)
            aurora_chunks = fetch_chunks_from_aurora(doc_id)
            index_chunks_to_opensearch(aurora_chunks)

            # Trigger Bedrock ingestion
            job_id = trigger_bedrock_ingestion()
            job_status, failure_reason = wait_for_bedrock_job(job_id) if job_id else ("FAILED", "No Job ID returned")

            # Update document & chunk status in Aurora
            update_document_status(doc_id, job_status)

            # Process top-k queries if provided
            top_k_results = {}
            for query in queries:
                query_text = query.get("text")
                query_id = query.get("query_id", str(uuid.uuid4()))
                if query_text:
                    top_k_results[query_id] = query_top_k_chunks(query_text, k=TOP_K)

            results.append({
                "file": s3_key,
                "document_id": doc_id,
                "last_ingestion_job_id": job_id,
                "status": job_status,
                "failure_reason": failure_reason,
                "top_k_results": top_k_results
            })
        except Exception as e:
            logger.exception(f"Failed processing {s3_key}")
            results.append({"file": s3_key, "status": "error", "reason": str(e)})
    return {"statusCode": 200, "body": json.dumps({"processed": results})}