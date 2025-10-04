import os
import json
import psycopg2
from psycopg2.extras import RealDictCursor
import boto3

DB_HOST = os.environ['DB_HOST']
DB_PORT = int(os.environ['DB_PORT'])
DB_NAME = os.environ['DB_NAME']
DB_SECRET_ARN = os.environ['DB_SECRET_ARN']
REGION = os.environ.get('REGION', 'us-east-1')

secrets_client = boto3.client('secretsmanager', region_name=REGION)
bedrock_client = boto3.client('bedrock-runtime', region_name=REGION)

def get_db_credentials(secret_arn):
    secret = secrets_client.get_secret_value(SecretId=secret_arn)
    creds = json.loads(secret['SecretString'])
    return creds['username'], creds['password']

def get_query_embedding(query_text):
    response = bedrock_client.invoke_model(
        ModelId='amazon.titan-embed-text-v1',
        Body=json.dumps({"inputText": query_text}),
        ContentType='application/json'
    )
    return json.loads(response['Body'].read())['embedding']

def lambda_handler(event, context):
    username, password = get_db_credentials(DB_SECRET_ARN)
    conn = psycopg2.connect(
        host=DB_HOST, port=DB_PORT, dbname=DB_NAME, user=username, password=password
    )
    cur = conn.cursor(cursor_factory=RealDictCursor)

    query_text = event.get("query_text")
    top_k = event.get("top_k", 5)
    filters = event.get("metadata_filters", {})

    sql = "SELECT tenant_id, user_id, document_id, document_name, project_id, thread_id, chunk_text, embedding_vector, metadata FROM document_chunks WHERE 1=1"
    params = []

    # Apply metadata filters
    for key, value in filters.items():
        sql += f" AND {key.lower()} = %s"
        params.append(value)

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

    return {"statusCode": 200, "body": json.dumps(rows, default=str)}
