"""
Lambda handler: triggered by S3 ObjectCreated events.
- Downloads the object
- Extracts text (supports txt, pdf, docx heuristically)
- Splits into chunks
- Calls AWS Bedrock to get embeddings
- Inserts chunk + metadata into PostgreSQL using credentials from Secrets Manager
This is a PoC and keeps things simple.
"""

import os
import json
import tempfile
import boto3
import logging
import psycopg2
from botocore.exceptions import ClientError
from typing import List
from pdfminer.high_level import extract_text as extract_text_from_pdf
from bs4 import BeautifulSoup

logger = logging.getLogger()
logger.setLevel(logging.INFO)

s3 = boto3.client('s3')
secrets = boto3.client('secretsmanager')
bedrock = boto3.client('bedrock-runtime')

DB_SECRET_ARN = os.environ.get('DB_SECRET_ARN')
DB_NAME = os.environ.get('DB_NAME')
DB_HOST = os.environ.get('DB_HOST')
DB_PORT = os.environ.get('DB_PORT', '5432')
REGION = os.environ.get('REGION')

# Simple splitter
def chunk_text(text: str, chunk_size: int = 500, overlap: int = 50) -> List[str]:
    if not text:
        return []
    chunks = []
    start = 0
    length = len(text)
    while start < length:
        end = min(start + chunk_size, length)
        chunk = text[start:end]
        chunks.append(chunk)
        start = end - overlap
        if start < 0:
            start = 0
    return chunks

def get_db_credentials():
    resp = secrets.get_secret_value(SecretId=DB_SECRET_ARN)
    secret = json.loads(resp['SecretString'])
    return secret

def connect_db(creds):
    conn = psycopg2.connect(
        host=DB_HOST or creds.get('host', 'localhost'),
        dbname=creds.get('dbname', DB_NAME),
        user=creds['username'],
        password=creds['password'],
        port=int(DB_PORT)
    )
    return conn

def extract_text_from_key(bucket, key, local_path):
    # Download
    s3.download_file(bucket, key, local_path)
    if key.lower().endswith('.pdf'):
        return extract_text_from_pdf(local_path)
    elif key.lower().endswith('.html') or key.lower().endswith('.htm'):
        with open(local_path, 'r', encoding='utf-8', errors='ignore') as f:
            soup = BeautifulSoup(f.read(), 'html.parser')
            return soup.get_text(separator=' ')
    else:
        # default: treat as plain text
        with open(local_path, 'r', encoding='utf-8', errors='ignore') as f:
            return f.read()

def bedrock_embed(text: str, metadata: dict) -> List[float]:
    # Use Bedrock Knowledge Base service for embedding
    payload = {
        "input": text,
        "metadata": metadata  # Pass metadata for indexing
    }
    response = bedrock.invoke_model(
        modelId='amazon.titan-embed-text-v1',
        contentType='application/json',
        body=json.dumps(payload)
    )
    body = response['body'].read()
    data = json.loads(body)
    if 'embedding' in data:
        return data['embedding']
    raise RuntimeError('Unexpected Bedrock response: %s' % (data,))

def retrieve_chunks(conn, document_ids: List[str]) -> List[dict]:
    # Retrieve chunks by filtering on document IDs
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT tenant_id, user_id, document_id, project_id, thread_id, chunk_text, embedding
            FROM document_chunks
            WHERE document_id = ANY(%s)
            """,
            (document_ids,)
        )
        return cur.fetchall()

def insert_chunk(conn, metadata: dict, chunk_text: str, embedding: List[float]):
    with conn.cursor() as cur:
        # store embedding as float8 array or vector depending on pg extension
        cur.execute(
            """
            INSERT INTO document_chunks (tenant_id, user_id, document_id, project_id, thread_id, chunk_text, embedding)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            """,
            (
                metadata.get('tenant_id'),
                metadata.get('user_id'),
                metadata.get('document_id'),
                metadata.get('project_id'),
                metadata.get('thread_id'),
                chunk_text,
                embedding
            )
        )
    conn.commit()

def lambda_handler(event, context):
    logger.info('Event: %s', json.dumps(event))
    # Expect S3 Put event
    record = event['Records'][0]
    bucket = record['s3']['bucket']['name']
    key = record['s3']['object']['key']

    # Optionally read metadata from S3 object tags or metadata
    metadata = {
        # For PoC we try to read object tags for tenant/user/document/project/thread
        'tenant_id': 'tenant_poc',
        'user_id': 'user_poc',
        'document_id': key,
        'project_id': 'project_poc',
        'thread_id': 'thread_poc'
    }
    try:
        tagging = s3.get_object_tagging(Bucket=bucket, Key=key)
        for tag in tagging.get('TagSet', []):
            k = tag['Key'].lower()
            if k in metadata:
                metadata[k] = tag['Value']
    except ClientError:
        logger.info('No tags or unable to read tags for %s/%s', bucket, key)

    with tempfile.TemporaryDirectory() as tmpdir:
        local_path = os.path.join(tmpdir, os.path.basename(key))
        text = extract_text_from_key(bucket, key, local_path)
        chunks = chunk_text(text, chunk_size=800, overlap=100)
        creds = get_db_credentials()
        conn = connect_db(creds)
        try:
            for ch in chunks:
                emb = bedrock_embed(ch, metadata)  # Pass metadata for embedding
                insert_chunk(conn, metadata, ch, emb)
        finally:
            conn.close()

    return {'status': 'ok', 'chunks': len(chunks)}