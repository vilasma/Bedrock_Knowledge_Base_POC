# AWS Bedrock Knowledge Base PoC

This Proof of Concept (PoC) demonstrates a complete RAG (Retrieval-Augmented Generation) pipeline that processes documents uploaded to S3, generates embeddings using AWS Bedrock, stores them in both Aurora PostgreSQL (with pgvector) and OpenSearch Serverless, and enables semantic search capabilities through AWS Bedrock Knowledge Base.

---

## Architecture Overview

This PoC implements a multi-tiered storage and retrieval system:

1. **Document Ingestion**: S3 → Lambda → Text Extraction → Chunking
2. **Embedding Generation**: AWS Bedrock Titan Embeddings (1536 dimensions)
3. **Dual Storage Strategy**:
   - **Aurora PostgreSQL**: Relational storage with pgvector for structured queries
   - **OpenSearch Serverless**: Vector search optimized for similarity queries
4. **Bedrock Knowledge Base**: Managed ingestion and retrieval interface
5. **Semantic Search**: Top-K retrieval using vector similarity

---

## How It Works

### 1. Document Upload and Processing
- Documents (`.txt`, `.pdf`, `.docx`) are uploaded to S3 bucket under `bedrock-poc-docs/` prefix
- S3 event notification triggers the main Lambda handler ([main_handler.py](lambda/main_handler.py))
- Lambda extracts text and splits into configurable chunks (default: 300 words)

### 2. Multi-Tier Storage Pipeline
- **Aurora PostgreSQL**: Stores documents, metadata, and chunks with embeddings
  - `documents` table: Document metadata and processing status
  - `metadata` table: Multi-tenant metadata (tenant_id, user_id, project_id, thread_id)
  - `document_chunks` table: Text chunks with 1536-dimensional embeddings and deduplication
- **OpenSearch Serverless**: Indexes chunks for fast KNN vector search
- **Bedrock Knowledge Base**: Syncs data from S3 for managed ingestion

### 3. Embedding Generation
- Each text chunk is sent to AWS Bedrock using `amazon.titan-embed-text-v1` model
- Generates 1536-dimensional embeddings
- Retry logic with exponential backoff for resilience

### 4. Bedrock Knowledge Base Ingestion
- Automatically triggers ingestion job after document processing
- Polls job status until completion (configurable timeout)
- Updates document and chunk status in Aurora based on ingestion results

### 5. Semantic Search
- Query text is embedded using the same Titan model
- OpenSearch performs KNN search to retrieve top-K most similar chunks
- Returns ranked results with similarity scores

---

## Deployment

### Prerequisites

1. **AWS CLI**:
   - Ensure the AWS CLI is installed and configured with appropriate permissions.

2. **Secrets**:
   - Add the following secrets to your GitHub repository:
     - `AWS_REGION`: AWS region for deployment (e.g., `us-east-1`).
     - `AWS_ACCESS_KEY_ID`: AWS access key ID.
     - `AWS_SECRET_ACCESS_KEY`: AWS secret access key.
     - `CFN_CODE_BUCKET`: ARN of the S3 bucket for storing Lambda artifacts.

3. **GitHub Actions**:
   - The CI/CD pipeline (`.github/workflows/deploy.yml`) automates packaging, uploading, and deploying the CloudFormation stack.

### Steps

1. **Push Code**:
   - Push your changes to the `main` branch or trigger the workflow manually.

2. **CI/CD Workflow**:
   - The workflow packages the Lambda functions (`ingest.py` and `knowledgebase_handler.py`) into zip files and uploads them to the S3 bucket.
   - Deploys the CloudFormation stack.

3. **CloudFormation Outputs**:
   - After deployment, note the outputs:
     - `S3BucketName`: Upload documents to this bucket.
     - `RDSInstanceEndpoint`: Connect to the PostgreSQL database.
     - `DBSecretArn`: ARN of the Secrets Manager secret containing DB credentials.

---

## Post-Deployment Steps

1. **Database Setup**:
   - Connect to the RDS instance using the credentials stored in Secrets Manager.
   - Run the `db/schema.sql` script to:
     - Create the `document_chunks` table.
     - Enable the `vector` extension for embedding storage.

2. **Upload Documents**:
   - Upload documents to the S3 bucket.
   - The pipeline will automatically process the documents and store embeddings in the database.

---

## Configuration

### Environment Variables

- **Ingest Lambda Function**:
  - `DB_SECRET_ARN`: ARN of the Secrets Manager secret for DB credentials.
  - `DB_NAME`: Name of the PostgreSQL database.
  - `DB_HOST`: Hostname of the RDS instance.
  - `DB_PORT`: Port of the RDS instance (default: `5432`).
  - `REGION`: AWS region.
  - `METADATA_FIELDS`: Metadata fields to store with each chunk.

- **Knowledge Base Handler**:
  - `CHUNK_SIZE`: Size of each text chunk (default: `800`).
  - `OVERLAP`: Overlap between consecutive chunks (default: `100`).

---

## Troubleshooting

1. **Lambda Connectivity**:
   - If Lambda cannot connect to RDS, verify:
     - The VPC, subnets, and security groups are correctly configured.
     - The Lambda function has the necessary IAM permissions.

2. **Bedrock Invocation**:
   - If Bedrock invocation fails, ensure:
     - Bedrock service is enabled in your account and region.
     - The `modelId` in the Lambda functions matches the available model.

3. **S3 Key Not Found**:
   - Ensure the CI/CD pipeline uploads the Lambda artifacts (`ingest.zip` and `knowledgebase_handler.zip`) to the correct S3 bucket and key.

---

## Security Notes

- **IAM Policies**:
  - Ensure IAM policies follow the principle of least privilege.
  - The Lambda execution role includes permissions for S3, Secrets Manager, RDS, and Bedrock.

- **RDS Accessibility**:
  - The RDS instance is deployed in private subnets for security.
  - Ensure the database is not publicly accessible in production.

---

## Future Enhancements

1. **Search and Retrieval**:
   - Implement a search API to retrieve document chunks based on metadata or embeddings.

2. **Additional File Formats**:
   - Extend support for more file formats (e.g., `.docx`, `.xlsx`).

3. **Monitoring and Alerts**:
   - Add CloudWatch alarms and dashboards for monitoring the pipeline.

4. **Scalability**:
   - Use AWS Step Functions for orchestrating complex workflows.
   - Add support for distributed processing of large documents.