import os
import json
import logging
import boto3
import psycopg2
import urllib3
import traceback

# ---------------- Config ----------------
DB_HOST = os.environ['DB_HOST']
DB_PORT = int(os.environ.get('DB_PORT', 5432))
DB_NAME = os.environ['DB_NAME']
DB_SECRET_ARN = os.environ['DB_SECRET_ARN']
RESET_DB = os.environ.get('RESET_DB', 'false').lower() == 'true'

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

boto_session = boto3.session.Session()
secrets_client = boto_session.client('secretsmanager')

http = urllib3.PoolManager()


# ---------------- CFN Response ----------------
def send_cfn_response(event, context, status, reason=None):
    if "ResponseURL" not in event:
        print(f"[CFN] Not CFN invocation. Status: {status}, Reason: {reason}")
        return
    body = {
        "Status": status,
        "Reason": reason or f"See CloudWatch Log Stream: {context.log_stream_name}",
        "PhysicalResourceId": context.log_stream_name,
        "StackId": event.get("StackId"),
        "RequestId": event.get("RequestId"),
        "LogicalResourceId": event.get("LogicalResourceId"),
        "Data": {}
    }
    try:
        http.request(
            "PUT",
            event["ResponseURL"],
            body=json.dumps(body),
            headers={
                "Content-Type": "application/json",
                "Content-Length": str(len(json.dumps(body)))
            }
        )
    except Exception as e:
        print(f"[ERROR] Failed CFN response: {e}")


# ---------------- DB Helpers ----------------
def get_db_credentials(secret_arn):
    secret = secrets_client.get_secret_value(SecretId=secret_arn)
    creds = json.loads(secret['SecretString'])
    return creds['username'], creds['password']


def get_db_conn():
    username, password = get_db_credentials(DB_SECRET_ARN)
    return psycopg2.connect(
        host=DB_HOST,
        port=DB_PORT,
        dbname=DB_NAME,
        user=username,
        password=password,
        connect_timeout=10
    )


# ---------------- Lambda Handler ----------------
def lambda_handler(event, context):
    result = {"table_counts": {}}
    conn = None
    cur = None

    try:
        conn = get_db_conn()
        cur = conn.cursor()

        # Enable extensions
        cur.execute("CREATE EXTENSION IF NOT EXISTS vector;")
        cur.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto;")

        # Optional reset
        if RESET_DB:
            cur.execute("""
                DROP TABLE IF EXISTS document_chunks CASCADE;
                DROP TABLE IF EXISTS metadata CASCADE;
                DROP TABLE IF EXISTS documents CASCADE;
            """)

        # ---------------- Documents Table ----------------
        cur.execute("""
            -- ---------------- DOCUMENTS TABLE ----------------
            CREATE TABLE IF NOT EXISTS documents (
                document_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                document_name TEXT NOT NULL,
                s3_key TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                created_at TIMESTAMP DEFAULT NOW(),
                updated_at TIMESTAMP DEFAULT NOW()
            );

            -- Index on status for quick queries
            CREATE INDEX IF NOT EXISTS idx_documents_status ON documents(status);


            -- ---------------- METADATA TABLE ----------------
            CREATE TABLE IF NOT EXISTS metadata (
                metadata_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                document_id UUID REFERENCES documents(document_id) ON DELETE CASCADE,
                tenant_id TEXT NOT NULL,
                user_id TEXT NOT NULL,
                project_id TEXT,
                thread_id TEXT,
                extra_metadata JSONB DEFAULT '{}'::jsonb,
                created_at TIMESTAMP DEFAULT NOW(),
                updated_at TIMESTAMP DEFAULT NOW()
            );

            -- Index on document_id for fast lookup
            CREATE INDEX IF NOT EXISTS idx_metadata_document_id ON metadata(document_id);


            -- ---------------- DOCUMENT_CHUNKS TABLE ----------------
            CREATE TABLE IF NOT EXISTS document_chunks (
                chunk_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                document_id UUID REFERENCES documents(document_id) ON DELETE CASCADE,
                chunk_index INT NOT NULL,
                chunk_text TEXT NOT NULL,
                embedding_vector vector(1536) NOT NULL, -- Adjust VECTOR_DIM if changed
                chunk_hash TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                similarity_score FLOAT,
                query_text TEXT,
                metadata JSONB DEFAULT '{}'::jsonb,
                created_at TIMESTAMP DEFAULT NOW(),
                updated_at TIMESTAMP DEFAULT NOW(),
                CONSTRAINT unique_document_chunk_index UNIQUE(document_id, chunk_index),  -- Required for Bedrock ON CONFLICT
                CONSTRAINT unique_document_chunk_hash UNIQUE(document_id, chunk_hash)  -- Prevents duplicate chunks
            );

            -- Indexes for faster similarity queries
            CREATE INDEX IF NOT EXISTS idx_chunks_document_id ON document_chunks(document_id);
            CREATE INDEX IF NOT EXISTS idx_chunks_similarity ON document_chunks(similarity_score);

            -- HNSW index for vector similarity search (required by AWS Bedrock)
            CREATE INDEX IF NOT EXISTS idx_chunks_vector ON document_chunks USING hnsw (embedding_vector vector_cosine_ops);

            -- GIN index for full-text search (required by AWS Bedrock)
            CREATE INDEX IF NOT EXISTS idx_chunks_text_gin ON document_chunks USING gin (to_tsvector('simple', chunk_text));
                    
            CREATE TABLE IF NOT EXISTS failed_chunks (
                chunk_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                document_id UUID REFERENCES documents(document_id) ON DELETE CASCADE,
                chunk_index INT,
                chunk_text TEXT,
                reason TEXT,
                created_at TIMESTAMP DEFAULT NOW()
            );
        """)

        conn.commit()
        logger.info("Aurora DB initialized successfully with all tables and indexes")

        # Row counts
        for t in ['documents', 'metadata', 'document_chunks']:
            cur.execute(f"SELECT COUNT(*) FROM {t};")
            result["table_counts"][t] = cur.fetchone()[0]

        result["message"] = "Aurora Knowledge Base initialized ✅"
        logger.info(json.dumps(result, indent=2))
        send_cfn_response(event, context, "SUCCESS", json.dumps(result))
        return {"statusCode": 200, "body": json.dumps(result)}

    except Exception as e:
        traceback.print_exc()
        result["message"] = f"[ERROR] {str(e)}"
        send_cfn_response(event, context, "FAILED", json.dumps(result))
        return {"statusCode": 500, "body": json.dumps(result)}

    finally:
        if cur:
            cur.close()
        if conn:
            conn.close()
