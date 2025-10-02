import os
import json
import psycopg2
import boto3
from psycopg2.extras import RealDictCursor

DB_HOST = os.environ['DB_HOST']
DB_PORT = os.environ['DB_PORT']
DB_NAME = os.environ['DB_NAME']
DB_SECRET_ARN = os.environ['DB_SECRET_ARN']
REGION = os.environ.get('REGION', 'ap-south-1')

secrets_client = boto3.client('secretsmanager', region_name=REGION)
bedrock_client = boto3.client('bedrock', region_name=REGION)

def get_db_credentials(secret_arn):
    secret = secrets_client.get_secret_value(SecretId=secret_arn)
    creds = json.loads(secret['SecretString'])
    return creds['username'], creds['password']

def get_query_embedding(query_text):
    """Generate embedding using Bedrock Titan model"""
    response = bedrock_client.invoke_model(
        ModelId='amazon.titan-embed-text-v2',
        Body=json.dumps({"text": query_text}),
        ContentType='application/json'
    )
    embedding = json.loads(response['Body'].read())['embedding']
    return embedding

def lambda_handler(event, context):
    """
    Expected input:
    {
      "query_text": "search text",
      "top_k": 5,
      "metadata_filters": {"tenant_id": "tenant_123"}
    }
    """
    username, password = get_db_credentials(DB_SECRET_ARN)
    conn = psycopg2.connect(
        host=DB_HOST, port=DB_PORT, dbname=DB_NAME, user=username, password=password
    )
    cur = conn.cursor(cursor_factory=RealDictCursor)

    query_text = event.get("query_text")
    top_k = event.get("top_k", 5)
    filters = event.get("metadata_filters", {})

    sql = "SELECT id, tenant_id, user_id, document_id, project_id, thread_id, chunk_text, created_at"
    sql += " FROM document_chunks WHERE 1=1"
    params = []

    # Add metadata filters
    for key, value in filters.items():
        if key.lower() in ['tenant_id','user_id','document_id','project_id','thread_id']:
            sql += f" AND {key.lower()} = %s"
            params.append(value)

    # Vector similarity search
    if query_text:
        query_embedding = get_query_embedding(query_text)
        sql += " ORDER BY embedding <-> %s LIMIT %s"
        params.extend([query_embedding, top_k])
    else:
        sql += " LIMIT %s"
        params.append(top_k)

    cur.execute(sql, params)
    rows = cur.fetchall()
    cur.close()
    conn.close()

    return {
        "statusCode": 200,
        "body": json.dumps(rows, default=str)
    }
