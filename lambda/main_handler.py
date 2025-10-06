import os
import io
import json
import uuid
import math
import time
import warnings
import boto3
import psycopg2
import pdfplumber
import docx
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed
from botocore.exceptions import ClientError

# ------------------ Environment Variables ------------------
DB_HOST = os.environ['DB_HOST']
DB_PORT = int(os.environ['DB_PORT'])
DB_NAME = os.environ['DB_NAME']
DB_SECRET_ARN = os.environ.get('DB_SECRET_ARN')
REGION = os.environ.get('REGION', 'us-east-1')
CHUNK_SIZE = int(os.environ.get('CHUNK_SIZE', 500))
BATCH_SIZE = int(os.environ.get('BATCH_SIZE', 20))
KB_ID = os.environ.get('KB_ID')
DATA_SOURCE_ID = os.environ.get('DataSourceId')
TOP_K = int(os.environ.get('TOP_K', 5))
MAX_WORKERS = int(os.environ.get('MAX_WORKERS', 5))

# ------------------ AWS Clients ------------------
s3_client = boto3.client('s3', region_name=REGION)
secrets_client = boto3.client('secretsmanager', region_name=REGION)
bedrock_client = boto3.client('bedrock-runtime', region_name=REGION)

# ------------------ Suppress PDF warnings ------------------
warnings.filterwarnings("ignore", category=UserWarning, message=".*Cannot set gray.*")

# ------------------ DB Helper ------------------
def get_db_credentials(secret_arn):
    secret = secrets_client.get_secret_value(SecretId=secret_arn)
    creds = json.loads(secret['SecretString'])
    return creds['username'], creds['password']

def get_db_connection():
    username, password = get_db_credentials(DB_SECRET_ARN)
    return psycopg2.connect(
        host=DB_HOST,
        port=DB_PORT,
        dbname=DB_NAME,
        user=username,
        password=password
    )

# ------------------ Text Extraction ------------------
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

# ------------------ Text Chunking ------------------
def chunk_text(text, chunk_size=CHUNK_SIZE):
    words = text.split()
    for i in range(0, len(words), chunk_size):
        yield i // chunk_size, " ".join(words[i:i + chunk_size])

# ------------------ Embeddings ------------------
def get_batch_embeddings(chunks):
    """
    Generates embeddings for multiple chunks.
    Titan expects a **single string per request**, so we call it per chunk in parallel.
    """
    embeddings = []

    def embed_chunk(chunk):
        try:
            response = bedrock_client.invoke_model(
                modelId="amazon.titan-embed-text-v1",
                body=json.dumps({"inputText": chunk['chunk_text']}),  # single string
                contentType="application/json",
                accept="application/json"
            )
            emb = json.loads(response['body'].read())['embedding']
            return emb
        except Exception as e:
            print(f"[WARN] Embedding failed for chunk {chunk['chunk_index']} of {chunk['document_name']}: {e}")
            return None

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        embeddings = list(executor.map(embed_chunk, chunks))

    return embeddings

# ------------------ Cosine Similarity ------------------
def parse_embedding(embedding):
    if isinstance(embedding, str):
        embedding = json.loads(embedding)
    return [float(x) for x in embedding]

def cosine_similarity(a, b):
    dot = sum(x*y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x*x for x in a))
    norm_b = math.sqrt(sum(y*y for y in b))
    return dot / (norm_a * norm_b) if norm_a and norm_b else 0.0

# ------------------ Query Top Chunks ------------------
def query_top_chunks_batch(query_texts):
    # Generate embeddings for all queries
    query_chunks = [{"chunk_text": q, "chunk_index": i, "document_name": "query"} for i, q in enumerate(query_texts)]
    query_embeddings = get_batch_embeddings(query_chunks)
    query_embeddings = [parse_embedding(e) for e in query_embeddings if e]

    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("""
        SELECT chunk_text, embedding_vector, metadata
        FROM document_chunks
        WHERE status='completed'
    """)
    rows = cur.fetchall()
    conn.close()

    # Parse DB embeddings
    chunk_vectors = [(row[0], parse_embedding(row[1]), row[2]) for row in rows]

    # Compute top K per query
    results = {}
    for embedding, query_text in zip(query_embeddings, query_texts):
        top_results = []
        for chunk_text, chunk_vector, metadata in chunk_vectors:
            similarity = cosine_similarity(embedding, chunk_vector)
            top_results.append({
                "chunk_text": chunk_text,
                "metadata": metadata,
                "similarity": similarity
            })
        top_results.sort(key=lambda x: x['similarity'], reverse=True)
        results[query_text] = top_results[:TOP_K]

    return results

# ------------------ KB Sync ------------------
def start_kb_sync():
    if not KB_ID or not DATA_SOURCE_ID:
        print("[WARN] KB_ID or DataSourceId not set, skipping sync")
        return
    client = boto3.client("bedrock-agent", region_name=REGION)
    for attempt in range(5):
        try:
            resp = client.start_ingestion_job(
                knowledgeBaseId=KB_ID,
                dataSourceId=DATA_SOURCE_ID
            )
            print("Started ingestion job:", resp["ingestionJob"]["ingestionJobId"])
            return resp
        except ClientError as e:
            if e.response["Error"]["Code"] == "ConflictException":
                print("Ingestion already running. Retrying in 60 s...")
                time.sleep(60)
            else:
                raise
    raise TimeoutError("Max retries reached while waiting for ingestion slot.")

# ------------------ Ingest Single S3 File ------------------
def ingest_s3_file(record):
    bucket = record['s3']['bucket']['name']
    key = record['s3']['object']['key']
    result = {"file": key}

    try:
        text = extract_text_from_s3(bucket, key)
        if not text.strip():
            return {"file": key, "status": "skipped", "error": "Empty document"}

        conn = get_db_connection()
        with conn:
            with conn.cursor() as cur:
                # Duplicate check
                cur.execute("SELECT document_id FROM documents WHERE document_name=%s", (key,))
                if cur.fetchone():
                    return {"file": key, "status": "skipped", "error": "Already ingested"}

                document_id = str(uuid.uuid4())
                cur.execute("""
                    INSERT INTO documents (document_id, document_name, status, created_at)
                    VALUES (%s, %s, 'in-progress', %s)
                """, (document_id, key, datetime.utcnow()))

                # Chunk and embed
                chunks = []
                for idx, chunk_text_content in chunk_text(text):
                    chunks.append({
                        "document_id": document_id,
                        "document_name": key,
                        "chunk_index": idx,
                        "chunk_text": chunk_text_content
                    })

                # Batch embeddings
                for i in range(0, len(chunks), BATCH_SIZE):
                    batch = chunks[i:i + BATCH_SIZE]
                    embeddings = get_batch_embeddings(batch)
                    chunk_records = [
                        (
                            c["document_id"],
                            c["document_name"],
                            c["chunk_index"],
                            c["chunk_text"],
                            e if e else [],
                            datetime.utcnow()
                        ) for c, e in zip(batch, embeddings)
                    ]
                    cur.executemany("""
                        INSERT INTO document_chunks
                        (document_id, document_name, chunk_index, chunk_text, embedding_vector, created_at)
                        VALUES (%s, %s, %s, %s, %s::vector, %s)
                    """, chunk_records)

                # Mark document complete
                cur.execute("""
                    UPDATE documents
                    SET status='completed', updated_at=NOW()
                    WHERE document_id=%s
                """, (document_id,))
        result["status"] = "success"

    except Exception as e:
        result["status"] = "failed"
        result["error"] = str(e)
        print(f"[ERROR] Ingest failed for {key}: {e}")

    finally:
        if 'conn' in locals():
            conn.close()

    return result

# ------------------ Ingest Multiple S3 Files in Parallel ------------------
def ingest_s3_records_parallel(records):
    results = []
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        future_to_record = {executor.submit(ingest_s3_file, r): r for r in records}
        for future in as_completed(future_to_record):
            try:
                results.append(future.result())
            except Exception as e:
                record = future_to_record[future]
                results.append({"file": record['s3']['object']['key'], "status": "failed", "error": str(e)})
    return results

# ------------------ Main Lambda Handler ------------------
def lambda_handler(event, context):
    s3_records = event.get('Records', [])
    ingest_results = []

    if s3_records:
        ingest_results = ingest_s3_records_parallel(s3_records)

    # Execute queries
    queries = event.get('queries') or ([event.get('query')] if event.get('query') else [])
    kb_results = {}
    if queries:
        try:
            kb_results = query_top_chunks_batch(queries)
        except Exception as e:
            print(f"[WARN] KnowledgeBase query failed: {e}")
            kb_results = {"error": str(e)}

    # Trigger KB sync once
    try:
        start_kb_sync()
    except Exception as e:
        print(f"[WARN] KB sync failed: {e}")

    return {
        "statusCode": 200,
        "body": {
            "ingest": ingest_results,
            "knowledgebase": kb_results
        }
    }
