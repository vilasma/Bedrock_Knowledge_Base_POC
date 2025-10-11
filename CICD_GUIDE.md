# CI/CD Pipeline Guide

Complete guide for automated deployment using GitHub Actions.

---

## 📋 Overview

The CI/CD pipeline (``.github/workflows/deploy.yml`) automates the entire deployment process:

1. ✅ Builds Lambda dependencies layer
2. ✅ Packages Lambda functions
3. ✅ Uploads artifacts to S3
4. ✅ Deploys CloudFormation stack
5. ✅ Configures S3 event notifications
6. ✅ Outputs configuration for local development

---

## 🔧 Setup GitHub Secrets

Before running the pipeline, configure these secrets in your GitHub repository:

**Navigate to:** `Settings` → `Secrets and variables` → `Actions` → `New repository secret`

### Required Secrets:

| Secret Name | Description | Example |
|-------------|-------------|---------|
| `AWS_ACCESS_KEY_ID` | AWS access key | `AKIAIOSFODNN7EXAMPLE` |
| `AWS_SECRET_ACCESS_KEY` | AWS secret key | `wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY` |
| `AWS_REGION` | AWS region | `us-east-1` |
| `S3_BUCKET` | S3 bucket name | `bedrock-ingest-bucket` |

---

## 🚀 Trigger Deployment

### Method 1: Automatic (Push to main)

```bash
git add .
git commit -m "Deploy Bedrock KB POC"
git push origin main
```

The pipeline will automatically run on every push to `main` branch.

### Method 2: Manual (Workflow Dispatch)

1. Go to GitHub repository
2. Click **Actions** tab
3. Select **Deploy Bedrock Knowledge Base PoC** workflow
4. Click **Run workflow**
5. Choose options:
   - **Environment:** dev/staging/prod
   - **Reset DB:** true/false (drop and recreate tables)
6. Click **Run workflow**

---

## 📊 Pipeline Steps

### Step 1: Build Lambda Layer
```bash
# What it does:
- Creates python/lib/python3.11/site-packages directory
- Installs: psycopg2-binary, pdfplumber, python-docx, boto3
- Removes unnecessary files (pip, setuptools, wheel)
- Zips layer: ingest-dependencies-layer.zip
```

**Output:** Lambda layer with all dependencies

### Step 2: Publish Lambda Layer
```bash
# What it does:
- Publishes layer to AWS Lambda
- Gets Layer ARN
- Sets environment variable: LAYER_ARN
```

**Output:** `arn:aws:lambda:us-east-1:ACCOUNT:layer:ingest-dependencies-layer:VERSION`

### Step 3: Package Lambda Functions
```bash
# What it does:
- Packages main_handler.py → s3_handler.zip
- Packages init_db.py → init_db.zip
- Packages index.py → index.zip
```

**Output:** Three Lambda function packages

### Step 4: Upload to S3
```bash
# What it does:
- Creates S3 bucket if doesn't exist
- Creates lambda/ folder in bucket
- Uploads all Lambda zips to s3://BUCKET/lambda/
```

**Output:** Lambda artifacts in S3

### Step 5: Deploy CloudFormation
```bash
# What it does:
- Deploys/updates stack: bedrock-kb-poc
- Uses parameters:
  - DBUsername=poc_admin
  - DBName=bedrock_poc
  - InstanceClass=db.r6g.large
- Creates:
  - VPC, Subnets, Security Groups
  - Aurora PostgreSQL cluster
  - Lambda functions with layer
  - Bedrock Knowledge Base
  - IAM roles
```

**Output:** Complete AWS infrastructure

### Step 6: Get Stack Outputs
```bash
# What it does:
- Fetches CloudFormation outputs
- Extracts:
  - Knowledge Base ID
  - Database Endpoint
  - Lambda ARNs
  - S3 Bucket name
```

**Output:** Configuration values for local .env

### Step 7: Configure S3 Event Notification
```bash
# What it does:
- Adds Lambda permission for S3 invocation
- Configures S3 bucket notification:
  - Trigger: s3:ObjectCreated:*
  - Prefix: bedrock-poc-docs/
  - Target: S3 Handler Lambda
```

**Output:** Automatic Lambda trigger on S3 upload

### Step 8: Summary
```bash
# What it does:
- Displays deployment summary
- Shows configuration values
- Provides next steps
```

---

## 📝 CloudFormation Template Parameters

The template (`cft/template.yml`) supports these parameters:

| Parameter | Default | Description |
|-----------|---------|-------------|
| `DBUsername` | `poc_admin` | Aurora master username |
| `DBName` | `bedrock_poc` | Database name |
| `InstanceClass` | `db.r6g.large` | RDS instance class |
| `DBAllocatedStorage` | `20` | Storage in GB |
| `S3BucketName` | `bedrock-ingest-bucket` | S3 bucket name |
| `LambdaLayerArn` | `` | Lambda layer ARN (auto from pipeline) |
| `ResetDB` | `false` | Reset database on init |

### Override Parameters

To override parameters during deployment:

```bash
aws cloudformation deploy \
  --template-file cft/template.yml \
  --stack-name bedrock-kb-poc \
  --capabilities CAPABILITY_NAMED_IAM CAPABILITY_IAM \
  --parameter-overrides \
    DBUsername=custom_admin \
    DBName=custom_db \
    InstanceClass=db.t4g.medium \
    S3BucketName=my-custom-bucket \
    ResetDB=true
```

---

## 🔍 Monitor Deployment

### View Pipeline Logs

1. Go to GitHub **Actions** tab
2. Click on running workflow
3. View logs for each step

### Check CloudFormation Stack

```bash
# Get stack status
aws cloudformation describe-stacks \
  --stack-name bedrock-kb-poc \
  --query 'Stacks[0].StackStatus'

# Watch stack events
aws cloudformation describe-stack-events \
  --stack-name bedrock-kb-poc \
  --max-items 20

# Get stack outputs
aws cloudformation describe-stacks \
  --stack-name bedrock-kb-poc \
  --query 'Stacks[0].Outputs'
```

---

## ✅ Verify Deployment

### 1. Check Lambda Functions

```bash
# List functions
aws lambda list-functions --query 'Functions[?starts_with(FunctionName, `poc-`)]'

# Test init DB Lambda
aws lambda invoke \
  --function-name poc-init-db \
  --payload '{}' \
  response.json && cat response.json
```

### 2. Check Database

```bash
# Get DB endpoint
aws rds describe-db-clusters \
  --db-cluster-identifier bedrock-kb-cluster \
  --query 'DBClusters[0].Endpoint'

# Connect (if psql installed)
psql -h ENDPOINT -U poc_admin -d bedrock_poc
```

### 3. Check Bedrock Knowledge Base

```bash
# List knowledge bases
aws bedrock-agent list-knowledge-bases

# Get specific KB
aws bedrock-agent get-knowledge-base \
  --knowledge-base-id YOUR_KB_ID
```

### 4. Test Document Upload

```bash
# Upload test document
echo "This is a test document" > test.txt
aws s3 cp test.txt s3://bedrock-ingest-bucket/bedrock-poc-docs/

# Check Lambda logs
aws logs tail /aws/lambda/poc-s3-handler --follow
```

---

## 🛠️ Troubleshooting

### Issue: Pipeline Fails at "Build Lambda Layer"

**Cause:** Missing system dependencies

**Solution:**
```yaml
# deploy.yml already includes:
sudo apt-get install -y zip wget postgresql-client libpq-dev gcc
```

### Issue: "Layer ARN not found"

**Cause:** Layer publish failed

**Solution:**
1. Check AWS Lambda quotas
2. Verify IAM permissions for Lambda layer creation
3. Check pipeline logs for publish_layer step

### Issue: CloudFormation Stack Fails

**Cause:** Various - check specific error

**Common Solutions:**

```bash
# View failure reason
aws cloudformation describe-stack-events \
  --stack-name bedrock-kb-poc \
  --query 'StackEvents[?ResourceStatus==`CREATE_FAILED`]'

# Common fixes:
# 1. VPC limit reached - delete unused VPCs
# 2. IAM role name conflict - delete old stack first
# 3. pgvector version - use Aurora PostgreSQL 15.5+
```

### Issue: S3 Event Notification Not Working

**Cause:** Permission or configuration error

**Solution:**
```bash
# Check Lambda permission
aws lambda get-policy --function-name poc-s3-handler

# Check S3 notification config
aws s3api get-bucket-notification-configuration \
  --bucket bedrock-ingest-bucket

# Re-run notification configuration step
# (See deploy.yml Step 7)
```

---

## 🔄 Update Deployment

### Update Lambda Code Only

```bash
# Make changes to lambda_codes/*.py
git add lambda_codes/
git commit -m "Update Lambda functions"
git push origin main

# Pipeline will redeploy only Lambda functions
```

### Update Infrastructure

```bash
# Make changes to cft/template.yml
git add cft/
git commit -m "Update CloudFormation template"
git push origin main

# Pipeline will update CloudFormation stack
```

### Force Database Reset

```bash
# Use workflow dispatch with reset_db=true
# Or update template parameter:
aws cloudformation update-stack \
  --stack-name bedrock-kb-poc \
  --use-previous-template \
  --parameters ParameterKey=ResetDB,ParameterValue=true
```

---

## 🗑️ Cleanup

### Delete via Pipeline

Add a cleanup workflow (optional):

```yaml
# .github/workflows/cleanup.yml
name: Cleanup Resources
on:
  workflow_dispatch:

jobs:
  cleanup:
    runs-on: ubuntu-latest
    steps:
      - name: Delete Stack
        run: |
          aws cloudformation delete-stack --stack-name bedrock-kb-poc
          aws cloudformation wait stack-delete-complete --stack-name bedrock-kb-poc
      - name: Empty S3 Bucket
        run: |
          aws s3 rm s3://bedrock-ingest-bucket --recursive
          aws s3 rb s3://bedrock-ingest-bucket
```

### Manual Cleanup

```bash
# 1. Delete CloudFormation stack
aws cloudformation delete-stack --stack-name bedrock-kb-poc

# 2. Empty and delete S3 bucket
aws s3 rm s3://bedrock-ingest-bucket --recursive
aws s3 rb s3://bedrock-ingest-bucket

# 3. Delete Lambda layer versions
aws lambda list-layer-versions --layer-name ingest-dependencies-layer
aws lambda delete-layer-version --layer-name ingest-dependencies-layer --version-number N

# 4. Delete secrets
aws secretsmanager delete-secret \
  --secret-id bedrock-ingest-poc-secrets-db/credentials \
  --force-delete-without-recovery
```

---

## 📚 Additional Resources

- **GitHub Actions Docs:** https://docs.github.com/en/actions
- **AWS CloudFormation:** https://docs.aws.amazon.com/cloudformation/
- **AWS Lambda Layers:** https://docs.aws.amazon.com/lambda/latest/dg/chapter-layers.html

---

## 🎯 Quick Commands

```bash
# Trigger deployment
git push origin main

# View logs
gh run list
gh run view [RUN_ID]

# Get stack outputs
aws cloudformation describe-stacks \
  --stack-name bedrock-kb-poc \
  --query 'Stacks[0].Outputs'

# Test Lambda
aws lambda invoke \
  --function-name poc-s3-handler \
  --payload '{"Records":[]}' \
  response.json

# Upload test document
aws s3 cp test.pdf s3://bedrock-ingest-bucket/bedrock-poc-docs/
```

---

**Your CI/CD pipeline is ready to deploy!** 🚀

Simply push to main or use workflow dispatch to deploy the entire infrastructure automatically.
