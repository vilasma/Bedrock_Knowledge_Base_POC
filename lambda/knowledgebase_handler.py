import os
import psycopg2
import json
import ast
import boto3

# --------------------- ENV ---------------------
DB_HOST = os.environ['DB_HOST']
DB_PORT = os.environ['DB_PORT']
DB_NAME = os.environ['DB_NAME']
DB_SECRET_ARN = os.environ['DB_SECRET_ARN']
REGION = os.environ.get('REGION', 'us-east-1')
TOP_K = int(os.environ.get('TOP_K', 5))

# --------------------- CLIENTS ---------------------
bedrock_client = boto3.client('bedrock-runtime', region_name=REGION)
secrets_client = boto3.client('secretsmanager', region_name=REGION)

# --------------------- HELPERS ---------------------
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

def parse_embedding(embedding):
    """
    Convert embedding string from DB to list of floats.
    If already a list, return as floats.
    """
    if isinstance(embedding, str):
        # Safe parsing of string like '[0.1, 0.2, ...]'
        embedding = ast.literal_eval(embedding)
    return [float(x) for x in embedding]

def cosine_similarity(a, b):
    """
    Compute cosine similarity between two vectors (lists of floats).
    """
    dot = sum(x*y for x, y in zip(a, b))
    norm_a = sum(x*x for x in a) ** 0.5
    norm_b = sum(y*y for y in b) ** 0.5
    return dot / (norm_a * norm_b) if norm_a and norm_b else 0.0

# --------------------- LAMBDA HANDLER ---------------------
def lambda_handler(event, context):
    # 1. Connect to PostgreSQL
    username, password = get_db_credentials(DB_SECRET_ARN)
    conn = psycopg2.connect(
        host=DB_HOST,
        port=DB_PORT,
        dbname=DB_NAME,
        user=username,
        password=password
    )
    cur = conn.cursor()

    # 2. Extract query and optional filters
    query_text = event.get("query", "Retrieve relevant chunks")
    filters = event.get("filters", {})

    # 3. Get embedding for query
    query_embedding = parse_embedding(get_query_embedding(query_text))

    # 4. Fetch all completed chunks
    sql = "SELECT chunk_text, embedding_vector, metadata FROM document_chunks WHERE status='completed'"
    cur.execute(sql)
    rows = cur.fetchall()

    # 5. Compute similarity
    results = []
    for chunk_text, embedding_vector, metadata in rows:
        embedding_vector = parse_embedding(embedding_vector)
        similarity = cosine_similarity(query_embedding, embedding_vector)
        results.append({
            "chunk_text": chunk_text,
            "metadata": metadata,
            "similarity": similarity
        })

    # 6. Sort by similarity and take top K
    results.sort(key=lambda x: x["similarity"], reverse=True)
    top_results = results[:TOP_K]

    # 7. Close DB connection
    cur.close()
    conn.close()

    return {"statusCode": 200, "body": json.dumps(top_results)}
