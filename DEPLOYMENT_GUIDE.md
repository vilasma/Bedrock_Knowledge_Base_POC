# Bedrock Knowledge Base POC - Deployment Guide

Complete step-by-step guide to deploy and run the entire POC from scratch.

---

## 📋 Prerequisites

- AWS Account with Administrator access
- AWS CLI installed and configured
- Python 3.11+ installed locally
- Docker (optional, for Lambda layer building)

---

## 🏗️ Architecture Overview

```
┌─────────────┐      ┌──────────────┐      ┌─────────────────┐
│   Gradio    │ ───> │  S3 Bucket   │ ───> │ Lambda Function │
│     UI      │      │   (Docs)     │      │ (main_handler)  │
└─────────────┘      └──────────────┘      └─────────────────┘
      │                                              │
      │                                              ↓
      │                                    ┌──────────────────┐
      │                                    │ Bedrock KB Sync  │
      │                                    └──────────────────┘
      │                                              │
      ↓                                              ↓
┌─────────────────────────────────────────────────────────────┐
│           Aurora PostgreSQL (pgvector)                       │
│  - documents (tracking)                                      │
│  - document_chunks (chunks + embeddings)                     │
│  - bedrock_kb_documents (managed by Bedrock)                 │
└─────────────────────────────────────────────────────────────┘
```

---

## 🚀 Step-by-Step Deployment

### **Step 1: Set Up Aurora PostgreSQL with pgvector**

#### 1.1 Create Aurora Cluster (via Console or CLI)

**Via AWS Console:**
1. Go to RDS → Create Database
2. Choose:
   - Engine: Aurora (PostgreSQL Compatible)
   - Version: PostgreSQL 15.5 or higher
   - Templates: Dev/Test or Production
3. Settings:
   - DB cluster identifier: `bedrock-kb-cluster`
   - Master username: `poc_admin`
   - Master password: (save this securely)
4. Instance configuration:
   - DB instance class: `db.r6g.large` (or smaller for testing)
5. Connectivity:
   - VPC: Select your VPC
   - Public access: No (for security)
   - VPC security group: Create new or use existing
6. Additional configuration:
   - Initial database name: `bedrock_poc`
   - Enable Data API: **YES** ✅ (Required for Bedrock)
7. Create database

**Via AWS CLI:**
```bash
aws rds create-db-cluster \
    --db-cluster-identifier bedrock-kb-cluster \
    --engine aurora-postgresql \
    --engine-version 15.5 \
    --master-username poc_admin \
    --master-user-password YOUR_PASSWORD \
    --database-name bedrock_poc \
    --enable-http-endpoint

aws rds create-db-instance \
    --db-instance-identifier bedrock-kb-instance \
    --db-instance-class db.r6g.large \
    --engine aurora-postgresql \
    --db-cluster-identifier bedrock-kb-cluster
```

#### 1.2 Store Database Credentials in Secrets Manager

```bash
aws secretsmanager create-secret \
    --name bedrock-ingest-poc-secrets-db/credentials \
    --description "Aurora credentials for Bedrock POC" \
    --secret-string '{
        "username":"poc_admin",
        "password":"YOUR_PASSWORD",
        "dbname":"bedrock_poc"
    }'
```

Note the Secret ARN - you'll need it later.

---

### **Step 2: Create S3 Bucket**

```bash
# Create bucket
aws s3 mb s3://bedrock-ingest-bucket

# Create folder for documents
aws s3api put-object \
    --bucket bedrock-ingest-bucket \
    --key bedrock-poc-docs/
```

---

### **Step 3: Deploy CloudFormation Stack**

The CloudFormation template (`cft/template.yml`) creates:
- VPC, Subnets, Security Groups
- NAT Gateway, VPC Endpoints
- Lambda functions
- IAM roles
- Bedrock Knowledge Base
- S3 event notifications

#### 3.1 Package Lambda Code

```bash
cd lambda

# Package init_db Lambda
zip -r init_db.zip init_db.py
aws s3 cp init_db.zip s3://bedrock-ingest-bucket/lambda/

# Package main_handler Lambda
zip -r s3_handler.zip main_handler.py
aws s3 cp s3_handler.zip s3://bedrock-ingest-bucket/lambda/
```

#### 3.2 Create Lambda Layer for Dependencies

```bash
# Create layer directory
mkdir -p python/lib/python3.11/site-packages

# Install dependencies
pip install \
    psycopg2-binary \
    pdfplumber \
    python-docx \
    boto3 \
    -t python/lib/python3.11/site-packages/

# Package layer
zip -r ingest-dependencies-layer.zip python/

# Upload to S3
aws s3 cp ingest-dependencies-layer.zip s3://bedrock-ingest-bucket/lambda/

# Create Lambda layer
aws lambda publish-layer-version \
    --layer-name ingest-dependencies-layer \
    --description "Dependencies for document ingestion" \
    --content S3Bucket=bedrock-ingest-bucket,S3Key=lambda/ingest-dependencies-layer.zip \
    --compatible-runtimes python3.11
```

Note the Layer ARN - update `cft/template.yml` line 415.

#### 3.3 Deploy CloudFormation Stack

```bash
cd cft

aws cloudformation create-stack \
    --stack-name bedrock-kb-poc \
    --template-body file://template.yml \
    --capabilities CAPABILITY_IAM \
    --parameters \
        ParameterKey=DBUsername,ParameterValue=poc_admin \
        ParameterKey=DBName,ParameterValue=bedrock_poc \
        ParameterKey=InstanceClass,ParameterValue=db.r6g.large
```

**Monitor deployment:**
```bash
aws cloudformation describe-stacks \
    --stack-name bedrock-kb-poc \
    --query 'Stacks[0].StackStatus'
```

Wait until status is `CREATE_COMPLETE` (10-20 minutes).

#### 3.4 Get Stack Outputs

```bash
aws cloudformation describe-stacks \
    --stack-name bedrock-kb-poc \
    --query 'Stacks[0].Outputs'
```

Save these values:
- `RDSClusterEndpoint`
- `DBSecretArn`
- `BedrockKnowledgeBaseId`
- `S3HandlerLambdaName`

---

### **Step 4: Initialize Database Schema**

The `init_db` Lambda should run automatically during stack creation. Verify:

```bash
aws lambda invoke \
    --function-name poc-init-db \
    --payload '{}' \
    response.json

cat response.json
```

Expected output:
```json
{
  "statusCode": 200,
  "body": {
    "message": "Aurora Knowledge Base initialized successfully",
    "table_counts": {
      "bedrock_kb_documents": 0,
      "documents": 0,
      "document_chunks": 0,
      "metadata": 0,
      "query_history": 0,
      "query_results": 0,
      "failed_chunks": 0
    }
  }
}
```

---

### **Step 5: Configure S3 Event Notification**

Connect S3 to Lambda for automatic processing:

```bash
aws s3api put-bucket-notification-configuration \
    --bucket bedrock-ingest-bucket \
    --notification-configuration '{
        "LambdaFunctionConfigurations": [
            {
                "Id": "TriggerDocumentProcessing",
                "LambdaFunctionArn": "arn:aws:lambda:us-east-1:YOUR_ACCOUNT:function:poc-s3-handler",
                "Events": ["s3:ObjectCreated:*"],
                "Filter": {
                    "Key": {
                        "FilterRules": [
                            {
                                "Name": "prefix",
                                "Value": "bedrock-poc-docs/"
                            },
                            {
                                "Name": "suffix",
                                "Value": ".pdf"
                            }
                        ]
                    }
                }
            }
        ]
    }'
```

---

### **Step 6: Configure Local Environment**

#### 6.1 Create `.env` file

```bash
cd /Users/vilasma/Bedrock_Knowledge_Base_POC
cp .env.example .env
```

Edit `.env` with your values:

```bash
# AWS Configuration
AWS_ACCESS_KEY_ID=YOUR_ACCESS_KEY
AWS_SECRET_ACCESS_KEY=YOUR_SECRET_KEY
AWS_REGION=us-east-1

# S3 Configuration
S3_BUCKET=bedrock-ingest-bucket
S3_UPLOAD_PREFIX=bedrock-poc-docs/

# Lambda Configuration
LAMBDA_FUNCTION=poc-s3-handler

# Bedrock Knowledge Base (from CloudFormation outputs)
KNOWLEDGE_BASE_ID=XXXXX
MODEL_ARN=arn:aws:bedrock:us-east-1::foundation-model/anthropic.claude-v2

# Database Configuration (from CloudFormation outputs)
DB_HOST=bedrock-kb-cluster.XXXXX.us-east-1.rds.amazonaws.com
DB_PORT=5432
DB_NAME=bedrock_poc
DB_SECRET_ARN=arn:aws:secretsmanager:us-east-1:ACCOUNT:secret:bedrock-db-credentials
DB_USER=poc_admin
DB_PASSWORD=YOUR_PASSWORD

# OpenAI Configuration
OPEN_CHAT_API_KEY=sk-YOUR_OPENAI_API_KEY

# Processing Configuration
CHUNK_SIZE=300
TOP_K=5
MAX_POLL_SECONDS=120
POLL_INTERVAL=5
```

#### 6.2 Install Python Dependencies

```bash
pip install -r requirements.txt
```

---

### **Step 7: Run the Application**

```bash
python app.py
```

**Expected output:**
```
Connected to AWS Account ID: 123456789012
Running on local URL:  http://0.0.0.0:7860
Running on FastAPI: http://0.0.0.0:8000
```

**Access the UI:**
- Gradio UI: http://localhost:7860
- FastAPI: http://localhost:8000/docs

---

## 🧪 Testing the POC

### Test 1: Upload a Document

1. Go to "📤 Upload & Monitor" tab
2. Upload a PDF/TXT file
3. Enter metadata:
   - Tenant ID: `tenant-demo`
   - User ID: `user-demo`
4. Click "🚀 Upload & Process"
5. Watch real-time buffering:
   ```
   [10:30:15] Starting upload...
   [10:30:16] ✅ Uploaded to s3://bedrock-ingest-bucket/bedrock-poc-docs/test.pdf
   [10:30:17] Lambda function triggered automatically...
   [10:30:22] Status: processing | Chunks stored: 0
   [10:30:24] Status: processing | Chunks stored: 15
   [10:30:35] ✅ Processing complete! Total chunks: 15
   ```

### Test 2: Query Knowledge Base

1. Go to "🔍 Query Knowledge Base" tab
2. Enter question: "What is the main topic of the document?"
3. (Optional) Add filters:
   - Filter by Tenant ID: `tenant-demo`
4. Click "💬 Ask Question"
5. View:
   - Answer from OpenAI
   - Retrieved context details
   - Similarity scores

### Test 3: Check Document Status

1. Go to "📊 Document Status" tab
2. Enter S3 key: `bedrock-poc-docs/test.pdf`
3. Click "🔍 Check Status"
4. View:
   - Document metadata
   - Processing status
   - Chunk count
   - Sample chunks

---

## 📊 Verify Data in Database

Connect to Aurora and check tables:

```bash
# Get database endpoint
aws rds describe-db-clusters \
    --db-cluster-identifier bedrock-kb-cluster \
    --query 'DBClusters[0].Endpoint'

# Connect via psql (if you have it installed)
psql -h YOUR_ENDPOINT -U poc_admin -d bedrock_poc
```

**Run queries:**
```sql
-- Check documents
SELECT document_id, document_name, status, chunk_count, created_at
FROM documents
ORDER BY created_at DESC;

-- Check chunks
SELECT document_id, chunk_index, LEFT(chunk_text, 100), status
FROM document_chunks
ORDER BY created_at DESC
LIMIT 10;

-- Check Bedrock KB documents
SELECT id, LEFT(chunks, 100), metadata
FROM bedrock_kb_documents
LIMIT 5;

-- Get statistics
SELECT
    status,
    COUNT(*) as count,
    SUM(chunk_count) as total_chunks
FROM documents
GROUP BY status;
```

---

## 🔧 Troubleshooting

### Issue 1: Lambda Cannot Connect to Database

**Symptoms:** Lambda times out, "cannot connect to database"

**Solution:**
1. Check Lambda is in VPC private subnets
2. Verify security group allows Lambda → RDS on port 5432
3. Check NAT Gateway is running for Lambda internet access
4. Verify VPC endpoints exist for Secrets Manager

```bash
# Check Lambda VPC config
aws lambda get-function-configuration \
    --function-name poc-s3-handler \
    --query 'VpcConfig'
```

### Issue 2: pgvector Version Error

**Symptoms:** "pgvector version X does not support HNSW"

**Solution:**
1. Aurora PostgreSQL must be version 15.5+
2. Update cluster:
```bash
aws rds modify-db-cluster \
    --db-cluster-identifier bedrock-kb-cluster \
    --engine-version 15.5 \
    --apply-immediately
```

### Issue 3: Bedrock Knowledge Base Not Syncing

**Symptoms:** Documents uploaded but Bedrock KB empty

**Solution:**
1. Check Bedrock KB IAM role has RDS Data API permissions
2. Verify Data API is enabled on Aurora cluster
3. Manually trigger sync:
```bash
aws bedrock-agent start-ingestion-job \
    --knowledge-base-id YOUR_KB_ID \
    --data-source-id YOUR_DATA_SOURCE_ID
```

### Issue 4: App Cannot Import main_handler

**Symptoms:** `ImportError: No module named 'lambda.main_handler'`

**Solution:**
```python
# Update app.py imports (already done in code)
import sys
sys.path.append(os.path.dirname(os.path.abspath(__file__)) + '/lambda')
```

Or run from project root:
```bash
cd /Users/vilasma/Bedrock_Knowledge_Base_POC
python app.py
```

---

## 🗑️ Clean Up (Optional)

To delete all resources and avoid charges:

```bash
# Delete CloudFormation stack
aws cloudformation delete-stack --stack-name bedrock-kb-poc

# Empty and delete S3 bucket
aws s3 rm s3://bedrock-ingest-bucket --recursive
aws s3 rb s3://bedrock-ingest-bucket

# Delete Secrets Manager secret
aws secretsmanager delete-secret \
    --secret-id bedrock-ingest-poc-secrets-db/credentials \
    --force-delete-without-recovery

# Delete Aurora cluster
aws rds delete-db-instance \
    --db-instance-identifier bedrock-kb-instance \
    --skip-final-snapshot

aws rds delete-db-cluster \
    --db-cluster-identifier bedrock-kb-cluster \
    --skip-final-snapshot
```

---

## 📚 Additional Resources

- [AWS Bedrock Knowledge Bases Documentation](https://docs.aws.amazon.com/bedrock/latest/userguide/knowledge-base.html)
- [Aurora PostgreSQL pgvector](https://docs.aws.amazon.com/AmazonRDS/latest/AuroraUserGuide/AuroraPostgreSQL.VectorDB.html)
- [AWS Lambda Layers](https://docs.aws.amazon.com/lambda/latest/dg/chapter-layers.html)

---

## 🎯 Quick Start Summary

```bash
# 1. Deploy infrastructure
aws cloudformation create-stack --stack-name bedrock-kb-poc --template-body file://cft/template.yml --capabilities CAPABILITY_IAM

# 2. Configure environment
cp .env.example .env
# Edit .env with your values

# 3. Install dependencies
pip install -r requirements.txt

# 4. Run application
python app.py

# 5. Access UI
# Open http://localhost:7860
```

---

**You're all set!** 🎉 The POC is now running end-to-end.
