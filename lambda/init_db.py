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
    # Get DB credentials from Secrets Manager
    username, password = get_db_credentials(DB_SECRET_ARN)

    conn = psycopg2.connect(
        host=DB_HOST,
        port=DB_PORT,
        dbname=DB_NAME,
        user=username,
        password=password
    )
    cur = conn.cursor()

    create_tables_query = """
    -- Enable vector extension for embeddings
    CREATE EXTENSION IF NOT EXISTS vector;
    CREATE EXTENSION IF NOT EXISTS pgcrypto;

    -- Document-level table
    CREATE TABLE IF NOT EXISTS documents (
        document_id TEXT PRIMARY KEY,
        tenant_id TEXT NOT NULL,
        user_id TEXT NOT NULL,
        project_id TEXT,
        document_name TEXT NOT NULL,
        thread_id TEXT,
        status TEXT NOT NULL DEFAULT 'not-started', -- not-started | in-progress | completed
        created_at TIMESTAMP DEFAULT NOW(),
        updated_at TIMESTAMP DEFAULT NOW()
    );

    -- Chunk-level table
    CREATE TABLE IF NOT EXISTS document_chunks (
        chunk_id SERIAL PRIMARY KEY,
        document_id TEXT NOT NULL REFERENCES documents(document_id),
        chunk_index INT NOT NULL,
        chunk_text TEXT NOT NULL,
        embedding_vector VECTOR(1536) NOT NULL,
        metadata JSONB,
        status TEXT NOT NULL DEFAULT 'not-started', -- optional chunk-level status
        created_at TIMESTAMP DEFAULT NOW()
    );

    -- Indexes for efficient search
    CREATE INDEX IF NOT EXISTS idx_document_chunks_embedding
    ON document_chunks USING ivfflat (embedding_vector vector_l2_ops) WITH (lists = 100);

    CREATE INDEX IF NOT EXISTS idx_document_chunks_document_id
    ON document_chunks(document_id);

    CREATE INDEX IF NOT EXISTS idx_document_chunks_chunk_text
    ON document_chunks USING gin (to_tsvector('simple', chunk_text));

    -- HNSW index (for Bedrock compatibility)
    CREATE INDEX IF NOT EXISTS idx_document_chunks_hnsw
    ON document_chunks USING hnsw (embedding_vector vector_cosine_ops);
    """

    cur.execute(create_tables_query)
    conn.commit()
    cur.close()
    conn.close()

    return {"statusCode": 200, "body": "Tables 'documents' and 'document_chunks' created successfully"}
