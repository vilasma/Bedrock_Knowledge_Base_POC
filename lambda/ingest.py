import os
import json
import boto3
import psycopg2
import uuid
from langchain.text_splitter import RecursiveCharacterTextSplitter

# Environment variables
DB_HOST = os.environ['DB_HOST']
DB_PORT = int(os.environ['DB_PORT'])
DB_NAME = os.environ['DB_NAME']
DB_SECRET_ARN = os.environ['DB_SECRET_ARN']
REGION = os.environ.get('REGION', 'us-east-1')
METADATA_FIELDS = os.environ.get(
    'METADATA_FIELDS',
    'tenant_id,user_id,document_id,project_id,thread_id'
).split(',')

# AWS clients
s3_client = boto3.client('s3', region_name=REGION)
bedrock_client = boto3.client('bedrock-runtime', region_name=REGION)
secrets_client = boto3.client('secretsmanager', region_name=REGION)


def get_db_credentials(secret_arn):
    """Fetch RDS credentials from Secrets Manager"""
    secret = secrets_client.get_secret_value(SecretId=secret_arn)
    creds = json.loads(secret['SecretString'])
    return creds['username'], creds['password']


def get_embedding(text):
    """Generate 1024-dimensional embedding using Bedrock Titan V1"""
    response = bedrock_client.invoke_model(
        modelId='amazon.titan-embed-text-v2:0',
        body=json.dumps({"inputText": text}),
        contentType='application/json',
        accept='application/json'
    )
    result = json.loads(response['body'].read())
    return result['embedding']


def lambda_handler(event, context):
    # Connect to Aurora PostgreSQL
    username, password = get_db_credentials(DB_SECRET_ARN)
    conn = psycopg2.connect(
        host=DB_HOST,
        port=DB_PORT,
        dbname=DB_NAME,
        user=username,
        password=password
    )
    cur = conn.cursor()
    processed_files = 0
    total_chunks = 0

    # Iterate over S3 event records
    for record in event.get('Records', []):
        bucket = record['s3']['bucket']['name']
        key = record['s3']['object']['key']

        # Fetch file content from S3 with safe decoding
        obj = s3_client.get_object(Bucket=bucket, Key=key)
        raw_data = obj['Body'].read()
        text = raw_data.decode('utf-8', errors='ignore')  # ✅ ignore invalid bytes

        # Split text into chunks
        splitter = RecursiveCharacterTextSplitter(chunk_size=500, chunk_overlap=100)
        chunks = splitter.split_text(text)

        # Prepare metadata
        metadata_values = {field.lower(): f"{field}_sample" for field in METADATA_FIELDS}
        tenant_id = metadata_values.get('tenant_id')
        user_id = metadata_values.get('user_id')
        project_id = metadata_values.get('project_id')
        thread_id = metadata_values.get('thread_id')

        # Insert each chunk into DB with unique document_id
        for i, chunk in enumerate(chunks):
            # Generate a UUID for document_id
            document_id = str(uuid.uuid4())

            embedding = get_embedding(chunk)
            # Keep the original file/key as document_name
            document_name = key  # original S3 key

            cur.execute(
                """
                INSERT INTO document_chunks
                (tenant_id, user_id, document_id, document_name, project_id, thread_id, chunk_text, embedding_vector, metadata)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    tenant_id, user_id, document_id, document_name,
                    project_id, thread_id, chunk.replace("\x00", ""),
                    embedding, json.dumps(metadata_values)
                )
            )

        processed_files += 1
        total_chunks += len(chunks)

    conn.commit()
    cur.close()
    conn.close()

    return {
        "statusCode": 200,
        "body": f"Processed {processed_files} documents and inserted {total_chunks} chunks"
    }
