import os
import json
import boto3
import psycopg2

DB_HOST = os.environ['DB_HOST']
DB_PORT = os.environ['DB_PORT']
DB_NAME = os.environ['DB_NAME']
DB_SECRET_ARN = os.environ['DB_SECRET_ARN']
REGION = os.environ.get('REGION', 'us-east-1')
TOP_K = int(os.environ.get('TOP_K', 5))

bedrock_client = boto3.client('bedrock-runtime', region_name=REGION)
secrets_client = boto3.client('secretsmanager', region_name=REGION)

def get_db_credentials(secret_arn):
    secret = secrets_client.get_secret_value(SecretId=secret_arn)
    creds = json.loads(secret['SecretString'])
    return creds['username'], creds['password']

def get_db_connection():
    username, password = get_db_credentials(DB_SECRET_ARN)
    return psycopg2.connect(
        host=DB_HOST, port=DB_PORT, dbname=DB_NAME,
        user=username, password=password
    )

def get_query_embedding(query_text):
    response = bedrock_client.invoke_model(
        modelId="amazon.titan-embed-text-v1",
        body=json.dumps({"inputText": query_text}),
        contentType="application/json",
        accept="application/json"
    )
    result = json.loads(response['body'].read())
    return result['embedding']

def lambda_handler(event, context):
    query_text = event.get("query", "Default query")
    filters = event.get("filters", {})

    conn = get_db_connection()
    cur = conn.cursor()

    query_embedding = get_query_embedding(query_text)

    sql = """
        SELECT chunk_text, metadata,
               embedding_vector <#> %s AS cosine_distance
        FROM document_chunks
        WHERE status='completed'
    """
    params = [query_embedding]

    if "document_ids" in filters:
        sql += " AND document_id = ANY(%s)"
        params.append(filters["document_ids"])
    if "tenant_id" in filters:
        sql += " AND metadata->>'tenant_id' = %s"
        params.append(filters["tenant_id"])
    if "project_id" in filters:
        sql += " AND metadata->>'project_id' = %s"
        params.append(filters["project_id"])

    sql += " ORDER BY cosine_distance ASC LIMIT %s"
    params.append(TOP_K)

    cur.execute(sql, params)
    results = cur.fetchall()

    response_data = [{"chunk_text": c, "metadata": m, "similarity": 1 - d} for c, m, d in results]

    cur.close()
    conn.close()

    return {"statusCode": 200, "body": json.dumps(response_data)}
