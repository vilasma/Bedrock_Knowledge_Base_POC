import os
import json
import asyncio
import aioboto3
import asyncpg

DB_HOST = os.environ['DB_HOST']
DB_PORT = int(os.environ['DB_PORT'])
DB_NAME = os.environ['DB_NAME']
DB_SECRET_ARN = os.environ['DB_SECRET_ARN']
REGION = os.environ.get('REGION', 'us-east-1')
TOP_K = int(os.environ.get('TOP_K', 5))

async def get_db_credentials(secret_arn):
    session = aioboto3.Session()
    async with session.client('secretsmanager', region_name=REGION) as secrets_client:
        secret = await secrets_client.get_secret_value(SecretId=secret_arn)
        creds = json.loads(secret['SecretString'])
        return creds['username'], creds['password']

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

async def get_query_embedding(text):
    session = aioboto3.Session()
    async with session.client('bedrock-runtime', region_name=REGION) as client:
        response = await client.invoke_model(
            modelId="amazon.titan-embed-text-v1",
            body=json.dumps({"inputText": text}),
            contentType="application/json",
            accept="application/json"
        )
        result = json.loads(await response['body'].read())
        return result['embedding']

async def query_top_k(pool, query_text):
    query_embedding = await get_query_embedding(query_text)
    async with pool.acquire() as conn:
        rows = await conn.fetch(f"""
            SELECT chunk_text, metadata, embedding_vector <#> $1 AS similarity
            FROM document_chunks
            WHERE status='completed'
            ORDER BY embedding_vector <#> $1
            LIMIT {TOP_K}
        """, query_embedding)
        return [
            {"chunk_text": r["chunk_text"], "metadata": r["metadata"], "similarity": r["similarity"]}
            for r in rows
        ]

async def async_handler(event, context):
    pool = await get_db_pool()
    query_text = event.get("query", "Retrieve relevant chunks")
    top_results = await query_top_k(pool, query_text)
    await pool.close()
    return {"statusCode": 200, "body": json.dumps(top_results)}

def lambda_handler(event, context):
    return asyncio.run(async_handler(event, context))
