import os
import psycopg2
import json
import boto3
import urllib3
import traceback
 
#----------------- Environment Variables ------------------
 
DB_HOST = os.environ['DB_HOST']
DB_PORT = os.environ['DB_PORT']
DB_NAME = os.environ['DB_NAME']
DB_SECRET_ARN = os.environ['DB_SECRET_ARN']
REGION = os.environ.get('REGION', 'us-east-1')
RESET_DB = os.environ.get('RESET_DB', 'false').lower() == 'true'

http = urllib3.PoolManager()
#------------------ Helper: CloudFormation response ------------------
 
def send_cfn_response(event, context, status, data=None, reason=None):
    """Send SUCCESS or FAILED signal back to CloudFormation."""
    
    response_body = {
        'Status': status,
        'Reason': reason or f'See details in CloudWatch Log Stream: {context.log_stream_name}',
        'PhysicalResourceId': 'InitDBLambdaResource',
        'StackId': event.get('StackId'),
        'RequestId': event.get('RequestId'),
        'LogicalResourceId': event.get('LogicalResourceId'),
        'Data': data or {}
    }
    json_response_body = json.dumps(response_body)
    try:
        http.request('PUT', event['ResponseURL'], body=json_response_body)
        print(f"Sending CloudFormation response: {response_body['Status']} successfully")
    except Exception as e:
        print(f"[WARN] Failed to send CloudFormation response: {e}")
 
#------------------ Secrets Manager ------------------
 
def get_db_credentials(secret_arn):
    client = boto3.client('secretsmanager', region_name=REGION)
    secret = client.get_secret_value(SecretId=secret_arn)
    creds = json.loads(secret['SecretString'])
    return creds['username'], creds['password']
 
#------------------ Lambda Handler ------------------
def lambda_handler(event, context):
    result = {
    "message": "",
    "tables_reset": RESET_DB,
    "unique_indexes": [],
    "table_counts": {}
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
    
        # 1️⃣ Enable Extensions
        cur.execute("CREATE EXTENSION IF NOT EXISTS vector;")
        cur.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto;")
        conn.commit()
    
        # 2️⃣ Optional Reset
        if RESET_DB:
            print("Resetting DB schema as RESET_DB=True ...")
            cur.execute("""
                DROP TABLE IF EXISTS document_chunks CASCADE;
                DROP TABLE IF EXISTS metadata CASCADE;
                DROP TABLE IF EXISTS documents CASCADE;
            """)
            conn.commit()
    
        # 3️⃣ Create Tables
        cur.execute("""
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
    
        CREATE TABLE IF NOT EXISTS metadata (
            metadata_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            document_id UUID REFERENCES documents(document_id) ON DELETE CASCADE,
            tenant_id TEXT NOT NULL,
            user_id TEXT NOT NULL,
            project_id TEXT,
            thread_id TEXT,
            metadata JSONB DEFAULT '{}'::jsonb,
            created_at TIMESTAMP DEFAULT NOW(),
            updated_at TIMESTAMP DEFAULT NOW()
        );
    
        CREATE TABLE IF NOT EXISTS document_chunks (
            chunk_id SERIAL PRIMARY KEY,
            document_id UUID NOT NULL REFERENCES documents(document_id) ON DELETE CASCADE,
            chunk_index INT NOT NULL,
            chunk_text TEXT NOT NULL,
            embedding_vector VECTOR(1536),
            metadata JSONB DEFAULT '{}'::jsonb,
            status TEXT DEFAULT 'not-started',
            created_at TIMESTAMP DEFAULT NOW(),
            updated_at TIMESTAMP DEFAULT NOW()
        );
        """)
        conn.commit()
    
        # 4️⃣ Create Indexes
        cur.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS idx_unique_doc_chunk
            ON document_chunks(document_id, chunk_index);
    
        CREATE INDEX IF NOT EXISTS idx_document_chunks_textsearch
            ON document_chunks USING gin (to_tsvector('simple', chunk_text));
    
        CREATE INDEX IF NOT EXISTS idx_document_chunks_vector_l2
            ON document_chunks USING ivfflat (embedding_vector vector_l2_ops) WITH (lists = 100);
    
        CREATE INDEX IF NOT EXISTS idx_document_chunks_vector_cosine
            ON document_chunks USING hnsw (embedding_vector vector_cosine_ops);
        """)
        conn.commit()
    
        # 5️⃣ Verify Unique Indexes
        cur.execute("""
            SELECT indexname
            FROM pg_indexes
            WHERE tablename = 'document_chunks'
            AND indexdef LIKE '%UNIQUE%';
        """)
        result["unique_indexes"] = [r[0] for r in cur.fetchall()]
    
        # 6️⃣ Row Counts
        for t in ['documents', 'metadata', 'document_chunks']:
            cur.execute(f"SELECT COUNT(*) FROM {t};")
            result["table_counts"][t] = cur.fetchone()[0]
    
        result["message"] = "Aurora Knowledge Base initialization complete ✅"
        print(json.dumps(result, indent=2))
        conn.commit()
    
        # Notify CloudFormation success
        send_cfn_response(event, context, "SUCCESS", result)
        return {"statusCode": 200, "body": json.dumps(result)}
    
    except Exception as e:
        traceback.print_exc()
        result["message"] = f"[ERROR] {str(e)}"
        send_cfn_response(event, context, "FAILED", result, reason=str(e))
        return {"statusCode": 500, "body": json.dumps(result)}
    
    finally:
        try:
            cur.close()
            conn.close()
        except Exception:
            pass