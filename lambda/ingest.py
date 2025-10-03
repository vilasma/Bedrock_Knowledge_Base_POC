import os
import json
import boto3
import psycopg2
from psycopg2.extras import Json
from langchain.text_splitter import RecursiveCharacterTextSplitter

# Environment variables
DB_HOST = os.environ['DB_HOST']
DB_PORT = int(os.environ['DB_PORT'])
DB_NAME = os.environ['DB_NAME']
DB_SECRET_ARN = os.environ['DB_SECRET_ARN']
REGION = os.environ.get('REGION', 'us-east-1')  # Bedrock Titan model region
METADATA_FIELDS = os.environ.get('METADATA_FIELDS', 'tenant_id,user_id,document_id,project_id,thread_id').split(',')

# AWS clients
s3_client = boto3.client('s3', region_name=REGION)
bedrock_client = boto3.client('bedrock', region_name=REGION)
secrets_client = boto3.client('secretsmanager', region_name=REGION)

# Get RDS credentials
def get_db_credentials(secret_arn):
    secret = secrets_client.get_secret_value(SecretId=secret_arn)
    creds = json.loads(secret['SecretString'])
    return creds['username'], creds['password']

# Generate embedding using Bedrock Titan model
def get_embedding(text):
    response = bedrock_client.invoke_model(
        ModelId='amazon.titan-embed-text-v2',  # Titan embedding model
        Body=json.dumps({"inputText": text}),
        ContentType='application/json'
    )
    result = json.loads(response['Body'].read())
    return result['embedding']  # 1536-dim float vector

def lambda_handler(event, context):
    # Connect to Aurora PostgreSQL
    username, password = get_db_credentials(DB_SECRET_ARN)
    conn = psycopg2.connect(
        host=DB_HOST, port=DB_PORT, dbname=DB_NAME, user=username, password=password
    )
    cur = conn.cursor()

    # Iterate S3 records
    for record in event.get('Records', []):
        bucket = record['s3']['bucket']['name']
        key = record['s3']['object']['key']

        obj = s3_client.get_object(Bucket=bucket, Key=key)
        text = obj['Body'].read().decode('utf-8')

        # Split text into chunks
        splitter = RecursiveCharacterTextSplitter(chunk_size=800, chunk_overlap=100)
        chunks = splitter.split_text(text)

        for chunk in chunks:
            embedding = get_embedding(chunk)

            # Prepare metadata
            metadata_values = {field.lower(): f"{field}_sample" for field in METADATA_FIELDS}
            tenant_id = metadata_values.get('tenant_id')
            user_id = metadata_values.get('user_id')
            document_id = metadata_values.get('document_id', key)
            project_id = metadata_values.get('project_id')
            thread_id = metadata_values.get('thread_id')

            cur.execute(
                """
                INSERT INTO document_chunks
                (tenant_id, user_id, document_id, project_id, thread_id, chunk_text, embedding)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                """,
                (tenant_id, user_id, document_id, project_id, thread_id, chunk, embedding)
            )

    conn.commit()
    cur.close()
    conn.close()
    return {"statusCode": 200, "body": f"Processed {len(event.get('Records', []))} documents"}
