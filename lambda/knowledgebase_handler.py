import os
import json
import math
import ast
import boto3
import psycopg2

DB_HOST = os.environ['DB_HOST']
DB_PORT = int(os.environ['DB_PORT'])
DB_NAME = os.environ['DB_NAME']
DB_USER = os.environ.get('DB_USER')
DB_PASSWORD = os.environ.get('DB_PASSWORD')
DB_SECRET_ARN = os.environ.get('DB_SECRET_ARN')
REGION = os.environ.get('REGION', 'us-east-1')
TOP_K = int(os.environ.get('TOP_K', 5))
KB_ID = os.environ.get('KB_ID')

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

def get_query_embedding(text):
    client = boto3.client("bedrock-runtime", region_name=REGION)
    response = client.invoke_model(
        modelId="amazon.titan-embed-text-v1",
        body=json.dumps({"inputText": text}),
        contentType="application/json",
        accept="application/json"
    )
    result = json.loads(response['body'].read())
    return result['embedding']

def query_top_chunks(query_text):
    """Return top-K chunks for a single query"""
    embedding = get_query_embedding(query_text)
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

    results = []
    for row in rows:
        chunk_vector = parse_embedding(row[1])
        similarity = cosine_similarity(embedding, chunk_vector)
        results.append({
            "chunk_text": row[0],
            "metadata": row[2],
            "similarity": similarity
        })

    results.sort(key=lambda x: x['similarity'], reverse=True)
    return results[:TOP_K]

# ---------------- KB Sync ----------------
def start_kb_sync():
    if not KB_ID:
        print("[WARN] KB_ID not set, skipping sync")
        return
    client = boto3.client("bedrock", region_name=os.environ.get("REGION", "us-east-1"))
    try:
        client.start_knowledge_base_sync(KnowledgeBaseId=KB_ID)
        print(f"[INFO] Knowledge Base sync started for KB ID: {KB_ID}")
    except AttributeError as e:
        print(f"[ERROR] KB sync failed: {e}")

# ---------------- Lambda Handler ----------------
def lambda_handler(event, context):
    queries = event.get('queries') or [event.get('query', 'Retrieve relevant chunks')]
    results = {}

    try:
        for query_text in queries:
            top_chunks = query_top_chunks(query_text)
            results[query_text] = top_chunks
    except Exception as e:
        return {"statusCode": 500, "body": f"DB query failed: {e}"}

    try:
        start_kb_sync()
    except Exception as e:
        print(f"[WARN] KB sync failed: {e}")

    return {
        "statusCode": 200,
        "body": json.dumps(results)
    }
