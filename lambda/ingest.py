import os
import io
import json
import uuid
import asyncio
import aioboto3
import asyncpg
import pdfplumber
import docx
from datetime import datetime
from pdfminer.pdfinterp import PDFInterpreterError
import warnings

warnings.filterwarnings("ignore", category=UserWarning, message=".*Cannot set gray.*")

DB_HOST = os.environ['DB_HOST']
DB_PORT = int(os.environ['DB_PORT'])
DB_NAME = os.environ['DB_NAME']
DB_SECRET_ARN = os.environ['DB_SECRET_ARN']
REGION = os.environ.get('REGION', 'us-east-1')
CHUNK_SIZE = int(os.environ.get('CHUNK_SIZE', 500))

# ------------------ HELPERS ------------------
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

def extract_text_from_s3(bucket, key):
    import boto3
    s3_client = boto3.client('s3', region_name=REGION)
    s3_obj = s3_client.get_object(Bucket=bucket, Key=key)
    raw_bytes = s3_obj['Body'].read()
    ext = key.split('.')[-1].lower()
    try:
        if ext == 'pdf':
            with pdfplumber.open(io.BytesIO(raw_bytes)) as pdf:
                return "\n".join([page.extract_text() or "" for page in pdf.pages]).strip()
        elif ext == 'docx':
            doc = docx.Document(io.BytesIO(raw_bytes))
            return "\n".join([para.text for para in doc.paragraphs])
        else:
            return raw_bytes.decode('utf-8', errors='ignore')
    except PDFInterpreterError:
        return ""
    except Exception as e:
        print(f"[ERROR] Failed to extract text: {e}")
        return ""

def chunk_text(text, chunk_size=CHUNK_SIZE):
    words = text.split()
    for i in range(0, len(words), chunk_size):
        yield i // chunk_size, " ".join(words[i:i + chunk_size])

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

async def embed_and_store_chunk(pool, document_id, document_name, chunk_index, chunk_text, metadata):
    embedding_vector = await get_query_embedding(chunk_text)
    async with pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO document_chunks
            (document_id, document_name, chunk_index, chunk_text, embedding_vector, metadata, status, created_at)
            VALUES ($1, $2, $3, $4, $5::vector, $6, 'completed', $7)
        """, document_id, document_name, chunk_index, chunk_text, embedding_vector, json.dumps(metadata), datetime.utcnow())

# ------------------ LAMBDA HANDLER ------------------
async def async_handler(event, context):
    bucket = event.get("bucket") or os.environ.get("CURRENT_S3_BUCKET")
    key = event.get("key") or os.environ.get("CURRENT_S3_KEY")
    if not bucket or not key:
        return {"statusCode": 400, "body": "S3 bucket/key not provided"}

    document_text = extract_text_from_s3(bucket, key)
    if not document_text.strip():
        return {"statusCode": 200, "body": f"No readable text in {key}"}

    pool = await get_db_pool()
    document_id = str(uuid.uuid4())
    tenant_id = "tenant_001"
    user_id = "user_001"
    project_id = "project_001"
    thread_id = "thread_001"

    async with pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO documents
            (document_id, document_name, tenant_id, user_id, project_id, thread_id, status, created_at)
            VALUES ($1, $2, $3, $4, $5, $6, 'in-progress', $7)
            ON CONFLICT (document_id) DO UPDATE
                SET status='in-progress', updated_at=NOW()
        """, document_id, key, tenant_id, user_id, project_id, thread_id, datetime.utcnow())

    tasks = []
    for chunk_index, chunk in chunk_text(document_text):
        metadata = {
            "tenant_id": tenant_id,
            "user_id": user_id,
            "project_id": project_id,
            "thread_id": thread_id,
            "chunk_index": chunk_index
        }
        tasks.append(embed_and_store_chunk(pool, document_id, key, chunk_index, chunk, metadata))
    await asyncio.gather(*tasks)

    async with pool.acquire() as conn:
        await conn.execute("""
            UPDATE documents SET status='completed', updated_at=NOW() WHERE document_id=$1
        """, document_id)

    await pool.close()
    return {"statusCode": 200, "body": f"Ingested {key} successfully"}

def lambda_handler(event, context):
    return asyncio.run(async_handler(event, context))
