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


SET session_replication_role = 'replica';
DELETE FROM document_chunks;
DELETE FROM documents;
SET session_replication_role = 'origin';

drop table document_chunks;

drop INDEX IF EXISTS idx_documents_embedding;

drop INDEX IF EXISTS idx_document_chunks_chunk_text;

drop INDEX IF EXISTS idx_document_id;

drop INDEX IF EXISTS idx_document_embedding;

drop INDEX IF EXISTS vector;

drop EXTENSION IF EXISTS vector;
drop EXTENSION IF EXISTS pgcrypto;



-- ==============================================
-- 1️⃣ Enable UUID generation
-- ==============================================
CREATE EXTENSION IF NOT EXISTS pgcrypto;

-- ==============================================
-- 2️⃣ Add new UUID columns
-- ==============================================
ALTER TABLE documents
ADD COLUMN document_id_new uuid DEFAULT gen_random_uuid();

ALTER TABLE document_chunks
ADD COLUMN document_id_new uuid;

-- ==============================================
-- 3️⃣ Populate child table UUIDs
-- ==============================================
UPDATE document_chunks dc
SET document_id_new = d.document_id_new
FROM documents d
WHERE dc.document_id = d.document_id;

-- ==============================================
-- 4️⃣ Drop foreign key on child table (if exists)
-- ==============================================
ALTER TABLE document_chunks
DROP CONSTRAINT IF EXISTS document_chunks_document_id_fkey;

-- ==============================================
-- 5️⃣ Drop old text ID columns
-- ==============================================
ALTER TABLE document_chunks DROP COLUMN document_id;
ALTER TABLE documents DROP COLUMN document_id;

-- ==============================================
-- 6️⃣ Rename new UUID columns to original names
-- ==============================================
ALTER TABLE documents RENAME COLUMN document_id_new TO document_id;
ALTER TABLE document_chunks RENAME COLUMN document_id_new TO document_id;

-- ==============================================
-- 7️⃣ Add PRIMARY KEY on documents
-- ==============================================
ALTER TABLE documents
ADD CONSTRAINT documents_pkey PRIMARY KEY (document_id);

-- ==============================================
-- 8️⃣ Add FOREIGN KEY on document_chunks
-- ==============================================
ALTER TABLE document_chunks
ADD CONSTRAINT document_chunks_document_id_fkey
FOREIGN KEY (document_id) REFERENCES documents(document_id);

-- ==============================================
-- ✅ Done
-- Now your tables are UUID-compliant for Bedrock ingestion
-- ==============================================



-- =========================================
-- 1️⃣ Extensions
-- =========================================
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pgcrypto;

-- =========================================
-- 2️⃣ Documents Table
-- =========================================
CREATE TABLE IF NOT EXISTS documents (
    document_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    project_id TEXT,
    document_name TEXT NOT NULL,
    thread_id TEXT,
    status TEXT NOT NULL DEFAULT 'not-started',
    created_at TIMESTAMP DEFAULT NOW(),
    updated_at TIMESTAMP DEFAULT NOW()
);

-- =========================================
-- 3️⃣ Document Chunks Table
-- =========================================
CREATE TABLE IF NOT EXISTS document_chunks (
    chunk_id SERIAL PRIMARY KEY,
    document_id UUID NOT NULL REFERENCES documents(document_id) ON DELETE CASCADE,
    chunk_index INT NOT NULL,
    chunk_text TEXT NOT NULL,
    document_name TEXT NOT NULL,
    embedding_vector VECTOR(1536) NOT NULL,
    metadata JSONB,
    status TEXT NOT NULL DEFAULT 'not-started',
    created_at TIMESTAMP DEFAULT NOW()
);

-- =========================================
-- 4️⃣ Unique Constraints (for ON CONFLICT)
-- =========================================
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.table_constraints
        WHERE table_name = 'document_chunks'
        AND constraint_name = 'document_chunks_unique_doc_idx'
    ) THEN
        ALTER TABLE document_chunks
        ADD CONSTRAINT document_chunks_unique_doc_idx UNIQUE (document_id, chunk_index);
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM information_schema.table_constraints
        WHERE table_name = 'document_chunks'
        AND constraint_name = 'document_chunks_unique_doc_id'
    ) THEN
        ALTER TABLE document_chunks
        ADD CONSTRAINT document_chunks_unique_doc_id UNIQUE (document_id, chunk_id);
    END IF;
END $$;

-- =========================================
-- 5️⃣ Indices
-- =========================================
CREATE INDEX IF NOT EXISTS idx_documents_tenant
    ON documents(tenant_id);

CREATE INDEX IF NOT EXISTS idx_document_chunks_document_id
    ON document_chunks(document_id);

CREATE INDEX IF NOT EXISTS idx_document_chunks_textsearch
    ON document_chunks USING gin (to_tsvector('simple', chunk_text));

CREATE INDEX IF NOT EXISTS idx_document_chunks_vector_l2
    ON document_chunks USING ivfflat (embedding_vector vector_l2_ops) WITH (lists = 100);

CREATE INDEX IF NOT EXISTS idx_document_chunks_vector_cosine
    ON document_chunks USING hnsw (embedding_vector vector_cosine_ops);

-- =========================================
-- 6️⃣ Insert Sample Document (for validation)
-- =========================================
INSERT INTO documents (document_id, tenant_id, user_id, project_id, document_name, status)
VALUES ('00000000-0000-0000-0000-000000000001', 'tenant1', 'user1', 'project1', 'bedrock-poc-docs/test.txt', 'active')
ON CONFLICT (document_id) DO NOTHING;

-- =========================================
-- 7️⃣ Insert Sample Chunk (for validation)
-- =========================================
INSERT INTO document_chunks (
    document_id,
    chunk_index,
    chunk_text,
    document_name,
    embedding_vector
)
VALUES (
    '00000000-0000-0000-0000-000000000001',
    0,
    'test chunk',
    'bedrockpoc-docs/test.txt',
    (SELECT ('[' || string_agg('0', ',') || ']')::vector
     FROM generate_series(1, 1536))
)
ON CONFLICT (document_id, chunk_index) DO UPDATE
SET chunk_text = EXCLUDED.chunk_text;

-- =========================================
-- 8️⃣ Verification
-- =========================================
SELECT conname, pg_get_constraintdef(oid)
FROM pg_constraint
WHERE conrelid = 'document_chunks'::regclass
AND contype IN ('u', 'p');

SELECT COUNT(*) AS documents_count FROM documents;
SELECT COUNT(*) AS chunks_count FROM document_chunks;
