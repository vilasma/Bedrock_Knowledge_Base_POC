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

    # ===============================
    # 1️⃣ Enable extensions
    # ===============================
    cur.execute("CREATE EXTENSION IF NOT EXISTS vector;")
    cur.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto;")

    # ===============================
    # 2️⃣ Create tables if not exist
    # ===============================
    create_tables_query = """
    CREATE TABLE IF NOT EXISTS documents (
        document_id TEXT PRIMARY KEY,
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
        document_id TEXT NOT NULL REFERENCES documents(document_id),
        chunk_index INT NOT NULL,
        chunk_text TEXT NOT NULL,
        embedding_vector VECTOR(1536) NOT NULL,
        metadata JSONB,
        status TEXT NOT NULL DEFAULT 'not-started',
        created_at TIMESTAMP DEFAULT NOW()
    );

    CREATE INDEX IF NOT EXISTS idx_document_chunks_embedding
    ON document_chunks USING ivfflat (embedding_vector vector_l2_ops) WITH (lists = 100);

    CREATE INDEX IF NOT EXISTS idx_document_chunks_document_id
    ON document_chunks(document_id);

    CREATE INDEX IF NOT EXISTS idx_document_chunks_chunk_text
    ON document_chunks USING gin (to_tsvector('simple', chunk_text));

    CREATE INDEX IF NOT EXISTS idx_document_chunks_hnsw
    ON document_chunks USING hnsw (embedding_vector vector_cosine_ops);
    """
    cur.execute(create_tables_query)
    conn.commit()

    # ===============================
    # 3️⃣ Add UUID columns if missing
    # ===============================
    cur.execute("""
    DO $$
    BEGIN
        IF NOT EXISTS (SELECT 1 FROM information_schema.columns 
                       WHERE table_name='documents' AND column_name='document_id_new') THEN
            ALTER TABLE documents ADD COLUMN document_id_new uuid DEFAULT gen_random_uuid();
        END IF;

        IF NOT EXISTS (SELECT 1 FROM information_schema.columns 
                       WHERE table_name='document_chunks' AND column_name='document_id_new') THEN
            ALTER TABLE document_chunks ADD COLUMN document_id_new uuid;
        END IF;
    END
    $$;
    """)

    # ===============================
    # 4️⃣ Populate child UUIDs safely
    # ===============================
    cur.execute("""
    UPDATE document_chunks dc
    SET document_id_new = d.document_id_new
    FROM documents d
    WHERE dc.document_id_new IS NULL
      AND dc.document_id = d.document_id;
    """)

    # ===============================
    # 5️⃣ Drop old foreign key if exists
    # ===============================
    cur.execute("""
    DO $$
    BEGIN
        IF EXISTS (SELECT 1 FROM information_schema.table_constraints 
                   WHERE table_name='document_chunks' 
                     AND constraint_name='document_chunks_document_id_fkey') THEN
            ALTER TABLE document_chunks DROP CONSTRAINT document_chunks_document_id_fkey;
        END IF;
    END
    $$;
    """)

    # ===============================
    # 6️⃣ Drop old document_id columns safely
    # ===============================
    cur.execute("""
    DO $$
    BEGIN
        IF EXISTS (SELECT 1 FROM information_schema.columns 
                   WHERE table_name='documents' AND column_name='document_id') THEN
            ALTER TABLE documents DROP COLUMN document_id;
        END IF;

        IF EXISTS (SELECT 1 FROM information_schema.columns 
                   WHERE table_name='document_chunks' AND column_name='document_id') THEN
            ALTER TABLE document_chunks DROP COLUMN document_id;
        END IF;
    END
    $$;
    """)

    # ===============================
    # 7️⃣ Rename UUID columns
    # ===============================
    cur.execute("""
    ALTER TABLE documents RENAME COLUMN document_id_new TO document_id;
    ALTER TABLE document_chunks RENAME COLUMN document_id_new TO document_id;
    """)

    # ===============================
    # 8️⃣ Add PRIMARY KEY if missing
    # ===============================
    cur.execute("""
    DO $$
    BEGIN
        IF NOT EXISTS (SELECT 1 FROM information_schema.table_constraints 
                       WHERE table_name='documents' AND constraint_type='PRIMARY KEY') THEN
            ALTER TABLE documents ADD CONSTRAINT documents_pkey PRIMARY KEY (document_id);
        END IF;
    END
    $$;
    """)

    # ===============================
    # 9️⃣ Add FOREIGN KEY if missing
    # ===============================
    cur.execute("""
    DO $$
    BEGIN
        IF NOT EXISTS (SELECT 1 FROM information_schema.table_constraints 
                       WHERE table_name='document_chunks' 
                         AND constraint_type='FOREIGN KEY' 
                         AND constraint_name='document_chunks_document_id_fkey') THEN
            ALTER TABLE document_chunks
            ADD CONSTRAINT document_chunks_document_id_fkey
            FOREIGN KEY (document_id) REFERENCES documents(document_id);
        END IF;
    END
    $$;
    """)

    conn.commit()
    cur.close()
    conn.close()

    return {
        "statusCode": 200,
        "body": "Tables 'documents' and 'document_chunks' are now UUID-compliant, safe for repeated runs, and ready for Bedrock ingestion."
    }
