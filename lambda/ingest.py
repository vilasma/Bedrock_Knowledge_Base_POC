import os
import json
import boto3
import psycopg2
from psycopg2.extras import Json
from langchain.text_splitter import RecursiveCharacterTextSplitter

DB_HOST = os.environ['DB_HOST']
DB_PORT = os.environ['DB_PORT']
DB_NAME = os.environ['DB_NAME']
DB_SECRET_ARN = os.environ['DB_SECRET_ARN']
REGION = os.environ.get('REGION', 'ap-south-1')
METADATA_FIELDS = os.environ['METADATA_FIELDS'].split(',')

s3_client = boto3.client('s3', region_name=REGION)
bedrock_client = boto3.client('bedrock', region_name=REGION)
secrets_client = boto3.client('secretsmanager', region_name=REGION)

def get_db_credentials(secret_arn):
    secret = secrets_client.get_secret_value(SecretId=secret_arn)
    creds = json.loads(secret['SecretString'])
    return creds['username'], creds['password']

def lambda_handler(event, context):
    username, password = get_db_credentials(DB_SECRET_ARN)
    conn = psycopg2.connect(
        host=DB_HOST, port=DB_PORT, dbname=DB_NAME, user=username, password=password
    )
    cur = conn.cursor()

    for record in event['Records']:
        bucket = record['s3']['bucket']['name']
        key = record['s3']['object']['key']

        obj = s3_client.get_object(Bucket=bucket, Key=key)
        text = obj['Body'].read().decode('utf-8')

        splitter = RecursiveCharacterTextSplitter(chunk_size=800, chunk_overlap=100)
        chunks = splitter.split_text(text)

        for chunk in chunks:
            # Call Bedrock embedding model
            response = bedrock_client.invoke_model(
                ModelId='amazon.titan-embed-text-v2',
                Body=json.dumps({"text": chunk}),
                ContentType='application/json'
            )
            embedding = json.loads(response['Body'].read())['embedding']

            # Prepare metadata for insertion
            metadata_values = {field.lower(): f"{field}_sample" for field in METADATA_FIELDS}
            tenant_id = metadata_values.get('tenant_id', None)
            user_id = metadata_values.get('user_id', None)
            document_id = metadata_values.get('document_id', key)
            project_id = metadata_values.get('project_id', None)
            thread_id = metadata_values.get('thread_id', None)

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
    return {"statusCode": 200, "body": f"Processed {len(event['Records'])} documents"}
