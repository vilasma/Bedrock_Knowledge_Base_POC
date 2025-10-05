import os
import io
import json
import uuid
import boto3
import psycopg2
import pdfplumber
import docx
from datetime import datetime
import warnings

warnings.filterwarnings("ignore", category=UserWarning, message=".*Cannot set gray.*")

# ------------------ ENV ------------------
DB_HOST = os.environ['DB_HOST']
DB_PORT = int(os.environ['DB_PORT'])
DB_NAME = os.environ['DB_NAME']
DB_SECRET_ARN = os.environ['DB_SECRET_ARN']
REGION = os.environ.get('REGION', 'us-east-1')
CHUNK_SIZE = int(os.environ.get('CHUNK_SIZE', 500))
KB_ID = os.environ.get('KB_ID')  # KB ID passed as environment variable

# ------------------ Clients ------------------
s3_client = boto3.client('s3', region_name=REGION)
secrets_client = boto3.client('secretsmanager', region_name=REGION)
bedrock_client = boto3.client('bedrock-runtime', region_name=REGION)

# ------------------ Helpers ------------------
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

def extract_text_from_s3(bucket, key):
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
    except Exception as e:
        print(f"[ERROR] Failed to extract text: {e}")
        return ""

def chunk_text(text, chunk_size=CHUNK_SIZE):
    words = text.split()
    for i in range(0, len(words), chunk_size):
        yield i // chunk_size, " ".join(words[i:i + chunk_size])

def get_query_embedding(text):
    response = bedrock_client.invoke_model(
        modelId="amazon.titan-embed-text-v1",
        body=json.dumps({"inputText": text}),
        contentType="application/json",
        accept="application/json"
    )
    result = json.loads(response['body'].read())
    return result['embedding']

def embed_and_store_chunk(conn, document_id, document_name, chunk_index, chunk_text, metadata):
    embedding_vector = get_query_embedding(chunk_text)
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO document_chunks
            (document_id, document_name, chunk_index, chunk_text, embedding_vector, metadata, status, created_at)
            VALUES (%s, %s, %s, %s, %s::vector, %s, 'completed', %s)
            ON CONFLICT (document_id, chunk_index) DO UPDATE
            SET chunk_text = EXCLUDED.chunk_text,
                embedding_vector = EXCLUDED.embedding_vector,
                metadata = EXCLUDED.metadata,
                status = 'completed',
                updated_at = NOW()
        """, (document_id, document_name, chunk_index, chunk_text, embedding_vector, json.dumps(metadata), datetime.utcnow()))
        conn.commit()

def start_kb_sync():
    if not KB_ID:
        print("[WARN] KB_ID not set, skipping sync")
        return
    bedrock_client.start_knowledge_base_sync(KnowledgeBaseId=KB_ID)
    print(f"[INFO] Knowledge Base sync started for KB ID: {KB_ID}")

# ------------------ Lambda Handler ------------------
def lambda_handler(event, context):
    bucket = event.get("bucket") or os.environ.get("CURRENT_S3_BUCKET")
    key = event.get("key") or os.environ.get("CURRENT_S3_KEY")
    if not bucket or not key:
        return {"statusCode": 400, "body": "S3 bucket/key not provided"}

    document_text = extract_text_from_s3(bucket, key)
    if not document_text.strip():
        return {"statusCode": 200, "body": f"No readable text in {key}"}

    conn = get_db_connection()
    document_id = str(uuid.uuid4())
    tenant_id = "tenant_001"
    user_id = "user_001"
    project_id = "project_001"
    thread_id = "thread_001"

    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO documents
            (document_id, document_name, tenant_id, user_id, project_id, thread_id, status, created_at)
            VALUES (%s, %s, %s, %s, %s, %s, 'in-progress', %s)
            ON CONFLICT (document_id) DO UPDATE
                SET status='in-progress', updated_at=NOW()
        """, (document_id, key, tenant_id, user_id, project_id, thread_id, datetime.utcnow()))
        conn.commit()

    # Embed + store chunks
    for chunk_index, chunk in chunk_text(document_text):
        metadata = {
            "tenant_id": tenant_id,
            "user_id": user_id,
            "project_id": project_id,
            "thread_id": thread_id,
            "chunk_index": chunk_index
        }
        embed_and_store_chunk(conn, document_id, key, chunk_index, chunk, metadata)

    with conn.cursor() as cur:
        cur.execute("""
            UPDATE documents SET status='completed', updated_at=NOW() WHERE document_id=%s
        """, (document_id,))
        conn.commit()

    # Trigger KB sync
    try:
        start_kb_sync()
    except Exception as e:
        print(f"[WARN] KB sync failed: {e}")

    conn.close()
    return {"statusCode": 200, "body": f"Ingested {key} successfully and triggered KB sync"}
