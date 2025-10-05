import os
import json
import ast
import math
import boto3
import psycopg2

# ------------------ ENV ------------------
DB_HOST = os.environ['DB_HOST']
DB_PORT = int(os.environ['DB_PORT'])
DB_NAME = os.environ['DB_NAME']
DB_USER = os.environ.get('DB_USER')  # optional if using secrets
DB_PASSWORD = os.environ.get('DB_PASSWORD')  # optional if using secrets
DB_SECRET_ARN = os.environ.get('DB_SECRET_ARN')
REGION = os.environ.get('REGION', 'us-east-1')
TOP_K = int(os.environ.get('TOP_K', 5))

# ---------- KB ID cache ----------
KB_ID_CACHE = None

# ------------------ HELPERS ------------------
def get_db_credentials(secret_arn):
    """Fetch DB username/password from Secrets Manager"""
    client = boto3.client('secretsmanager', region_name=REGION)
    secret = client.get_secret_value(SecretId=secret_arn)
    creds = json.loads(secret['SecretString'])
    return creds['username'], creds['password']

def get_db_connection():
    """Return a psycopg2 connection"""
    if DB_SECRET_ARN:
        username, password = get_db_credentials(DB_SECRET_ARN)
    else:
        username = DB_USER
        password = DB_PASSWORD

    conn = psycopg2.connect(
        host=DB_HOST,
        port=DB_PORT,
        dbname=DB_NAME,
        user=username,
        password=password
    )
    return conn

def parse_embedding(embedding):
    if isinstance(embedding, str):
        embedding = ast.literal_eval(embedding)
    return [float(x) for x in embedding]

def cosine_similarity(a, b):
    dot = sum(x*y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x*x for x in a))
    norm_b = math.sqrt(sum(y*y for y in b))
    return dot / (norm_a * norm_b) if norm_a and norm_b else 0.0

# ------------------ Bedrock KB Helpers ------------------
def get_kb_id(name="poc-bedrock-kb"):
    """Fetch KB ID and cache it"""
    global KB_ID_CACHE
    if KB_ID_CACHE:
        return KB_ID_CACHE

    client = boto3.client("bedrock", region_name=REGION)
    paginator = client.get_paginator("list_knowledge_bases")
    for page in paginator.paginate():
        for kb in page.get("KnowledgeBases", []):
            if kb["Name"] == name:
                KB_ID_CACHE = kb["KnowledgeBaseId"]
                return KB_ID_CACHE
    raise Exception(f"Knowledge Base '{name}' not found")

def start_kb_sync(kb_id):
    client = boto3.client("bedrock-runtime", region_name=REGION)
    client.start_knowledge_base_sync(KnowledgeBaseId=kb_id)
    print(f"[INFO] KB sync started for KB ID: {kb_id}")

# ------------------ Retrieval ------------------
def get_query_embedding(text):
    """Get embedding for query text using Bedrock"""
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
    """Return top-K chunks based on cosine similarity"""
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

# ------------------ LAMBDA HANDLER ------------------
def lambda_handler(event, context):
    query_text = event.get('query', 'Retrieve relevant chunks')
    
    # 1️⃣ Get top chunks
    top_chunks = query_top_chunks(query_text)

    # 2️⃣ Trigger KB sync automatically
    try:
        kb_id = get_kb_id()
        start_kb_sync(kb_id)
    except Exception as e:
        print(f"[WARN] KB sync failed: {e}")

    return {
        "statusCode": 200,
        "body": json.dumps(top_chunks)
    }
