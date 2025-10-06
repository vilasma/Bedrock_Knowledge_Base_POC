import os
import json
import math
import ast
import boto3
import psycopg2
import time
from botocore.exceptions import ClientError

DB_HOST = os.environ['DB_HOST']
DB_PORT = int(os.environ['DB_PORT'])
DB_NAME = os.environ['DB_NAME']
DB_USER = os.environ.get('DB_USER')
DB_PASSWORD = os.environ.get('DB_PASSWORD')
DB_SECRET_ARN = os.environ.get('DB_SECRET_ARN')
REGION = os.environ.get('REGION', 'us-east-1')
TOP_K = int(os.environ.get('TOP_K', 5))
KB_ID = os.environ.get('KB_ID')
DataSourceId = os.environ.get('DataSourceId')

# ---------------- DB Helpers ----------------
def get_db_credentials(secret_arn):
    client = boto3.client('secretsmanager', region_name=REGION)
    secret = client.get_secret_value(SecretId=secret_arn)
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

# ---------------- Embedding / Similarity ----------------
def parse_embedding(embedding):
    if isinstance(embedding, str):
        embedding = ast.literal_eval(embedding)
    return [float(x) for x in embedding]

def cosine_similarity(a, b):
    dot = sum(x*y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x*x for x in a))
    norm_b = math.sqrt(sum(y*y for y in b))
    return dot / (norm_a * norm_b) if norm_a and norm_b else 0.0

def get_batch_embeddings(text_list):
    """
    True batch embedding using a single Titan API call.
    Note: Titan supports 'inputText' as a list for batch embedding.
    """
    client = boto3.client("bedrock-runtime", region_name=REGION)
    body = {"inputText": text_list}  # List of queries
    response = client.invoke_model(
        modelId="amazon.titan-embed-text-v1",
        body=json.dumps(body),
        contentType="application/json",
        accept="application/json"
    )
    result = json.loads(response['body'].read())
    # Titan returns a list of embeddings corresponding to each input
    return result['embeddings']

def query_top_chunks_batch(query_embeddings, query_texts):
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

# ---------------- KB Sync ----------------
def start_kb_sync():
    if not KB_ID:
        print("[WARN] KB_ID not set, skipping sync")
        return
    client = boto3.client("bedrock-agent", region_name=REGION)
    for attempt in range(5):
        try:
            resp = client.start_ingestion_job(
                knowledgeBaseId=KB_ID,
                dataSourceId=DataSourceId
            )
            print("Started ingestion job:", resp["ingestionJob"]["ingestionJobId"])
            return resp
        except ClientError as e:
            if e.response["Error"]["Code"] == "ConflictException":
                print(f"Ingestion already running. Retrying in 60 s...")
                time.sleep(60)
            else:
                raise
    raise TimeoutError("Max retries reached while waiting for ingestion slot.")

# ---------------- Lambda Handler ----------------
def lambda_handler(event, context):
    query_texts = event.get('queries') or [event.get('query', 'Retrieve relevant chunks')]
    
    try:
        # ✅ True batch embeddings
        query_embeddings = get_batch_embeddings(query_texts)
    except Exception as e:
        return {"statusCode": 500, "body": f"Embedding generation failed: {e}"}

    try:
        results = query_top_chunks_batch(query_embeddings, query_texts)
    except Exception as e:
        return {"statusCode": 500, "body": f"DB query failed: {e}"}

    # Trigger KB sync once after retrieval
    try:
        start_kb_sync()
    except Exception as e:
        print(f"[WARN] KB sync failed: {e}")

    return {
        "statusCode": 200,
        "body": json.dumps(results)
    }
