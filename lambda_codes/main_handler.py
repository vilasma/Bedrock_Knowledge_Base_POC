"""
Bedrock Knowledge Base POC - S3-based Ingestion Handler with RDS Vector Store

ARCHITECTURE:
1. Documents uploaded to S3 (bedrock-poc-docs/) trigger this Lambda
2. Lambda creates metadata JSON files in S3 alongside documents
3. Lambda tracks document status in Aurora (documents table)
4. Bedrock Knowledge Base syncs from S3 and populates bedrock_kb_documents table
5. For queries, Lambda calls Bedrock KB Retrieve API and stores results in query_results table

TABLES:
- bedrock_kb_documents: Vector store (managed by Bedrock)
- documents: Document tracking with status, tenant_id, user_id, etc.
- query_results: Query results with similarity_score, chunk_text, etc.
- query_history: Query tracking
"""

import os
import io
import json
import uuid
import time
import logging
import boto3
import psycopg2
import pdfplumber
import docx

# ---------------- Logger ----------------
logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)

# ---------------- Config ----------------
S3_BUCKET = os.environ['S3_BUCKET_NAME']
S3_INCOMING_PREFIX = "bedrock-poc-docs/"
REGION = os.environ.get('REGION', 'us-east-1')

DB_HOST = os.environ['DB_HOST']
DB_PORT = int(os.environ.get('DB_PORT', 5432))
DB_NAME = os.environ['DB_NAME']
DB_SECRET_ARN = os.environ['DB_SECRET_ARN']

KNOWLEDGE_BASE_ID = os.environ['KB_ID']
DATA_SOURCE_ID = os.environ['DATA_SOURCE_ID']

CHUNK_SIZE = int(os.environ.get('CHUNK_SIZE', 300))
TOP_K = int(os.environ.get('TOP_K', 5))
MAX_POLL_SECONDS = int(os.environ.get('MAX_POLL_SECONDS', 120))
POLL_INTERVAL = int(os.environ.get('POLL_INTERVAL', 5))

# ---------------- AWS Clients ----------------
boto_session = boto3.session.Session(region_name=REGION)
secrets_client = boto_session.client('secretsmanager')
s3 = boto_session.client('s3')
bedrock_agent = boto_session.client('bedrock-agent')
bedrock_runtime = boto_session.client('bedrock-runtime')

# ---------------- DB Helpers ----------------
def get_db_credentials(secret_arn):
    secret = secrets_client.get_secret_value(SecretId=secret_arn)
    creds = json.loads(secret['SecretString'])
    return creds['username'], creds['password']

def get_db_conn():
    username, password = get_db_credentials(DB_SECRET_ARN)
    return psycopg2.connect(
        host=DB_HOST, port=DB_PORT, dbname=DB_NAME,
        user=username, password=password, connect_timeout=10
    )

# ---------------- Text Extraction ----------------
def extract_text_from_s3(bucket, key):
    """Extract text from PDF, DOCX, or TXT files in S3"""
    obj = s3.get_object(Bucket=bucket, Key=key)
    raw = obj['Body'].read()
    ext = key.split('.')[-1].lower()

    if ext == 'pdf':
        with pdfplumber.open(io.BytesIO(raw)) as pdf:
            return "\n".join([p.extract_text() or "" for p in pdf.pages]).strip()
    elif ext == 'docx':
        doc = docx.Document(io.BytesIO(raw))
        return "\n".join([p.text for p in doc.paragraphs]).strip()
    else:
        return raw.decode('utf-8', errors='ignore').strip()

# ---------------- Text Chunking ----------------
def split_chunk_text(text, chunk_size=CHUNK_SIZE, overlap=50):
    """
    Split text into overlapping chunks based on word count.

    Args:
        text: Full text to chunk
        chunk_size: Number of words per chunk
        overlap: Number of overlapping words between chunks

    Returns:
        List of text chunks
    """
    words = text.split()
    chunks = []

    for i in range(0, len(words), chunk_size - overlap):
        chunk = ' '.join(words[i:i + chunk_size])
        if chunk.strip():
            chunks.append(chunk)

        # Break if we've reached the end
        if i + chunk_size >= len(words):
            break

    return chunks

# ---------------- Embeddings ----------------
def generate_embedding(text):
    """
    Generate embedding using Bedrock Titan model.

    Args:
        text: Text to embed

    Returns:
        List of 1536 floats (embedding vector)
    """
    try:
        response = bedrock_runtime.invoke_model(
            modelId='amazon.titan-embed-text-v1',
            body=json.dumps({"inputText": text})
        )

        result = json.loads(response['body'].read())
        return result['embedding']

    except Exception as e:
        logger.error(f"Failed to generate embedding: {e}")
        return None

# ---------------- Store Chunks in Aurora ----------------
def store_chunks_in_aurora(document_id, chunks, metadata_dict):
    """
    Store document chunks with embeddings in Aurora document_chunks table.

    Args:
        document_id: UUID of the parent document
        chunks: List of text chunks
        metadata_dict: Metadata to store with each chunk

    Returns:
        Number of chunks successfully stored
    """
    conn = get_db_conn()
    stored_count = 0

    try:
        with conn:
            with conn.cursor() as cur:
                for idx, chunk_text in enumerate(chunks):
                    try:
                        # Generate embedding for this chunk
                        logger.info(f"Generating embedding for chunk {idx+1}/{len(chunks)}")
                        embedding = generate_embedding(chunk_text)

                        if not embedding:
                            logger.warning(f"Skipping chunk {idx} - no embedding generated")
                            continue

                        # Store chunk with embedding
                        cur.execute("""
                            INSERT INTO document_chunks
                            (document_id, chunk_index, chunk_text, embedding, metadata, status)
                            VALUES (%s, %s, %s, %s, %s, 'completed')
                            ON CONFLICT (document_id, chunk_index)
                            DO UPDATE SET
                                chunk_text = EXCLUDED.chunk_text,
                                embedding = EXCLUDED.embedding,
                                metadata = EXCLUDED.metadata,
                                status = 'completed',
                                updated_at = NOW()
                        """, (
                            document_id,
                            idx,
                            chunk_text,
                            embedding,
                            json.dumps(metadata_dict)
                        ))

                        stored_count += 1
                        logger.info(f"✅ Chunk {idx} stored successfully")

                    except Exception as chunk_error:
                        logger.error(f"Failed to store chunk {idx}: {chunk_error}")

                        # Log failed chunk
                        try:
                            cur.execute("""
                                INSERT INTO failed_chunks
                                (document_id, chunk_index, chunk_text, error_reason)
                                VALUES (%s, %s, %s, %s)
                            """, (document_id, idx, chunk_text, str(chunk_error)))
                        except:
                            pass

        conn.commit()
        logger.info(f"✅ Stored {stored_count}/{len(chunks)} chunks in Aurora")
        return stored_count

    except Exception as e:
        logger.exception(f"Failed to store chunks: {e}")
        conn.rollback()
        return stored_count
    finally:
        conn.close()

# ---------------- Document Tracking ----------------
def insert_document_record(s3_key, metadata_dict):
    """
    Insert document record into tracking table.
    Returns document_id.
    """
    conn = get_db_conn()
    doc_id = str(uuid.uuid4())

    try:
        with conn:
            with conn.cursor() as cur:
                # Insert document with metadata fields directly in table
                cur.execute("""
                    INSERT INTO documents
                    (document_id, document_name, s3_key, status, tenant_id, user_id, project_id, thread_id)
                    VALUES (%s, %s, %s, 'processing', %s, %s, %s, %s)
                """, (
                    doc_id,
                    os.path.basename(s3_key),
                    s3_key,
                    metadata_dict.get('tenant_id'),
                    metadata_dict.get('user_id'),
                    metadata_dict.get('project_id'),
                    metadata_dict.get('thread_id')
                ))

                # Insert additional metadata fields
                for key, value in metadata_dict.items():
                    if key not in ['tenant_id', 'user_id', 'project_id', 'thread_id']:
                        cur.execute("""
                            INSERT INTO metadata (metadata_id, document_id, metadata_key, metadata_value)
                            VALUES (gen_random_uuid(), %s, %s, %s)
                        """, (doc_id, key, str(value)))

        conn.commit()
        logger.info(f"✅ Document {doc_id} tracked in Aurora")
        return doc_id

    except Exception as e:
        logger.exception(f"Failed to insert document record: {e}")
        conn.rollback()
        raise
    finally:
        conn.close()

def update_document_status(doc_id, status, job_id=None, error_message=None, chunk_count=None):
    """
    Update document status after Bedrock ingestion.
    Status: 'pending', 'processing', 'completed', 'failed'
    """
    conn = get_db_conn()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute("""
                    UPDATE documents
                    SET status = %s,
                        ingestion_job_id = COALESCE(%s, ingestion_job_id),
                        error_message = %s,
                        chunk_count = COALESCE(%s, chunk_count),
                        updated_at = NOW()
                    WHERE document_id = %s
                """, (status, job_id, error_message, chunk_count, doc_id))

        conn.commit()
        logger.info(f"✅ Document {doc_id} status updated to '{status}'")

    except Exception as e:
        logger.exception(f"Failed to update document status: {e}")
        conn.rollback()
    finally:
        conn.close()

def get_document_status(document_id=None, s3_key=None):
    """
    Query document ingestion status by document_id or s3_key.

    Returns:
        dict with keys: document_id, document_name, s3_key, status, ingestion_job_id,
                        chunk_count, error_message, tenant_id, user_id, project_id,
                        thread_id, created_at, updated_at
        None if not found
    """
    if not document_id and not s3_key:
        raise ValueError("Either document_id or s3_key must be provided")

    conn = get_db_conn()
    try:
        with conn.cursor() as cur:
            if document_id:
                cur.execute("""
                    SELECT document_id, document_name, s3_key, status,
                           ingestion_job_id, chunk_count, error_message,
                           tenant_id, user_id, project_id, thread_id,
                           created_at, updated_at
                    FROM documents
                    WHERE document_id = %s
                """, (document_id,))
            else:
                cur.execute("""
                    SELECT document_id, document_name, s3_key, status,
                           ingestion_job_id, chunk_count, error_message,
                           tenant_id, user_id, project_id, thread_id,
                           created_at, updated_at
                    FROM documents
                    WHERE s3_key = %s
                """, (s3_key,))

            row = cur.fetchone()
            if row:
                return {
                    'document_id': str(row[0]),
                    'document_name': row[1],
                    's3_key': row[2],
                    'status': row[3],
                    'ingestion_job_id': row[4],
                    'chunk_count': row[5],
                    'error_message': row[6],
                    'tenant_id': row[7],
                    'user_id': row[8],
                    'project_id': row[9],
                    'thread_id': row[10],
                    'created_at': row[11].isoformat() if row[11] else None,
                    'updated_at': row[12].isoformat() if row[12] else None
                }
            return None

    except Exception as e:
        logger.exception(f"Failed to get document status: {e}")
        return None
    finally:
        conn.close()

def get_documents_by_status(status=None, tenant_id=None, user_id=None, limit=100):
    """
    Query multiple documents by status and/or tenant/user.

    Args:
        status: Filter by status ('pending', 'processing', 'completed', 'failed')
        tenant_id: Filter by tenant_id
        user_id: Filter by user_id
        limit: Maximum number of results (default: 100)

    Returns:
        List of document status dictionaries
    """
    conn = get_db_conn()
    try:
        with conn.cursor() as cur:
            # Build dynamic query
            query = """
                SELECT document_id, document_name, s3_key, status,
                       ingestion_job_id, chunk_count, error_message,
                       tenant_id, user_id, project_id, thread_id,
                       created_at, updated_at
                FROM documents
                WHERE 1=1
            """
            params = []

            if status:
                query += " AND status = %s"
                params.append(status)

            if tenant_id:
                query += " AND tenant_id = %s"
                params.append(tenant_id)

            if user_id:
                query += " AND user_id = %s"
                params.append(user_id)

            query += " ORDER BY created_at DESC LIMIT %s"
            params.append(limit)

            cur.execute(query, params)
            rows = cur.fetchall()

            results = []
            for row in rows:
                results.append({
                    'document_id': str(row[0]),
                    'document_name': row[1],
                    's3_key': row[2],
                    'status': row[3],
                    'ingestion_job_id': row[4],
                    'chunk_count': row[5],
                    'error_message': row[6],
                    'tenant_id': row[7],
                    'user_id': row[8],
                    'project_id': row[9],
                    'thread_id': row[10],
                    'created_at': row[11].isoformat() if row[11] else None,
                    'updated_at': row[12].isoformat() if row[12] else None
                })

            return results

    except Exception as e:
        logger.exception(f"Failed to get documents by status: {e}")
        return []
    finally:
        conn.close()

# ---------------- S3 Metadata File Creation ----------------
def create_s3_metadata_file(s3_key, metadata_dict):
    """
    Create a .metadata.json file in S3 for Bedrock to ingest.
    Bedrock requires a specific format with 'metadataAttributes' key.

    Format:
    {
        "metadataAttributes": {
            "tenant_id": "value",
            "user_id": "value",
            ...
        }
    }
    """
    metadata_key = s3_key + ".metadata.json"

    # Bedrock-compatible metadata format
    bedrock_metadata = {
        "metadataAttributes": metadata_dict
    }

    try:
        s3.put_object(
            Bucket=S3_BUCKET,
            Key=metadata_key,
            Body=json.dumps(bedrock_metadata),
            ContentType='application/json'
        )
        logger.info(f"✅ Metadata file created: {metadata_key}")
        logger.info(f"Metadata content: {json.dumps(bedrock_metadata, indent=2)}")
        return metadata_key
    except Exception as e:
        logger.error(f"Failed to create metadata file: {e}")
        return None

# ---------------- Bedrock KB Ingestion ----------------
def trigger_bedrock_ingestion():
    """Trigger Bedrock Knowledge Base ingestion job"""
    try:
        resp = bedrock_agent.start_ingestion_job(
            knowledgeBaseId=KNOWLEDGE_BASE_ID,
            dataSourceId=DATA_SOURCE_ID
        )
        job_id = resp.get("ingestionJob", {}).get("ingestionJobId")
        logger.info(f"✅ Bedrock ingestion job started: {job_id}")
        return job_id
    except Exception as e:
        logger.exception(f"Error triggering Bedrock ingestion: {e}")
        return None

def wait_for_bedrock_job(job_id):
    """Wait for Bedrock ingestion job to complete"""
    elapsed = 0
    while elapsed < MAX_POLL_SECONDS:
        try:
            resp = bedrock_agent.get_ingestion_job(
                ingestionJobId=job_id,
                knowledgeBaseId=KNOWLEDGE_BASE_ID,
                dataSourceId=DATA_SOURCE_ID
            )
            job_info = resp.get("ingestionJob", {})
            status = job_info.get("status")
            statistics = job_info.get("statistics", {})

            if status in ("COMPLETE", "FAILED"):
                # Extract statistics for chunk count
                chunk_count = statistics.get("numberOfDocumentsScanned", 0)
                failure_reason = job_info.get("failureReasons", ["N/A"])[0] if status == "FAILED" else None
                return status.lower(), failure_reason, chunk_count

        except Exception as e:
            logger.error(f"Error polling job: {e}")
            return "failed", str(e), 0

        time.sleep(POLL_INTERVAL)
        elapsed += POLL_INTERVAL

    return "failed", "Job polling timeout", 0

# ---------------- Bedrock KB Retrieve API ----------------
def retrieve_from_knowledge_base(query_text, k=TOP_K, tenant_id=None, user_id=None, document_ids=None, project_id=None, thread_id=None):
    """
    Query Bedrock Knowledge Base and store results in Aurora.
    This populates query_history and query_results tables with:
    - query_text
    - similarity_score
    - chunk_text
    - document_id
    - chunk_index

    Args:
        query_text: The search query
        k: Number of results to return
        tenant_id: Filter by tenant_id
        user_id: Filter by user_id
        document_ids: List of document IDs to filter by (e.g., ["doc-1", "doc-2"])
        project_id: Filter by project_id
        thread_id: Filter by thread_id
    """
    query_id = str(uuid.uuid4())
    start_time = time.time()

    try:
        # Build metadata filter configuration
        filter_conditions = []

        if document_ids:
            # Filter by specific document IDs
            if isinstance(document_ids, list) and len(document_ids) > 1:
                # Multiple document IDs - use OR condition
                filter_conditions.append({
                    'orAll': [{'equals': {'key': 'document_id', 'value': doc_id}} for doc_id in document_ids]
                })
            elif isinstance(document_ids, list) and len(document_ids) == 1:
                filter_conditions.append({'equals': {'key': 'document_id', 'value': document_ids[0]}})
            elif isinstance(document_ids, str):
                filter_conditions.append({'equals': {'key': 'document_id', 'value': document_ids}})

        if tenant_id:
            filter_conditions.append({'equals': {'key': 'tenant_id', 'value': tenant_id}})

        if user_id:
            filter_conditions.append({'equals': {'key': 'user_id', 'value': user_id}})

        if project_id:
            filter_conditions.append({'equals': {'key': 'project_id', 'value': project_id}})

        if thread_id:
            filter_conditions.append({'equals': {'key': 'thread_id', 'value': thread_id}})

        # Build retrieval configuration
        retrieval_config = {
            'vectorSearchConfiguration': {
                'numberOfResults': k
            }
        }

        # Add filter if any conditions exist
        if filter_conditions:
            if len(filter_conditions) == 1:
                retrieval_config['vectorSearchConfiguration']['filter'] = filter_conditions[0]
            else:
                # Multiple conditions - use AND
                retrieval_config['vectorSearchConfiguration']['filter'] = {
                    'andAll': filter_conditions
                }

        logger.info(f"Retrieval config: {json.dumps(retrieval_config, indent=2)}")

        # Execute Bedrock retrieval
        response = bedrock_agent.retrieve(
            knowledgeBaseId=KNOWLEDGE_BASE_ID,
            retrievalQuery={'text': query_text},
            retrievalConfiguration=retrieval_config
        )

        execution_time_ms = int((time.time() - start_time) * 1000)
        retrieval_results = response.get('retrievalResults', [])

        # Store query and results in Aurora
        conn = get_db_conn()
        try:
            with conn:
                with conn.cursor() as cur:
                    # Insert query history
                    cur.execute("""
                        INSERT INTO query_history
                        (query_id, query_text, tenant_id, user_id, top_k, execution_time_ms, result_count)
                        VALUES (%s, %s, %s, %s, %s, %s, %s)
                    """, (query_id, query_text, tenant_id, user_id, k, execution_time_ms, len(retrieval_results)))

                    # Insert query results
                    results = []
                    for rank, item in enumerate(retrieval_results, start=1):
                        content_text = item.get('content', {}).get('text', '')
                        similarity_score = item.get('score', 0.0)
                        location = item.get('location', {})
                        s3_location = location.get('s3Location', {}).get('uri', '')
                        metadata = item.get('metadata', {})

                        # Try to match document by S3 key
                        document_id = None
                        chunk_index = None
                        chunk_id = None

                        if s3_location:
                            try:
                                # Extract S3 key from URI
                                s3_key = s3_location.replace(f"s3://{S3_BUCKET}/", "")
                                cur.execute("""
                                    SELECT document_id FROM documents WHERE s3_key = %s
                                """, (s3_key,))
                                row = cur.fetchone()
                                if row:
                                    document_id = row[0]

                                # Try to extract chunk_index from metadata
                                chunk_index = metadata.get('chunk_index')
                                chunk_id = metadata.get('chunk_id')

                            except Exception as e:
                                logger.warning(f"Failed to match S3 location: {e}")

                        # Insert query result
                        cur.execute("""
                            INSERT INTO query_results
                            (result_id, query_id, document_id, chunk_id, chunk_index,
                             chunk_text, similarity_score, result_rank, s3_location, metadata)
                            VALUES (gen_random_uuid(), %s, %s, %s, %s, %s, %s, %s, %s, %s)
                        """, (
                            query_id, document_id, chunk_id, chunk_index,
                            content_text, similarity_score, rank, s3_location, json.dumps(metadata)
                        ))

                        results.append({
                            'rank': rank,
                            'content': content_text,
                            'score': similarity_score,
                            'document_id': document_id,
                            'chunk_index': chunk_index,
                            'metadata': metadata
                        })

            conn.commit()
            logger.info(f"✅ Query {query_id} stored with {len(results)} results")
            return results

        finally:
            conn.close()

    except Exception as e:
        logger.exception(f"Bedrock KB retrieve failed: {e}")
        return []

# ---------------- Lambda Handler ----------------
def lambda_handler(event, context):
    """
    Main handler supporting multiple operations:

    1. S3 Event Processing (default):
       - Extract text from uploaded document
       - Create metadata JSON file in S3
       - Insert document tracking record in Aurora
       - Trigger Bedrock ingestion job
       - Wait for job completion
       - Update document status in Aurora

    2. API Operations (via direct invocation):
       - action: 'get_status' - Get document status by document_id or s3_key
       - action: 'get_documents' - Query documents by status/tenant/user
       - action: 'query' - Query knowledge base with filters

    Event formats:

    S3 Event:
    {
        "Records": [{"s3": {...}}]
    }

    API Events:
    {
        "action": "get_status",
        "document_id": "uuid" OR "s3_key": "path/to/doc"
    }
    {
        "action": "get_documents",
        "status": "completed",
        "tenant_id": "tenant-123",
        "user_id": "user-456",
        "limit": 100
    }
    {
        "action": "query",
        "query_text": "search terms",
        "filters": {
            "tenant_id": "tenant-123",
            "user_id": "user-456",
            "document_ids": ["doc-1", "doc-2"],
            "project_id": "project-001",
            "thread_id": "thread-999"
        },
        "top_k": 10
    }
    """

    # Check if this is an API call (not S3 event)
    action = event.get('action')

    if action == 'get_status':
        # Get single document status
        try:
            document_id = event.get('document_id')
            s3_key = event.get('s3_key')

            status = get_document_status(document_id=document_id, s3_key=s3_key)

            if status:
                return {
                    "statusCode": 200,
                    "body": json.dumps(status)
                }
            else:
                return {
                    "statusCode": 404,
                    "body": json.dumps({"error": "Document not found"})
                }
        except Exception as e:
            logger.exception(f"Error getting document status: {e}")
            return {
                "statusCode": 500,
                "body": json.dumps({"error": str(e)})
            }

    elif action == 'get_documents':
        # Get multiple documents by filters
        try:
            status = event.get('status')
            tenant_id = event.get('tenant_id')
            user_id = event.get('user_id')
            limit = event.get('limit', 100)

            documents = get_documents_by_status(
                status=status,
                tenant_id=tenant_id,
                user_id=user_id,
                limit=limit
            )

            return {
                "statusCode": 200,
                "body": json.dumps({
                    "count": len(documents),
                    "documents": documents
                })
            }
        except Exception as e:
            logger.exception(f"Error getting documents: {e}")
            return {
                "statusCode": 500,
                "body": json.dumps({"error": str(e)})
            }

    elif action == 'query':
        # Query knowledge base with filters
        try:
            query_text = event.get('query_text')
            if not query_text:
                return {
                    "statusCode": 400,
                    "body": json.dumps({"error": "query_text is required"})
                }

            filters = event.get('filters', {})
            top_k = event.get('top_k', TOP_K)

            results = retrieve_from_knowledge_base(
                query_text=query_text,
                k=top_k,
                tenant_id=filters.get('tenant_id'),
                user_id=filters.get('user_id'),
                document_ids=filters.get('document_ids'),
                project_id=filters.get('project_id'),
                thread_id=filters.get('thread_id')
            )

            return {
                "statusCode": 200,
                "body": json.dumps({
                    "query": query_text,
                    "filters": filters,
                    "count": len(results),
                    "results": results
                })
            }
        except Exception as e:
            logger.exception(f"Error querying knowledge base: {e}")
            return {
                "statusCode": 500,
                "body": json.dumps({"error": str(e)})
            }

    # Default: S3 event processing
    results = []

    for record in event.get("Records", []):
        s3_key = record["s3"]["object"]["key"]

        # Skip metadata files and non-document files
        if not s3_key.startswith(S3_INCOMING_PREFIX) or s3_key.endswith('.metadata.json'):
            continue

        logger.info(f"📄 Processing document: {s3_key}")

        try:
            # Extract text (for validation, not used by Bedrock)
            text = extract_text_from_s3(S3_BUCKET, s3_key)
            if not text.strip():
                logger.warning(f"Empty document: {s3_key}")
                results.append({"file": s3_key, "status": "empty"})
                continue

            # Generate metadata
            metadata_dict = {
                "document_id": str(uuid.uuid4()),
                "tenant_id": f"tenant-{uuid.uuid4().hex[:8]}",
                "user_id": f"user-{uuid.uuid4().hex[:8]}",
                "project_id": f"project-{uuid.uuid4().hex[:8]}",
                "thread_id": f"thread-{uuid.uuid4().hex[:8]}",
                "source": "s3_upload",
                "file_type": s3_key.split('.')[-1].lower()
            }

            # Create metadata file in S3 for Bedrock
            metadata_key = create_s3_metadata_file(s3_key, metadata_dict)

            # Insert document tracking record
            doc_id = insert_document_record(s3_key, metadata_dict)

            # Chunk the document text
            logger.info(f"Chunking document text ({len(text)} chars)")
            chunks = split_chunk_text(text, chunk_size=CHUNK_SIZE)
            logger.info(f"Created {len(chunks)} chunks")

            # Store chunks with embeddings in Aurora
            logger.info("Storing chunks in Aurora with embeddings...")
            stored_count = store_chunks_in_aurora(doc_id, chunks, metadata_dict)

            # Update chunk count in documents table
            update_document_status(
                doc_id,
                status="processing",
                chunk_count=stored_count
            )

            # Trigger Bedrock ingestion
            job_id = trigger_bedrock_ingestion()
            if not job_id:
                update_document_status(doc_id, "failed", error_message="Failed to start ingestion job")
                results.append({"file": s3_key, "status": "failed", "reason": "No job ID", "chunks_stored": stored_count})
                continue

            # Wait for ingestion to complete
            job_status, failure_reason, bedrock_chunk_count = wait_for_bedrock_job(job_id)

            # Update document status
            update_document_status(
                doc_id,
                status=job_status,
                job_id=job_id,
                error_message=failure_reason if job_status == "failed" else None,
                chunk_count=stored_count  # Use our stored count, not Bedrock's
            )

            results.append({
                "file": s3_key,
                "document_id": doc_id,
                "status": job_status,
                "job_id": job_id,
                "chunk_count": stored_count,
                "chunks_stored_in_aurora": stored_count,
                "bedrock_ingestion_count": bedrock_chunk_count,
                "metadata_file": metadata_key
            })

            logger.info(f"✅ Document {s3_key} processed: {job_status}")

        except Exception as e:
            logger.exception(f"Failed processing {s3_key}: {e}")
            results.append({"file": s3_key, "status": "error", "reason": str(e)})

    return {
        "statusCode": 200,
        "body": json.dumps({"processed": results})
    }
