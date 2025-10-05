import os
import io
import json
import boto3
import psycopg2
import chardet
import pdfplumber
import docx
import uuid
import logging
import warnings
from datetime import datetime
from pdfminer.pdfinterp import PDFInterpreterError

# ------------------ Logging and Warning Suppression ------------------
logging.getLogger("pdfminer").setLevel(logging.ERROR)

# Suppress benign warnings (but NOT exceptions like PDFInterpreterError)
warnings.filterwarnings("ignore", category=UserWarning, message=".*Cannot set gray.*")

# ------------------ Environment ------------------
DB_HOST = os.environ['DB_HOST']
DB_PORT = os.environ['DB_PORT']
DB_NAME = os.environ['DB_NAME']
DB_SECRET_ARN = os.environ['DB_SECRET_ARN']
REGION = os.environ.get('REGION', 'us-east-1')
CHUNK_SIZE = int(os.environ.get('CHUNK_SIZE', 500))

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
        host=DB_HOST, port=DB_PORT, dbname=DB_NAME,
        user=username, password=password
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
            encoding = chardet.detect(raw_bytes)['encoding'] or 'utf-8'
            return raw_bytes.decode(encoding, errors='ignore')
    except PDFInterpreterError:
        return ""
    except Exception as e:
        print(f"[ERROR] Failed to extract text: {e}")
        return ""

def chunk_text(text, chunk_size=CHUNK_SIZE):
    words = text.split()
    for i in range(0, len(words), chunk_size):
        yield i // chunk_size, " ".join(words[i:i + chunk_size])

def get_query_embedding(query_text):
    response = bedrock_client.invoke_model(
        modelId="amazon.titan-embed-text-v1",
        body=json.dumps({"inputText": query_text}),
        contentType="application/json",
        accept="application/json"
    )
    result = json.loads(response['body'].read())
    return result['embedding']

# ------------------ Lambda Handler ------------------
def lambda_handler(event, context):
    # Use CURRENT_S3_BUCKET and CURRENT_S3_KEY injected by main Lambda
    bucket = os.environ.get("CURRENT_S3_BUCKET")
    key = os.environ.get("CURRENT_S3_KEY")
    if not bucket or not key:
        return {"statusCode": 400, "body": "S3 bucket/key not found in environment"}

    conn = get_db_connection()
    cur = conn.cursor()

    document_text = extract_text_from_s3(bucket, key)
    if not document_text.strip():
        return {"statusCode": 200, "body": f"No readable text in {key}"}

    document_id = str(uuid.uuid4())
    tenant_id = "tenant_001"
    user_id = "user_001"
    project_id = "project_001"
    thread_id = "thread_001"

    # Insert document record
    cur.execute("""
        INSERT INTO documents (document_id, document_name, tenant_id, user_id, project_id, thread_id, status, created_at)
        VALUES (%s, %s, %s, %s, %s, %s, 'in-progress', %s)
        ON CONFLICT (document_id) DO UPDATE
            SET status='in-progress', updated_at=NOW()
    """, (document_id, key, tenant_id, user_id, project_id, thread_id, datetime.utcnow()))
    conn.commit()

    # Embed chunks
    for chunk_index, chunk in chunk_text(document_text):
        try:
            embedding_vector = get_query_embedding(chunk)
        except Exception as e:
            print(f"[ERROR] Chunk {chunk_index}: {e}")
            continue

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

    cur.execute("UPDATE documents SET status='completed', updated_at=NOW() WHERE document_id=%s", (document_id,))
    conn.commit()
    cur.close()
    conn.close()

    return {"statusCode": 200, "body": f"Ingested {key} successfully"}
