import os
import io
import json
import boto3
import psycopg2
import chardet
import pdfplumber
import docx
from datetime import datetime

# ------------------ Environment Variables ------------------
DB_HOST = os.environ['DB_HOST']
DB_PORT = os.environ['DB_PORT']
DB_NAME = os.environ['DB_NAME']
DB_SECRET_ARN = os.environ['DB_SECRET_ARN']
REGION = os.environ.get('REGION', 'us-east-1')
CHUNK_SIZE = int(os.environ.get('CHUNK_SIZE', 500))  # words per chunk

# ------------------ Initialize clients ------------------
s3_client = boto3.client('s3', region_name=REGION)
secrets_client = boto3.client('secretsmanager', region_name=REGION)
bedrock_client = boto3.client('bedrock-runtime', region_name=REGION)

# ------------------ Database ------------------
def get_db_credentials(secret_arn):
    secret = secrets_client.get_secret_value(SecretId=secret_arn)
    creds = json.loads(secret['SecretString'])
    return creds['username'], creds['password']

def get_db_connection():
    username, password = get_db_credentials(DB_SECRET_ARN)
    conn = psycopg2.connect(
        host=DB_HOST,
        port=DB_PORT,
        dbname=DB_NAME,
        user=username,
        password=password
    )
    return conn

# ------------------ Text Extraction ------------------
def extract_text_from_s3(bucket, key):
    s3_obj = s3_client.get_object(Bucket=bucket, Key=key)
    raw_bytes = s3_obj['Body'].read()
    ext = key.split('.')[-1].lower()

    if ext == 'pdf':
        with pdfplumber.open(io.BytesIO(raw_bytes)) as pdf:
            return "\n".join([page.extract_text() or "" for page in pdf.pages])
    elif ext == 'docx':
        doc = docx.Document(io.BytesIO(raw_bytes))
        return "\n".join([para.text for para in doc.paragraphs])
    else:
        encoding = chardet.detect(raw_bytes)['encoding'] or 'utf-8'
        return raw_bytes.decode(encoding, errors='ignore')

# ------------------ Chunking ------------------
def chunk_text(text, chunk_size=CHUNK_SIZE):
    words = text.split()
    for i in range(0, len(words), chunk_size):
        yield i // chunk_size, " ".join(words[i:i+chunk_size])

# ------------------ Embeddings ------------------
def generate_embedding(text):
    response = bedrock_client.invoke_model(
        ModelId="amazon.titan-embed-text-v1",
        Body=json.dumps({"text": text}),
        ContentType="application/json"
    )
    result = json.loads(response['Body'].read())
    return result['embedding']

# ------------------ Lambda Handler ------------------
def lambda_handler(event, context):
    conn = get_db_connection()
    cur = conn.cursor()

    for record in event.get('Records', []):
        bucket = record['s3']['bucket']['name']
        key = record['s3']['object']['key']

        try:
            document_text = extract_text_from_s3(bucket, key)
        except Exception as e:
            print(f"Failed to extract text from {key}: {e}")
            continue

        # Extract metadata
        document_id = key.split('/')[-1].split('.')[0]  # e.g., "doc_001"
        tenant_id = "tenant_001"
        user_id = "user_001"
        project_id = "project_001"
        thread_id = "thread_001"

        # Insert document record with in-progress status
        cur.execute("""
            INSERT INTO documents (document_id, document_name, tenant_id, user_id, project_id, thread_id, status, created_at)
            VALUES (%s, %s, %s, %s, %s, %s, 'in-progress', %s)
            ON CONFLICT (document_id) DO UPDATE
                SET status='in-progress', updated_at=NOW()
        """, (document_id, key, tenant_id, user_id, project_id, thread_id, datetime.utcnow()))
        conn.commit()

        # Process chunks
        for chunk_index, chunk in chunk_text(document_text):
            embedding_vector = generate_embedding(chunk)
            metadata = {
                "tenant_id": tenant_id,
                "user_id": user_id,
                "project_id": project_id,
                "thread_id": thread_id,
                "chunk_index": chunk_index
            }

            cur.execute("""
                INSERT INTO document_chunks (document_id, chunk_index, chunk_text, embedding_vector, metadata, status, created_at)
                VALUES (%s, %s, %s, %s, %s, 'completed', %s)
            """, (document_id, chunk_index, chunk, embedding_vector, json.dumps(metadata), datetime.utcnow()))

        # Update document status to completed
        cur.execute("""
            UPDATE documents SET status='completed', updated_at=NOW()
            WHERE document_id=%s
        """, (document_id,))
        conn.commit()

        print(f"Document {document_id} ingested successfully.")

    cur.close()
    conn.close()

    return {"statusCode": 200, "body": f"Ingestion completed for {len(event.get('Records', []))} file(s)"}
