import os
import io
import json
import uuid
import boto3
import psycopg2
import pdfplumber
import docx
from datetime import datetime
import time
from botocore.exceptions import ClientError

DB_HOST = os.environ['DB_HOST']
DB_PORT = int(os.environ['DB_PORT'])
DB_NAME = os.environ['DB_NAME']
DB_SECRET_ARN = os.environ['DB_SECRET_ARN']
REGION = os.environ.get('REGION', 'us-east-1')
CHUNK_SIZE = int(os.environ.get('CHUNK_SIZE', 500))
KB_ID = os.environ.get('KB_ID')
DATA_SOURCE_ID = os.environ.get('DataSourceId')
BATCH_SIZE = int(os.environ.get('BATCH_SIZE', 20))  # Number of chunks per embedding batch

s3_client = boto3.client('s3', region_name=REGION)
secrets_client = boto3.client('secretsmanager', region_name=REGION)
bedrock_client = boto3.client('bedrock-runtime', region_name=REGION)

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

def get_batch_embeddings(chunks):
    """
    Send a batch of text chunks to Bedrock embedding model.
    Each element in `chunks` is a dict with keys: chunk_text, document_id, chunk_index, metadata
    Returns a list of embeddings aligned with input chunks.
    """
    embeddings = []
    for start in range(0, len(chunks), BATCH_SIZE):
        batch = chunks[start:start+BATCH_SIZE]
        batch_texts = [c['chunk_text'] for c in batch]
        response = bedrock_client.invoke_model(
            modelId="amazon.titan-embed-text-v1",
            body=json.dumps({"inputText": batch_texts}),
            contentType="application/json",
            accept="application/json"
        )
        batch_embeddings = json.loads(response['body'].read())['embedding']
        embeddings.extend(batch_embeddings)
    return embeddings

def start_kb_sync():
    if not KB_ID:
        print("[WARN] KB_ID not set, skipping sync")
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
                print(f"Ingestion already running. Retrying in 60 s...")
                time.sleep(60)
            else:
                raise
    raise TimeoutError("Max retries reached while waiting for ingestion slot.")

def lambda_handler(event, context):
    if 'Records' not in event:
        return {"statusCode": 400, "body": "No S3 records found in event"}

    conn = get_db_connection()
    processed_files = []
    all_chunks = []

    try:
        with conn:
            with conn.cursor() as cur:
                # Step 1: Extract text, create documents, and prepare chunk list
                for record in event['Records']:
                    bucket = record['s3']['bucket']['name']
                    key = record['s3']['object']['key']
                    document_text = extract_text_from_s3(bucket, key)
                    if not document_text.strip():
                        continue

                    document_id = str(uuid.uuid4())
                    tenant_id = f"tenant_{uuid.uuid4().hex[:6]}"
                    user_id = f"user_{uuid.uuid4().hex[:6]}"
                    project_id = f"project_{uuid.uuid4().hex[:6]}"
                    thread_id = f"thread_{uuid.uuid4().hex[:6]}"

                    cur.execute("""
                        INSERT INTO documents
                        (document_id, document_name, tenant_id, user_id, project_id, thread_id, status, created_at)
                        VALUES (%s, %s, %s, %s, %s, %s, 'in-progress', %s)
                    """, (document_id, key, tenant_id, user_id, project_id, thread_id, datetime.utcnow()))

                    # Prepare chunk dicts for batch embedding
                    for chunk_index, chunk_text in chunk_text(document_text):
                        metadata = {
                            "tenant_id": tenant_id,
                            "user_id": user_id,
                            "project_id": project_id,
                            "thread_id": thread_id,
                            "chunk_index": chunk_index
                        }
                        all_chunks.append({
                            "document_id": document_id,
                            "document_name": key,
                            "chunk_index": chunk_index,
                            "chunk_text": chunk_text,
                            "metadata": metadata
                        })

                    processed_files.append(key)

                # Step 2: Generate embeddings in batches
                embeddings = get_batch_embeddings(all_chunks)

                # Step 3: Insert all chunks with embeddings
                for chunk, embedding in zip(all_chunks, embeddings):
                    cur.execute("""
                        INSERT INTO document_chunks
                        (document_id, document_name, chunk_index, chunk_text, embedding_vector, metadata, status, created_at)
                        VALUES (%s, %s, %s, %s, %s::vector, %s, 'completed', %s)
                        """, (
                        chunk["document_id"],
                        chunk["document_name"],
                        chunk["chunk_index"],
                        chunk["chunk_text"],
                        embedding,
                        json.dumps(chunk["metadata"]),
                        datetime.utcnow()
                    ))

                # Step 4: Update document status
                for chunk in all_chunks:
                    cur.execute("""
                        UPDATE documents SET status='completed', updated_at=NOW() 
                        WHERE document_id=%s
                    """, (chunk["document_id"],))

        # Trigger KB sync after all commits
        try:
            start_kb_sync()
        except Exception as e:
            print(f"[WARN] KB sync failed: {e}")

    finally:
        conn.close()

    return {
        "statusCode": 200,
        "body": f"Ingested files: {processed_files} with batched embeddings successfully"
    }
