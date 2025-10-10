import json
import boto3
import urllib3
import traceback
from opensearchpy import OpenSearch, RequestsHttpConnection, AWSV4SignerAuth
import time

http = urllib3.PoolManager()


# ---------------- CFN Response ----------------
def send_cfn_response(event, context, status, reason=None):
    if "ResponseURL" not in event:
        print(f"[CFN] Not CFN invocation. Status: {status}, Reason: {reason}")
        return
    body = {
        "Status": status,
        "Reason": reason or f"See CloudWatch Log Stream: {context.log_stream_name}",
        "PhysicalResourceId": context.log_stream_name,
        "StackId": event.get("StackId"),
        "RequestId": event.get("RequestId"),
        "LogicalResourceId": event.get("LogicalResourceId"),
        "Data": {}
    }
    try:
        http.request(
            "PUT",
            event["ResponseURL"],
            body=json.dumps(body),
            headers={
                "Content-Type": "application/json",
                "Content-Length": str(len(json.dumps(body)))
            }
        )
    except Exception as e:
        print(f"[ERROR] Failed CFN response: {e}")

def lambda_handler(event, context):
    print(f"Event: {json.dumps(event)}")

    try:
        collection_endpoint = event['ResourceProperties']['CollectionEndpoint']
        index_name = event['ResourceProperties']['IndexName']
        region = event['ResourceProperties']['Region']

        # Clean up the endpoint
        host = collection_endpoint.replace('https://', '').replace('http://', '')

        # Get credentials
        credentials = boto3.Session().get_credentials()
        auth = AWSV4SignerAuth(credentials, region, 'aoss')

        # Create OpenSearch client
        os_client = OpenSearch(
            hosts=[{'host': host, 'port': 443}],
            http_auth=auth,
            use_ssl=True,
            verify_certs=True,
            connection_class=RequestsHttpConnection,
            timeout=300
        )

        # Check if index exists
        if os_client.indices.exists(index=index_name):
            print(f"Index {index_name} already exists")
            send_cfn_response(event, context, "SUCCESS", {'IndexName': index_name})
            return {"statusCode": 200, "body": json.dumps({"message": "Index already exists"})}

        # Create index with mapping
        mapping = {
            "settings": {
                "index": {
                    "knn": True,
                    "knn.algo_param.ef_search": 512
                }
            },
            "mappings": {
                "properties": {
                    "embedding_vector": {
                        "type": "knn_vector",
                        "dimension": 1536,
                        "method": {
                            "engine": "faiss",
                            "name": "hnsw",
                            "space_type": "l2",
                            "parameters": {
                                "ef_construction": 512,
                                "m": 16
                            }
                        }
                    },
                    "chunk_text": {"type": "text"},
                    "metadata": {"type": "text"},
                    "document_id": {"type": "text"}
                }
            }
        }

        os_client.indices.create(index=index_name, body=mapping)
        print(f"Index {index_name} created successfully")

        # Wait a bit for propagation
        time.sleep(30)

        send_cfn_response(event, context, "SUCCESS", {'IndexName': index_name})

    except Exception as e:
        print(f"Error: {str(e)}")
        traceback.print_exc()
        send_cfn_response(event, context, "FAILED", {'IndexName': index_name})
        return {"statusCode": 500, "body": {'IndexName': index_name}}
