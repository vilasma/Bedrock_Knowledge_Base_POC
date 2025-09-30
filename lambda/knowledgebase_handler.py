import os
import json
import boto3
import logging
from typing import List, Dict

logger = logging.getLogger()
logger.setLevel(logging.INFO)

bedrock = boto3.client('bedrock-runtime')

CHUNK_SIZE = int(os.environ.get('CHUNK_SIZE', 800))
OVERLAP = int(os.environ.get('OVERLAP', 100))

def chunk_text(text: str, chunk_size: int = CHUNK_SIZE, overlap: int = OVERLAP) -> List[str]:
    """Splits text into overlapping chunks."""
    if not text:
        return []
    chunks = []
    start = 0
    length = len(text)
    while start < length:
        end = min(start + chunk_size, length)
        chunk = text[start:end]
        chunks.append(chunk)
        start = end - overlap
        if start < 0:
            start = 0
    return chunks

def generate_embeddings(chunks: List[str], metadata: Dict[str, str]) -> List[Dict[str, any]]:
    """Generates embeddings for each chunk using AWS Bedrock."""
    results = []
    for chunk in chunks:
        payload = {
            "input": chunk,
            "metadata": metadata
        }
        response = bedrock.invoke_model(
            modelId='amazon.titan-embed-text-v1',
            contentType='application/json',
            body=json.dumps(payload)
        )
        body = response['body'].read()
        data = json.loads(body)
        if 'embedding' in data:
            results.append({
                "chunk": chunk,
                "embedding": data['embedding']
            })
        else:
            raise RuntimeError(f"Unexpected Bedrock response: {data}")
    return results

def lambda_handler(event, context):
    """Lambda handler for processing text and generating embeddings."""
    logger.info("Event received: %s", json.dumps(event))

    # Extract input text and metadata from the event
    text = event.get('text', '')
    metadata = event.get('metadata', {})

    if not text:
        raise ValueError("No text provided for processing.")

    # Chunk the text
    chunks = chunk_text(text)
    logger.info("Text split into %d chunks.", len(chunks))

    # Generate embeddings for each chunk
    embeddings = generate_embeddings(chunks, metadata)
    logger.info("Generated embeddings for %d chunks.", len(embeddings))

    # Return the chunks and embeddings
    return {
        "status": "success",
        "chunks": embeddings
    }
