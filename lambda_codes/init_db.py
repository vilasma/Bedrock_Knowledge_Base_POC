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

        # Check pgvector version (must be >= 0.5.0 for HNSW support)
        cur.execute("SELECT extversion FROM pg_extension WHERE extname = 'vector';")
        vector_version = cur.fetchone()[0]
        logger.info(f"pgvector version: {vector_version}")

        # Verify HNSW support
        if vector_version < '0.5.0':
            raise Exception(f"pgvector version {vector_version} does not support HNSW. Requires >= 0.5.0")

        # Optional reset
        if RESET_DB:
            cur.execute("""
                DROP TABLE IF EXISTS query_results CASCADE;
                DROP TABLE IF EXISTS query_history CASCADE;
                DROP TABLE IF EXISTS failed_chunks CASCADE;
                DROP TABLE IF EXISTS bedrock_kb_documents CASCADE;
                DROP TABLE IF EXISTS metadata CASCADE;
                DROP TABLE IF EXISTS documents CASCADE;
                DROP TABLE IF EXISTS document_chunks CASCADE;
            """)

        # ---------------- Create Tables ----------------
        cur.execute("""
            -- ============================================================
            -- BEDROCK KNOWLEDGE BASE TABLE (MANAGED BY BEDROCK)
            -- ============================================================
            -- This table is the PRIMARY vector store for Bedrock KB
            -- Bedrock will populate this table when ingesting from S3
            -- DO NOT manually insert/update - Bedrock manages this
            -- ============================================================
            CREATE TABLE IF NOT EXISTS bedrock_kb_documents (
                id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                embedding vector(1536) NOT NULL,
                chunks TEXT NOT NULL,
                metadata JSONB DEFAULT '{}'::jsonb
            );

            -- HNSW index for vector similarity search (REQUIRED by Bedrock)
            CREATE INDEX IF NOT EXISTS bedrock_kb_documents_embedding_idx
            ON bedrock_kb_documents USING hnsw (embedding vector_cosine_ops);


            -- ============================================================
            -- TRACKING TABLES (MANAGED BY LAMBDA)
            -- ============================================================

            -- ---------------- DOCUMENTS TABLE ----------------
            -- Tracks document ingestion status and metadata
            CREATE TABLE IF NOT EXISTS documents (
                document_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                document_name TEXT NOT NULL,
                s3_key TEXT UNIQUE NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                ingestion_job_id TEXT,
                chunk_count INT DEFAULT 0,
                error_message TEXT,
                tenant_id TEXT NOT NULL,
                user_id TEXT NOT NULL,
                project_id TEXT,
                thread_id TEXT,
                created_at TIMESTAMP DEFAULT NOW(),
                updated_at TIMESTAMP DEFAULT NOW()
            );

            CREATE INDEX IF NOT EXISTS idx_documents_status ON documents(status);
            CREATE INDEX IF NOT EXISTS idx_documents_s3_key ON documents(s3_key);
            CREATE INDEX IF NOT EXISTS idx_documents_tenant_user ON documents(tenant_id, user_id);


            -- ---------------- DOCUMENT CHUNKS TABLE ----------------
            -- Stores individual chunks with embeddings from each document
            -- This is SEPARATE from bedrock_kb_documents (which Bedrock manages)
            -- We populate this table for direct querying and tracking
            CREATE TABLE IF NOT EXISTS document_chunks (
                chunk_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                document_id UUID REFERENCES documents(document_id) ON DELETE CASCADE,
                chunk_index INT NOT NULL,
                chunk_text TEXT NOT NULL,
                embedding vector(1536),
                metadata JSONB DEFAULT '{}'::jsonb,
                status TEXT NOT NULL DEFAULT 'pending',
                created_at TIMESTAMP DEFAULT NOW(),
                updated_at TIMESTAMP DEFAULT NOW(),
                UNIQUE(document_id, chunk_index)
            );

            CREATE INDEX IF NOT EXISTS idx_document_chunks_document_id ON document_chunks(document_id);
            CREATE INDEX IF NOT EXISTS idx_document_chunks_status ON document_chunks(status);

            -- HNSW index for fast vector similarity search
            CREATE INDEX IF NOT EXISTS idx_document_chunks_embedding_hnsw
            ON document_chunks USING hnsw (embedding vector_cosine_ops);


            -- ---------------- METADATA TABLE ----------------
            -- Extended metadata for documents (extra custom fields)
            CREATE TABLE IF NOT EXISTS metadata (
                metadata_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                document_id UUID REFERENCES documents(document_id) ON DELETE CASCADE,
                metadata_key TEXT NOT NULL,
                metadata_value TEXT,
                created_at TIMESTAMP DEFAULT NOW(),
                UNIQUE(document_id, metadata_key)
            );

            CREATE INDEX IF NOT EXISTS idx_metadata_document_id ON metadata(document_id);
            CREATE INDEX IF NOT EXISTS idx_metadata_key ON metadata(metadata_key);


            -- ---------------- QUERY HISTORY TABLE ----------------
            -- Tracks all queries for analytics and auditing
            CREATE TABLE IF NOT EXISTS query_history (
                query_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                query_text TEXT NOT NULL,
                tenant_id TEXT,
                user_id TEXT,
                top_k INT DEFAULT 5,
                execution_time_ms INT,
                result_count INT,
                query_timestamp TIMESTAMP DEFAULT NOW()
            );

            CREATE INDEX IF NOT EXISTS idx_query_history_timestamp ON query_history(query_timestamp DESC);
            CREATE INDEX IF NOT EXISTS idx_query_history_tenant_user ON query_history(tenant_id, user_id);


            -- ---------------- QUERY RESULTS TABLE ----------------
            -- Stores individual query results with similarity scores
            -- This is where similarity_score, chunk_text, query_text get populated
            CREATE TABLE IF NOT EXISTS query_results (
                result_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                query_id UUID REFERENCES query_history(query_id) ON DELETE CASCADE,
                document_id UUID REFERENCES documents(document_id) ON DELETE SET NULL,
                chunk_id UUID,
                chunk_index INT,
                chunk_text TEXT NOT NULL,
                similarity_score FLOAT NOT NULL,
                result_rank INT NOT NULL,
                s3_location TEXT,
                metadata JSONB DEFAULT '{}'::jsonb,
                created_at TIMESTAMP DEFAULT NOW()
            );

            CREATE INDEX IF NOT EXISTS idx_query_results_query_id ON query_results(query_id);
            CREATE INDEX IF NOT EXISTS idx_query_results_document_id ON query_results(document_id);
            CREATE INDEX IF NOT EXISTS idx_query_results_score ON query_results(similarity_score DESC);
            CREATE INDEX IF NOT EXISTS idx_query_results_rank ON query_results(result_rank);


            -- ---------------- FAILED CHUNKS TABLE ----------------
            -- Tracks chunks that failed processing for debugging
            CREATE TABLE IF NOT EXISTS failed_chunks (
                failure_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                document_id UUID REFERENCES documents(document_id) ON DELETE CASCADE,
                chunk_index INT,
                chunk_text TEXT,
                error_reason TEXT,
                created_at TIMESTAMP DEFAULT NOW()
            );

            CREATE INDEX IF NOT EXISTS idx_failed_chunks_document_id ON failed_chunks(document_id);


            -- ============================================================
            -- VIEWS FOR EASY QUERYING
            -- ============================================================

            -- View: Document Summary with Query Stats
            CREATE OR REPLACE VIEW document_query_stats AS
            SELECT
                d.document_id,
                d.document_name,
                d.s3_key,
                d.status,
                d.tenant_id,
                d.user_id,
                d.chunk_count,
                d.created_at,
                COUNT(DISTINCT qr.query_id) as times_retrieved,
                AVG(qr.similarity_score) as avg_similarity_score,
                MAX(qr.similarity_score) as max_similarity_score
            FROM documents d
            LEFT JOIN query_results qr ON d.document_id = qr.document_id
            GROUP BY d.document_id, d.document_name, d.s3_key, d.status,
                     d.tenant_id, d.user_id, d.chunk_count, d.created_at;

            -- View: Query Results with Document Info
            CREATE OR REPLACE VIEW query_results_detailed AS
            SELECT
                qh.query_id,
                qh.query_text,
                qh.query_timestamp,
                qh.tenant_id,
                qh.user_id,
                qr.result_rank,
                qr.similarity_score,
                qr.chunk_text,
                qr.chunk_index,
                d.document_id,
                d.document_name,
                d.s3_key
            FROM query_history qh
            JOIN query_results qr ON qh.query_id = qr.query_id
            LEFT JOIN documents d ON qr.document_id = d.document_id
            ORDER BY qh.query_timestamp DESC, qr.result_rank ASC;
        """)

        conn.commit()
        logger.info("✅ Aurora DB initialized successfully with all tables and indexes")

        # Row counts
        tables = ['bedrock_kb_documents', 'documents', 'document_chunks', 'metadata', 'query_history', 'query_results', 'failed_chunks']
        for t in tables:
            cur.execute(f"SELECT COUNT(*) FROM {t};")
            result["table_counts"][t] = cur.fetchone()[0]

        result["message"] = "Aurora Knowledge Base initialized successfully ✅"
        result["pgvector_version"] = vector_version
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
