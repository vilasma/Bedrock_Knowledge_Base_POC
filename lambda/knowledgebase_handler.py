import os
import psycopg2
import json
import boto3

# ------------------ Environment ------------------
DB_HOST = os.environ['DB_HOST']
DB_PORT = os.environ['DB_PORT']
DB_NAME = os.environ['DB_NAME']
DB_SECRET_ARN = os.environ['DB_SECRET_ARN']
REGION = os.environ.get('REGION', 'us-east-1')
TOP_K = int(os.environ.get('TOP_K', 5))

# ------------------ Clients ------------------
bedrock_client = boto3.client('bedrock-runtime', region_name=REGION)
secrets_client = boto3.client('secretsmanager', region_name=REGION)

# ------------------ Helpers ------------------
def get_db_credentials(secret_arn):
    secret = secrets_client.get_secret_value(SecretId=secret_arn)
    creds = json.loads(secret['SecretString'])
    return creds['username'], creds['password']

def get_query_embedding(query_text):
    """
    Generate vector embedding using Amazon Titan embeddings via Bedrock.
    """
    response = bedrock_client.invoke_model(
        modelId="amazon.titan-embed-text-v1",
        body=json.dumps({"inputText": query_text}),
        contentType="application/json",
        accept="application/json"
    )
    result = json.loads(response['body'].read())
    return result['embedding']  # this is a Python list

# ------------------ Lambda Handler ------------------
def lambda_handler(event, context):
    # Optional: use CURRENT_S3_KEY for logging
    s3_key = os.environ.get("CURRENT_S3_KEY", "unknown")

    username, password = get_db_credentials(DB_SECRET_ARN)

    # Connect to DB
    conn = psycopg2.connect(
        host=DB_HOST,
        port=DB_PORT,
        dbname=DB_NAME,
        user=username,
        password=password
    )
    cur = conn.cursor()

    # Extract query text & filters
    query_text = event.get("query", f"Retrieve data for {s3_key}")
    filters = event.get("filters", {})

    if not query_text:
        return {"statusCode": 400, "body": "Query text is required"}

    # Step 1: Generate embedding vector
    query_embedding = get_query_embedding(query_text)  # wrap Python list as Vector

    # Step 2: Build SQL with vector similarity search
    sql = """
        SELECT chunk_text, metadata,
               embedding_vector <#> %s AS cosine_distance
        FROM document_chunks
        WHERE status='completed'
    """
    params = [query_embedding]

    # Optional filters
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

    # Step 4: Format results
    response_data = []
    for chunk_text, metadata, distance in results:
        response_data.append({
            "chunk_text": chunk_text,
            "metadata": metadata,
            "similarity": 1 - distance  # cosine similarity
        })

    # Close connection
    cur.close()
    conn.close()

    return {"statusCode": 200, "body": json.dumps(response_data)}
