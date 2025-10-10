import json
import boto3
import cfnresponse
from opensearchpy import OpenSearch, RequestsHttpConnection, AWSV4SignerAuth
import time

def lambda_handler(event, context):
    print(f"Event: {json.dumps(event)}")

    try:
        if event['RequestType'] == 'Delete':
            cfnresponse.send(event, context, cfnresponse.SUCCESS, {})
            return

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
            cfnresponse.send(event, context, cfnresponse.SUCCESS, {'IndexName': index_name})
            return

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

        cfnresponse.send(event, context, cfnresponse.SUCCESS, {'IndexName': index_name})

    except Exception as e:
        print(f"Error: {str(e)}")
        import traceback
        traceback.print_exc()
        cfnresponse.send(event, context, cfnresponse.FAILED, {'Error': str(e)})
