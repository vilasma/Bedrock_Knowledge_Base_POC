import os
import psycopg2
import json
import boto3

DB_HOST = os.environ['DB_HOST']
DB_PORT = os.environ['DB_PORT']
DB_NAME = os.environ['DB_NAME']
DB_SECRET_ARN = os.environ['DB_SECRET_ARN']
REGION = os.environ.get('REGION', 'us-east-1')
TOP_K = int(os.environ.get('TOP_K', 5))

# Initialize boto3 clients
bedrock_client = boto3.client('bedrock-runtime', region_name=REGION)
secrets_client = boto3.client('secretsmanager', region_name=REGION)


def get_db_credentials(secret_arn):
    secret = secrets_client.get_secret_value(SecretId=secret_arn)
    creds = json.loads(secret['SecretString'])
    return creds['username'], creds['password']


def get_query_embedding(query_text):
    response = bedrock_client.invoke_model(
        ModelId="amazon.titan-embed-text-v1",
        Body=json.dumps({"text": query_text}),
        ContentType="application/json"
    )
    result = json.loads(response['Body'].read())
    return result['embedding']


def lambda_handler(event, context):
    """
    Expected event format:
    {
        "query": "search text",
        "filters": {
            "document_ids": ["doc_001", "doc_002"],
            "tenant_id": "tenant_001"
        }
    }
    """
    username, password = get_db_credentials(DB_SECRET_ARN)

    conn = psycopg2.connect(
        host=DB_HOST,
        port=DB_PORT,
        dbname=DB_NAME,
        user=username,
        password=password
    )
    cur = conn.cursor()

    query_text = event.get("query", "")
    filters = event.get("filters", {})

    if not query_text:
        return {"statusCode": 400, "body": "Query text is required"}

    # Step 1: Generate embedding for the query
    query_embedding = get_query_embedding(query_text)

    # Step 2: Build SQL for vector similarity search with optional filters
    sql = """
        SELECT chunk_text, metadata,
               embedding_vector <#> %s AS cosine_distance
        FROM document_chunks
        WHERE status='completed'
    """
    params = [query_embedding]

    # Apply filters if provided
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

    # Step 3: Execute query
    cur.execute(sql, params)
    results = cur.fetchall()

    # Format results
    response_data = []
    for chunk_text, metadata, distance in results:
        response_data.append({
            "chunk_text": chunk_text,
            "metadata": metadata,
            "similarity": 1 - distance  # cosine similarity
        })

    cur.close()
    conn.close()

    return {"statusCode": 200, "body": json.dumps(response_data)}
