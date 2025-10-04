import os
import psycopg2
import json
import boto3
import math

DB_HOST = os.environ['DB_HOST']
DB_PORT = os.environ['DB_PORT']
DB_NAME = os.environ['DB_NAME']
DB_SECRET_ARN = os.environ['DB_SECRET_ARN']
REGION = os.environ.get('REGION', 'us-east-1')
INPUT_S3_DIR = os.environ['INPUT_S3_DIR']

CHUNK_SIZE = 500  # number of words per chunk (adjust as needed)

# Initialize boto3 clients
s3_client = boto3.client('s3', region_name=REGION)
bedrock_client = boto3.client('bedrock-runtime', region_name=REGION)
secrets_client = boto3.client('secretsmanager', region_name=REGION)


def get_db_credentials(secret_arn):
    secret = secrets_client.get_secret_value(SecretId=secret_arn)
    creds = json.loads(secret['SecretString'])
    return creds['username'], creds['password']


def split_into_chunks(text, chunk_size=CHUNK_SIZE):
    words = text.split()
    chunks = []
    for i in range(0, len(words), chunk_size):
        chunk_text = " ".join(words[i:i + chunk_size])
        chunks.append((i // chunk_size, chunk_text))
    return chunks


def get_embedding(text):
    response = bedrock_client.invoke_model(
        ModelId="amazon.titan-embed-text-v1",
        Body=json.dumps({"text": text}),
        ContentType="application/json"
    )
    result = json.loads(response['Body'].read())
    return result['embedding']


def lambda_handler(event, context):
    username, password = get_db_credentials(DB_SECRET_ARN)

    conn = psycopg2.connect(
        host=DB_HOST,
        port=DB_PORT,
        dbname=DB_NAME,
        user=username,
        password=password
    )
    cur = conn.cursor()

    # Process each file uploaded to S3
    for record in event['Records']:
        bucket = record['s3']['bucket']['name']
        key = record['s3']['object']['key']

        # Read document from S3
        s3_obj = s3_client.get_object(Bucket=bucket, Key=key)
        document_text = s3_obj['Body'].read().decode('utf-8')

        # Extract document metadata from key or event
        document_id = key.split('/')[-1].split('.')[0]  # e.g., "doc_123"
        tenant_id = "tenant_001"  # Replace with dynamic value if available
        user_id = "user_001"      # Replace with dynamic value if available
        project_id = "project_001"
        thread_id = "thread_001"

        # Insert document row with in-progress status
        cur.execute("""
            INSERT INTO documents (document_id, tenant_id, user_id, project_id, thread_id, status)
            VALUES (%s, %s, %s, %s, %s, 'in-progress')
            ON CONFLICT (document_id) DO UPDATE SET status='in-progress', updated_at=NOW()
        """, (document_id, tenant_id, user_id, project_id, thread_id))

        conn.commit()

        # Split document into chunks
        chunks = split_into_chunks(document_text)

        # Insert chunks with embeddings
        for chunk_index, chunk_text in chunks:
            embedding_vector = get_embedding(chunk_text)
            metadata = {
                "tenant_id": tenant_id,
                "user_id": user_id,
                "project_id": project_id,
                "thread_id": thread_id,
                "chunk_index": chunk_index
            }

            cur.execute("""
                INSERT INTO document_chunks (document_id, chunk_index, chunk_text, embedding_vector, metadata, status)
                VALUES (%s, %s, %s, %s, %s, 'completed')
            """, (document_id, chunk_index, chunk_text, embedding_vector, json.dumps(metadata)))

        # Update document status to completed
        cur.execute("""
            UPDATE documents SET status='completed', updated_at=NOW()
            WHERE document_id=%s
        """, (document_id,))

        conn.commit()

    cur.close()
    conn.close()

    return {"statusCode": 200, "body": "Document ingested and indexed successfully"}
