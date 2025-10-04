from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
import boto3
import os
import json

REGION = os.environ.get('REGION', 'us-east-1')
KNOWLEDGEBASE_LAMBDA = os.environ.get('KNOWLEDGEBASE_LAMBDA', 'poc-kb-handler')
TOP_K = int(os.environ.get('TOP_K', 5))

lambda_client = boto3.client('lambda', region_name=REGION)

app = FastAPI(title="RAG LLM API")

class QueryRequest(BaseModel):
    query: str
    document_ids: list[str] = None
    tenant_id: str = None
    project_id: str = None

@app.post("/query")
def query_rag(req: QueryRequest):
    if not req.query:
        raise HTTPException(status_code=400, detail="Query text is required")
    
    payload = {
        "query": req.query,
        "filters": {}
    }
    if req.document_ids:
        payload["filters"]["document_ids"] = req.document_ids
    if req.tenant_id:
        payload["filters"]["tenant_id"] = req.tenant_id
    if req.project_id:
        payload["filters"]["project_id"] = req.project_id

    response = lambda_client.invoke(
        FunctionName=KNOWLEDGEBASE_LAMBDA,
        InvocationType='RequestResponse',
        Payload=json.dumps(payload)
    )

    result_payload = json.loads(response['Payload'].read())
    if result_payload.get("statusCode") != 200:
        raise HTTPException(status_code=500, detail=result_payload.get("body"))

    return json.loads(result_payload['body'])
