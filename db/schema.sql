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

-- Enable the pgcrypto extension for gen_random_uuid
CREATE EXTENSION IF NOT EXISTS pgcrypto;

-- Update document_id column with new UUIDs
UPDATE document_chunks
SET document_id = gen_random_uuid();

-- Alter column to UUID type (should succeed now)
ALTER TABLE document_chunks
    ALTER COLUMN document_id TYPE UUID USING document_id::uuid;

-- Ensure uniqueness
ALTER TABLE document_chunks
ADD CONSTRAINT document_id_unique UNIQUE (document_id);

ALTER TABLE document_chunks
ALTER COLUMN embedding_vector TYPE vector(1536);

SELECT extname, extversion FROM pg_extension WHERE extname = 'vector';

ALTER EXTENSION vector UPDATE;

select document_name, document_id, chunk_text from document_chunks limit 1000;


drop table document_chunks;

drop INDEX IF EXISTS idx_documents_embedding;

drop INDEX IF EXISTS idx_document_chunks_chunk_text;

drop INDEX IF EXISTS idx_document_id;

drop INDEX IF EXISTS idx_document_embedding;

drop INDEX IF EXISTS vector;

drop EXTENSION IF EXISTS vector;
drop EXTENSION IF EXISTS pgcrypto;
