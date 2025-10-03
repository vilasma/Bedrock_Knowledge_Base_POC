import os
import json
import boto3
import psycopg2
from psycopg2.extras import Json
from langchain.text_splitter import RecursiveCharacterTextSplitter

# Environment variables
DB_HOST = os.environ['DB_HOST']
DB_PORT = os.environ['DB_PORT']
DB_NAME = os.environ['DB_NAME']
DB_SECRET_ARN = os.environ['DB_SECRET_ARN']
REGION = os.environ.get('REGION', 'ap-south-1')
METADATA_FIELDS = os.environ['METADATA_FIELDS'].split(',')

# AWS clients
s3_client = boto3.client('s3', region_name=REGION)
bedrock_client = boto3.client('bedrock-runtime', region_name=REGION)  # Correct client
secrets_client = boto3.client('secretsmanager', region_name=REGION)

# Fetch DB credentials from Secrets Manager
def get_db_credentials(secret_arn):
    secret = secrets_client.get_secret_value(SecretId=secret_arn)
    creds = json.loads(secret['SecretString'])
    return creds['username'], creds['password']

# Lambda handler
def lambda_handler(event, context):
    # Handle non-S3 event gracefully
    if 'Records' not in event:
        return {"statusCode": 400, "body": "No Records found in event."}

    # Connect to PostgreSQL
    username, password = get_db_credentials(DB_SECRET_ARN)
    conn = psycopg2.connect(
        host=DB_HOST,
        port=DB_PORT,
        dbname=DB_NAME,
        user=username,
        password=password
    )
    cur = conn.cursor()

    for record in event['Records']:
        bucket = record['s3']['bucket']['name']
        key = record['s3']['object']['key']

        try:
            obj = s3_client.get_object(Bucket=bucket, Key=key)
            text = obj['Body'].read().decode('utf-8')
        except Exception as e:
            print(f"Error fetching {key} from S3: {str(e)}")
            continue

        # Split text into chunks
        splitter = RecursiveCharacterTextSplitter(chunk_size=800, chunk_overlap=100)
        chunks = splitter.split_text(text)

        for chunk in chunks:
            # Call Bedrock embedding model
            try:
                response = bedrock_client.invoke_model(
                    modelId='amazon.titan-embed-text-v2',
                    contentType='application/json',
                    body=json.dumps({"text": chunk})
                )
                result = json.loads(response['body'].read())
                embedding = result['embedding']
            except Exception as e:
                print(f"Error invoking Bedrock model: {str(e)}")
                continue

            # Prepare metadata
            metadata_values = {field.lower(): f"{field}_sample" for field in METADATA_FIELDS}
            tenant_id = metadata_values.get('tenant_id', None)
            user_id = metadata_values.get('user_id', None)
            document_id = metadata_values.get('document_id', key)
            project_id = metadata_values.get('project_id', None)
            thread_id = metadata_values.get('thread_id', None)

            # Insert into PostgreSQL
            try:
                cur.execute(
                    """
                    INSERT INTO document_chunks 
                    (tenant_id, user_id, document_id, project_id, thread_id, chunk_text, embedding)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                    """,
                    (tenant_id, user_id, document_id, project_id, thread_id, chunk, embedding)
                )
            except Exception as e:
                print(f"Error inserting into PostgreSQL: {str(e)}")
                continue

    conn.commit()
    cur.close()
    conn.close()

    return {"statusCode": 200, "body": f"Processed {len(event['Records'])} documents"}
