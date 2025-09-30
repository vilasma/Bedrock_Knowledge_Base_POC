# S3 -> Bedrock -> PostgreSQL PoC

This PoC creates a pipeline that:

1. Accepts file uploads to an S3 bucket.
2. Triggers a Lambda function on new file uploads.
3. Lambda chunks text, calls AWS Bedrock to compute embeddings.
4. Stores embeddings and metadata in PostgreSQL (RDS).

---

## How it Works

1. Use the GitHub Actions workflow to package the Lambda code and upload `ingest.zip` to the bucket configured in the `CFN_CODE_BUCKET` secret.
2. The workflow then deploys the CloudFormation stack. The CloudFormation template provisions a VPC with public + private subnets, NAT gateway, RDS in private subnets, Secrets Manager for DB credentials, Lambda in the private subnets, and an S3 bucket that triggers the Lambda.
3. After deployment, upload documents to the S3 bucket output in CloudFormation outputs. Lambda will run and insert chunks into the database.

---

## Post-deploy Manual Steps

1. Connect to the RDS instance using the credentials stored in Secrets Manager (the Secrets Manager secret ARN is available in the CloudFormation outputs).
2. Run `db/schema.sql` to:
   - Create the `document_chunks` table.
   - Create indexes.
   - Enable the `vector` extension.
   - Note: Some RDS/Aurora instances may require enabling extensions differently; adapt as needed.
3. Ensure the `modelId` in `lambda/ingest.py` matches the Bedrock model available for your account/region.

---

## Security Notes

- **RDS Accessibility**: This PoC enables public accessibility for simplicity. For production:
  - Place the database in private subnets.
  - Configure Lambda to run in the same VPC.
- **IAM Policies**: Limit IAM policies to the principle of least privilege in production.

---

## Troubleshooting

- **Lambda Connectivity**: If Lambda cannot reach RDS, verify the networking configuration (VPC, subnets, security groups) allows connectivity.
- **Bedrock Invocation**: If Bedrock invocation fails, ensure:
  - Bedrock service access is correctly configured.
  - The `modelId` is valid for your account/region.