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
    result = {
        "message": "",
        "unique_constraints": [],
        "duplicates_removed": 0,
        "existing_documents": 0
    }

    try:
        username, password = get_db_credentials(DB_SECRET_ARN)

        conn = psycopg2.connect(
            host=DB_HOST,
            port=DB_PORT,
            dbname=DB_NAME,
            user=username,
            password=password
        )
        cur = conn.cursor()

        # 1️⃣ Enable extensions
        cur.execute("CREATE EXTENSION IF NOT EXISTS vector;")
        cur.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto;")
        conn.commit()

        # 2️⃣ Create tables (idempotent)
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
            document_name TEXT NOT NULL,
            embedding_vector VECTOR(1536) NOT NULL,
            metadata JSONB,
            status TEXT NOT NULL DEFAULT 'not-started',
            created_at TIMESTAMP DEFAULT NOW(),
            updated_at TIMESTAMP DEFAULT NOW()
        );
        """
        cur.execute(create_tables_query)
        conn.commit()

        # 3️⃣ Detect & remove duplicates
        cur.execute("""
            SELECT COUNT(*) 
            FROM (
                SELECT document_id, chunk_index, COUNT(*) 
                FROM document_chunks 
                GROUP BY document_id, chunk_index 
                HAVING COUNT(*) > 1
            ) dup;
        """)
        duplicates_found = cur.fetchone()[0]

        if duplicates_found > 0:
            delete_duplicates = """
            DELETE FROM document_chunks a
            USING document_chunks b
            WHERE a.chunk_id < b.chunk_id
              AND a.document_id = b.document_id
              AND a.chunk_index = b.chunk_index;
            """
            cur.execute(delete_duplicates)
            conn.commit()

        result["duplicates_removed"] = duplicates_found

        # 4️⃣ Ensure UNIQUE constraint
        add_constraint = """
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1
                FROM information_schema.table_constraints
                WHERE table_name = 'document_chunks'
                  AND constraint_type = 'UNIQUE'
                  AND constraint_name = 'document_chunks_unique'
            ) THEN
                ALTER TABLE document_chunks
                ADD CONSTRAINT document_chunks_unique
                UNIQUE (document_id, chunk_index);
            END IF;
        END $$;
        """
        cur.execute(add_constraint)
        conn.commit()

        # 5️⃣ Rebuild indices
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

        # 6️⃣ Fetch constraints & document count
        cur.execute("""
            SELECT constraint_name
            FROM information_schema.table_constraints
            WHERE table_name = 'document_chunks'
              AND constraint_type = 'UNIQUE';
        """)
        result["unique_constraints"] = [r[0] for r in cur.fetchall()]

        cur.execute("SELECT COUNT(*) FROM documents;")
        result["existing_documents"] = cur.fetchone()[0]

        result["message"] = "Aurora setup complete ✅ — constraints and indices verified."

    except Exception as e:
        result["message"] = f"[ERROR] {str(e)}"
        return {"statusCode": 500, "body": json.dumps(result)}
    
    finally:
        try:
            cur.close()
            conn.close()
        except:
            pass

    print(json.dumps(result, indent=2))
    return {"statusCode": 200, "body": json.dumps(result)}
