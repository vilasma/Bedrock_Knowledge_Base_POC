import boto3
import os
import json
import psycopg2

secrets = boto3.client("secretsmanager")

DB_SECRET_ARN = os.environ["DB_SECRET_ARN"]
DB_NAME = os.environ["DB_NAME"]
DB_HOST = os.environ["DB_HOST"]
DB_PORT = os.environ["DB_PORT"]

def get_db_credentials():
    secret = secrets.get_secret_value(SecretId=DB_SECRET_ARN)
    creds = json.loads(secret["SecretString"])
    return creds["username"], creds["password"]

def lambda_handler(event, context):
    print("Init DB event:", json.dumps(event))

    user, password = get_db_credentials()
    conn = psycopg2.connect(
        dbname=DB_NAME, user=user, password=password,
        host=DB_HOST, port=DB_PORT
    )
    cur = conn.cursor()

    # Enable pgvector and create table if not exists
    cur.execute("CREATE EXTENSION IF NOT EXISTS vector;")
    cur.execute("""
        CREATE TABLE IF NOT EXISTS document_chunks (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            text TEXT NOT NULL,
            vector VECTOR(1536),
            metadata JSONB
        );
    """)
    conn.commit()
    cur.close()
    conn.close()

    return {"status": "table ready"}
