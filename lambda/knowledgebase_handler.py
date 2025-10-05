import os
import asyncio
import json
import ast
import aioboto3
import asyncpg
import math

# ------------------ ENV ------------------
DB_HOST = os.environ['DB_HOST']
DB_PORT = int(os.environ['DB_PORT'])
DB_NAME = os.environ['DB_NAME']
DB_SECRET_ARN = os.environ['DB_SECRET_ARN']
REGION = os.environ.get('REGION', 'us-east-1')
TOP_K = int(os.environ.get('TOP_K', 5))

# ---------- KB ID cache (future use) ----------
KB_ID_CACHE = None

# ------------------ HELPERS ------------------
async def get_db_credentials(secret_arn):
    session = aioboto3.Session()
    async with session.client('secretsmanager', region_name=REGION) as client:
        secret = await client.get_secret_value(SecretId=secret_arn)
        creds = json.loads(secret['SecretString'])
        return creds['username'], creds['password']

def parse_embedding(embedding):
    if isinstance(embedding, str):
        embedding = ast.literal_eval(embedding)
    return [float(x) for x in embedding]

def cosine_similarity(a, b):
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x*x for x in a))
    norm_b = math.sqrt(sum(y*y for y in b))
    return dot / (norm_a * norm_b) if norm_a and norm_b else 0.0

async def get_db_pool():
    username, password = await get_db_credentials(DB_SECRET_ARN)
    return await asyncpg.create_pool(
        host=DB_HOST,
        port=DB_PORT,
        database=DB_NAME,
        user=username,
        password=password,
        min_size=1,
        max_size=10
    )

async def async_query_chunks(pool, query_text):
    """
    Fetch embeddings from DB, get query embedding from Bedrock, and return top-K similar chunks.
    """
    # Call Bedrock to get query embedding
    session = aioboto3.Session()
    async with session.client('bedrock-runtime', region_name=REGION) as client:
        response = await client.invoke_model(
            modelId="amazon.titan-embed-text-v1",
            body=json.dumps({"inputText": query_text}),
            contentType="application/json",
            accept="application/json"
        )
        query_embedding = json.loads(await response['body'].read())['embedding']

    # Async fetch all chunks
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT chunk_text, embedding_vector, metadata 
            FROM document_chunks 
            WHERE status='completed'
        """)

    results = []
    for row in rows:
        embedding_vector = parse_embedding(row['embedding_vector'])
        similarity = cosine_similarity(query_embedding, embedding_vector)
        results.append({
            "chunk_text": row['chunk_text'],
            "metadata": row['metadata'],
            "similarity": similarity
        })

    results.sort(key=lambda x: x['similarity'], reverse=True)
    return results[:TOP_K]

# ------------------ LAMBDA HANDLER ------------------
async def async_handler(event, context):
    query_text = event.get('query', 'Retrieve relevant chunks')
    pool = await get_db_pool()
    top_results = await async_query_chunks(pool, query_text)
    await pool.close()
    return {"statusCode": 200, "body": json.dumps(top_results)}

def lambda_handler(event, context):
    return asyncio.run(async_handler(event, context))
