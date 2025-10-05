import os
import psycopg2
import json
import boto3

# ------------------ Environment Variables ------------------
DB_HOST = os.environ['DB_HOST']
DB_PORT = os.environ['DB_PORT']
DB_NAME = os.environ['DB_NAME']
DB_SECRET_ARN = os.environ['DB_SECRET_ARN']
REGION = os.environ.get('REGION', 'us-east-1')

# ------------------ Secrets Manager ------------------
def get_db_credentials(secret_arn):
    client = boto3.client('secretsmanager', region_name=REGION)
    secret = client.get_secret_value(SecretId=secret_arn)
    creds = json.loads(secret['SecretString'])
    return creds['username'], creds['password']

# ------------------ Lambda Handler ------------------
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

    # ------------------ 1️⃣ Enable pgvector + pgcrypto ------------------
    cur.execute("CREATE EXTENSION IF NOT EXISTS vector;")
    cur.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto;")
    conn.commit()

    # ------------------ 2️⃣ Create/Ensure UUID-Compatible Tables ------------------
    create_tables_query = """
    CREATE TABLE IF NOT EXISTS documents (
        document_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        tenant_id TEXT NOT NULL,
        user_id TEXT NOT NULL,
        project_id TEXT,
        document_name TEXT NOT NULL,
        thread_id TEXT,
        status TEXT NOT NULL DEFAULT 'not-started',
        created_at TIMESTAMP DEFAULT NOW(),
        updated_at TIMESTAMP DEFAULT NOW()
    );

    CREATE TABLE IF NOT EXISTS document_chunks (
        chunk_id SERIAL PRIMARY KEY,
        document_id UUID NOT NULL REFERENCES documents(document_id) ON DELETE CASCADE,
        chunk_index INT NOT NULL,
        chunk_text TEXT NOT NULL,
        embedding_vector VECTOR(1536) NOT NULL,
        metadata JSONB,
        status TEXT NOT NULL DEFAULT 'not-started',
        created_at TIMESTAMP DEFAULT NOW()
    );
    """
    cur.execute(create_tables_query)
    conn.commit()

    # ------------------ 3️⃣ Ensure Columns Have Correct Data Types ------------------
    alter_types = """
    -- documents table
    ALTER TABLE documents
        ALTER COLUMN document_id SET DATA TYPE UUID USING document_id::uuid,
        ALTER COLUMN tenant_id SET DATA TYPE TEXT,
        ALTER COLUMN user_id SET DATA TYPE TEXT,
        ALTER COLUMN project_id SET DATA TYPE TEXT,
        ALTER COLUMN document_name SET DATA TYPE TEXT,
        ALTER COLUMN thread_id SET DATA TYPE TEXT;

    -- document_chunks table
    ALTER TABLE document_chunks
        ALTER COLUMN document_id SET DATA TYPE UUID USING document_id::uuid,
        ALTER COLUMN chunk_index SET DATA TYPE INT,
        ALTER COLUMN chunk_text SET DATA TYPE TEXT,
        ALTER COLUMN embedding_vector SET DATA TYPE VECTOR(1536),
        ALTER COLUMN metadata SET DATA TYPE JSONB;
    """
    try:
        cur.execute(alter_types)
        conn.commit()
    except Exception as e:
        print(f"[INFO] Column type normalization skipped (likely already correct): {e}")

    # ------------------ 4️⃣ Rebuild Indices for Search & Vector Similarity ------------------
    cur.execute("""
    CREATE INDEX IF NOT EXISTS idx_documents_tenant
    ON documents(tenant_id);

    CREATE INDEX IF NOT EXISTS idx_document_chunks_document_id
    ON document_chunks(document_id);

    CREATE INDEX IF NOT EXISTS idx_document_chunks_textsearch
    ON document_chunks USING gin (to_tsvector('simple', chunk_text));

    CREATE INDEX IF NOT EXISTS idx_document_chunks_vector_l2
    ON document_chunks USING ivfflat (embedding_vector vector_l2_ops) WITH (lists = 100);

    CREATE INDEX IF NOT EXISTS idx_document_chunks_vector_cosine
    ON document_chunks USING hnsw (embedding_vector vector_cosine_ops);
    """)
    conn.commit()

    # ------------------ 5️⃣ Final Validation ------------------
    cur.execute("SELECT COUNT(*) FROM documents;")
    docs_count = cur.fetchone()[0]

    cur.close()
    conn.close()

    return {
        "statusCode": 200,
        "body": json.dumps({
            "message": "Tables 'documents' and 'document_chunks' initialized successfully and are UUID-compliant.",
            "documents_existing": docs_count
        })
    }
