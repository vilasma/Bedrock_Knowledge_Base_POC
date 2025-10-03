-- Create extension for vector operations
CREATE EXTENSION IF NOT EXISTS vector;

-- Create table for chunks (run once after DB is reachable)
CREATE TABLE IF NOT EXISTS document_chunks (
    id SERIAL PRIMARY KEY,
    tenant_id VARCHAR(128),
    user_id VARCHAR(128),
    document_id VARCHAR(256) UNIQUE,
    project_id VARCHAR(128),
    thread_id VARCHAR(128),
    chunks TEXT,
    embedding_vector vector(1536),
    metadata JSONB,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Index for fast similarity search
CREATE INDEX IF NOT EXISTS idx_document_embedding 
ON document_chunks USING ivfflat (embedding_vector vector_l2_ops) 
WITH (lists = 100);

-- Index on document_id for filtering
CREATE INDEX IF NOT EXISTS idx_document_id 
ON document_chunks(document_id);