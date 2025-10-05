import os
import psycopg2
import json
import math
import boto3

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

def get_query_embedding(query_text):
    response = bedrock_client.invoke_model(
        modelId="amazon.titan-embed-text-v1",
        body=json.dumps({"inputText": query_text}),
        contentType="application/json",
        accept="application/json"
    )
    result = json.loads(response['body'].read())
    return result['embedding']

def cosine_similarity(a, b):
    dot = sum(x*y for x,y in zip(a,b))
    norm_a = math.sqrt(sum(x*x for x in a))
    norm_b = math.sqrt(sum(y*y for y in b))
    return dot / (norm_a * norm_b) if norm_a and norm_b else 0.0

def lambda_handler(event, context):
    username, password = get_db_credentials(DB_SECRET_ARN)
    conn = psycopg2.connect(
        host=DB_HOST, port=DB_PORT, dbname=DB_NAME,
        user=username, password=password
    )
    cur = conn.cursor()

    query_text = event.get("query", "Retrieve relevant chunks")
    filters = event.get("filters", {})

    query_embedding = get_query_embedding(query_text)

    # Fetch all embeddings from DB (no pgvector)
    sql = "SELECT chunk_text, embedding_vector, metadata FROM document_chunks WHERE status='completed'"
    cur.execute(sql)
    rows = cur.fetchall()

    results = []
    for chunk_text, embedding_vector, metadata in rows:
        similarity = cosine_similarity(query_embedding, embedding_vector)
        results.append({"chunk_text": chunk_text, "metadata": metadata, "similarity": similarity})

    # Sort by similarity
    results.sort(key=lambda x: x["similarity"], reverse=True)
    cur.close()
    conn.close()

    return {"statusCode": 200, "body": json.dumps(results[:TOP_K])}
