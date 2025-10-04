import os
import psycopg2
import json
import boto3

DB_HOST = os.environ['DB_HOST']
DB_PORT = os.environ['DB_PORT']
DB_NAME = os.environ['DB_NAME']
DB_SECRET_ARN = os.environ['DB_SECRET_ARN']
REGION = os.environ.get('REGION', 'us-east-1')

def get_db_credentials(secret_arn):
    client = boto3.client('secretsmanager', region_name=REGION)
    secret = client.get_secret_value(SecretId=secret_arn)
    creds = json.loads(secret['SecretString'])
    return creds['username'], creds['password']

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

    create_table_query = """
    -- Create extension for vector operations
    CREATE EXTENSION IF NOT EXISTS vector;
    CREATE EXTENSION IF NOT EXISTS pgcrypto;

    -- Create table for chunks (run once after DB is reachable)
    CREATE TABLE IF NOT EXISTS document_chunks (
        id SERIAL PRIMARY KEY,
        tenant_id VARCHAR(128),
        user_id VARCHAR(128),
        document_id UUID UNIQUE DEFAULT gen_random_uuid(),
        document_name VARCHAR(256),
        project_id VARCHAR(128),
        thread_id VARCHAR(128),
        chunk_text TEXT,
        embedding_vector vector(1536),
        metadata JSONB,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );

    -- Index for fast similarity search
    CREATE INDEX IF NOT EXISTS idx_document_embedding 
    ON document_chunks USING ivfflat (embedding_vector vector_l2_ops) 
    WITH (lists = 100);

    -- Index on document_id for filtering
    CREATE INDEX IF NOT EXISTS idx_document_id 
    ON document_chunks(document_id);

    -- Create GIN index for full-text search
    CREATE INDEX idx_document_chunks_chunk_text
    ON document_chunks
    USING gin (to_tsvector('simple', chunk_text));
    """

    cur.execute(create_table_query)
    conn.commit()
    cur.close()
    conn.close()

    return {"statusCode": 200, "body": "Table created successfully"}
