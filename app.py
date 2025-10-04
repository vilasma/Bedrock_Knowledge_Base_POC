from fastapi import FastAPI, Query
from pydantic import BaseModel
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
TOP_K = int(os.environ.get('TOP_K', 5))

# ------------------ Initialize clients ------------------
secrets_client = boto3.client('secretsmanager', region_name=REGION)
bedrock_client = boto3.client('bedrock-runtime', region_name=REGION)

# ------------------ Database ------------------
def get_db_credentials(secret_arn):
    secret = secrets_client.get_secret_value(SecretId=secret_arn)
    creds = json.loads(secret['SecretString'])
    return creds['username'], creds['password']

def get_db_connection():
    username, password = get_db_credentials(DB_SECRET_ARN)
    conn = psycopg2.connect(
        host=DB_HOST,
        port=DB_PORT,
        dbname=DB_NAME,
        user=username,
        password=password
    )
    return conn

# ------------------ Generate embedding for query ------------------
def generate_embedding(text):
    response = bedrock_client.invoke_model(
        ModelId="amazon.titan-embed-text-v1",
        Body=json.dumps({"text": text}),
        ContentType="application/json"
    )
    result = json.loads(response['Body'].read())
    return result['embedding']

# ------------------ FastAPI app ------------------
app = FastAPI(title="RAG Query API")

class QueryRequest(BaseModel):
    query: str
    tenant_id: str = None
    document_ids: list[str] = None
    top_k: int = TOP_K

@app.post("/query")
def query_documents(req: QueryRequest):
    conn = get_db_connection()
    cur = conn.cursor()

    # Generate embedding for query
    query_embedding = generate_embedding(req.query)

    # Build dynamic SQL with optional metadata filters
    sql = """
    SELECT document_id, chunk_index, chunk_text, metadata,
           embedding_vector <#> %s AS distance
    FROM document_chunks
    WHERE status='completed'
    """
    params = [query_embedding]

    if req.tenant_id:
        sql += " AND metadata->>'tenant_id'=%s"
        params.append(req.tenant_id)
    if req.document_ids:
        sql += " AND document_id = ANY(%s)"
        params.append(req.document_ids)

    sql += " ORDER BY distance ASC LIMIT %s"
    params.append(req.top_k)

    cur.execute(sql, params)
    results = cur.fetchall()
    conn.close()

    # Format results
    output = [
        {
            "document_id": r[0],
            "chunk_index": r[1],
            "chunk_text": r[2],
            "metadata": r[3],
            "similarity_score": float(r[4])
        }
        for r in results
    ]

    return {"results": output}
