# import os
# import threading
# import gradio as gr
# import boto3
# from dotenv import load_dotenv
# from fastapi import FastAPI
# from pydantic import BaseModel
# from openai import OpenAI
# from botocore.exceptions import NoCredentialsError, PartialCredentialsError


# class ChatApp:
#     def __init__(self):
#         # -------------------- Load Environment --------------------
#         load_dotenv()
#         self.aws_access_key = os.getenv("AWS_ACCESS_KEY_ID")
#         self.aws_secret_key = os.getenv("AWS_SECRET_ACCESS_KEY")
#         self.aws_region = os.getenv("AWS_REGION", "us-east-1")

#         self.knowledge_base_id = os.getenv("KNOWLEDGE_BASE_ID")
#         self.model_arn = os.getenv("MODEL_ARN")
#         self.openai_key = os.getenv("OPEN_CHAT_API_KEY")

#         # -------------------- OpenAI Client --------------------
#         self.openai_client = OpenAI(api_key=self.openai_key)

#         # -------------------- Boto3 Session --------------------
#         try:
#             self.boto_session = boto3.Session(
#                 aws_access_key_id=self.aws_access_key,
#                 aws_secret_access_key=self.aws_secret_key,
#                 region_name=self.aws_region
#             )

#             sts = self.boto_session.client("sts")
#             identity = sts.get_caller_identity()
#             print(f"Connected to AWS Account ID: {identity['Account']}")

#         except (NoCredentialsError, PartialCredentialsError) as e:
#             raise RuntimeError(f"AWS credentials not found or incomplete: {e}")

#         self.bedrock_runtime = self.boto_session.client("bedrock-agent-runtime")

#         # -------------------- FastAPI App --------------------
#         self.app = FastAPI(title="OpenAI + Bedrock Chat API")

#         # Register routes
#         self._register_routes()

#         # -------------------- Gradio UI --------------------
#         self.demo = gr.Interface(
#             fn=self.ask_openai,
#             inputs=gr.Textbox(label="Ask me anything"),
#             outputs=gr.Textbox(label="Response"),
#             title="🧠 Simple OpenAI Chatbot",
#         )

#     # -------------------- Data Model --------------------
#     class Query(BaseModel):
#         prompt: str

#     # -------------------- Register Routes --------------------
#     def _register_routes(self):
#         @self.app.get("/")
#         async def root():
#             return {"message": "OpenAI Chat API is running!"}

#         @self.app.post("/chat")
#         async def chat(query: self.Query):
#             return await self.chat_endpoint(query)

#         @self.app.on_event("startup")
#         async def startup_event():
#             threading.Thread(
#                 target=lambda: self.demo.launch(
#                     server_name="0.0.0.0", server_port=7860, show_error=True
#                 )
#             ).start()

#     # -------------------- Chat Endpoint --------------------
#     async def chat_endpoint(self, query: Query):
#         try:
#             response = self.openai_client.chat.completions.create(
#                 model="gpt-4o-mini",
#                 messages=[
#                     {"role": "system", "content": "You are a helpful assistant."},
#                     {"role": "user", "content": query.prompt},
#                 ],
#             )
#             answer = response.choices[0].message.content
#             print(answer)
#             return {"response": answer}
#         except Exception as e:
#             return {"error": str(e)}

#     # -------------------- Bedrock Interaction --------------------
#     async def ask_bedrock(self, user_input: str):
#         try:
#             response = self.bedrock_runtime.retrieve_and_generate(
#                 input={"text": user_input},
#                 retrieveAndGenerateConfiguration={
#                     "type": "KNOWLEDGE_BASE",
#                     "knowledgeBaseConfiguration": {
#                         "knowledgeBaseId": self.knowledge_base_id,
#                         "modelArn": self.model_arn,
#                     },
#                 },
#             )
#             return response
#         except Exception as e:
#             return f"Error: {e}"

#     # -------------------- Combined Bedrock + OpenAI --------------------
#     async def ask_openai(self, user_input: str):
#         try:
#             print(f"🔍 Query: {user_input}")
#             bedrock_resp = await self.ask_bedrock(user_input)
#             print(f"📚 Bedrock Response: {bedrock_resp}")
            
#             prompt = f"""
#             You are an assistant. Use **only** the following knowledge base info to answer the user query. 
#             Do not use any other data, information, or assumptions outside this context.

#             Knowledge Base Context:
#             {bedrock_resp}

#             User Query:
#             {user_input}

#             Answer concisely and clearly based strictly on the above knowledge base context. 
#             If the answer is not contained in the context, say "Sorry, I do not have enough information to answer that."
#             """


#             response = self.openai_client.chat.completions.create(
#                 model="gpt-4o-mini",
#                 messages=[
#                     {"role": "system", "content": "You are a helpful assistant."},
#                     {"role": "user", "content": prompt},
#                 ],
#                 temperature=0.7,
#                 max_tokens=300,
#             )

#             return response.choices[0].message.content
#         except Exception as e:
#             return f"Error: {e}"


# # -------------------- Run App --------------------
# chat_app = ChatApp()
# app = chat_app.app

import os
import sys
import threading
import gradio as gr
import boto3
from dotenv import load_dotenv
from fastapi import FastAPI
from pydantic import BaseModel
from openai import OpenAI
from botocore.exceptions import NoCredentialsError, PartialCredentialsError
from pathlib import Path
import tempfile
import uvicorn
import json
import time
from datetime import datetime

# Import functions from main_handler for document management
sys.path.append(os.path.dirname(os.path.abspath(__file__)))
from lambda_codes.main_handler import get_document_status, get_documents_by_status, get_document_chunks


class ChatApp:
    """
    Bedrock Knowledge Base POC - Gradio UI Application

    ARCHITECTURE:
    =============
    1. Document Ingestion:
       - User uploads document via UI
       - Document uploaded to S3
       - Lambda (main_handler.py) automatically triggered by S3 event
       - Lambda chunks document, generates embeddings, stores in Aurora (document_chunks table)
       - Lambda triggers Bedrock Knowledge Base sync
       - Bedrock populates bedrock_kb_documents table (managed by Bedrock)

    2. Document Tracking:
       - Database (Aurora) stores: documents, document_chunks, metadata
       - UI polls database for status updates (via main_handler.py functions)
       - Real-time buffering shows: upload → processing → chunking → completion

    3. Querying (via Bedrock Knowledge Base):
       - User asks question in UI
       - Lambda queries Bedrock Knowledge Base (NOT direct database queries)
       - Bedrock retrieves from bedrock_kb_documents table (vector search)
       - Results filtered by metadata (tenant_id, user_id, document_ids)
       - OpenAI generates final answer from retrieved context

    DATA FLOW:
    ===========
    - Database: Document tracking, metadata, status (via main_handler.py)
    - Bedrock KB: Semantic search, vector retrieval (via Lambda)
    - OpenAI: Answer generation from retrieved context
    """
    def __init__(self):
        # -------------------- Load Environment --------------------
        load_dotenv()
        self.aws_access_key = os.getenv("AWS_ACCESS_KEY_ID")
        self.aws_secret_key = os.getenv("AWS_SECRET_ACCESS_KEY")
        self.aws_region = os.getenv("AWS_REGION", "us-east-1")
        self.s3_bucket = os.getenv("S3_BUCKET")  # ✅ Add this to .env
        self.s3_upload_prefix = os.getenv("S3_UPLOAD_PREFIX", "bedrock-poc-docs/")
        self.lambda_function = os.getenv("LAMBDA_FUNCTION", "poc-s3-handler")

        self.knowledge_base_id = os.getenv("KNOWLEDGE_BASE_ID")
        self.model_arn = os.getenv("MODEL_ARN")
        self.openai_key = os.getenv("OPEN_CHAT_API_KEY")

        # Database configuration
        self.db_host = os.getenv("DB_HOST")
        self.db_port = int(os.getenv("DB_PORT", 5432))
        self.db_name = os.getenv("DB_NAME")
        self.db_secret_arn = os.getenv("DB_SECRET_ARN")

        # -------------------- OpenAI Client --------------------
        self.openai_client = OpenAI(api_key=self.openai_key)

        # -------------------- Boto3 Session --------------------
        try:
            self.boto_session = boto3.Session(
                aws_access_key_id=self.aws_access_key,
                aws_secret_access_key=self.aws_secret_key,
                region_name=self.aws_region
            )

            sts = self.boto_session.client("sts")
            identity = sts.get_caller_identity()
            print(f"Connected to AWS Account ID: {identity['Account']}")

        except (NoCredentialsError, PartialCredentialsError) as e:
            raise RuntimeError(f"AWS credentials not found or incomplete: {e}")

        self.bedrock_runtime = self.boto_session.client("bedrock-agent-runtime")
        self.bedrock_agent = self.boto_session.client("bedrock-agent")
        self.s3_client = self.boto_session.client("s3")  # ✅ S3 Client
        self.lambda_client = self.boto_session.client("lambda")
        self.secrets_client = self.boto_session.client("secretsmanager")

        # -------------------- FastAPI App --------------------
        self.app = FastAPI(title="OpenAI + Bedrock Chat API")

        # Register routes
        self._register_routes()

        # -------------------- Gradio UI --------------------
        with gr.Blocks(title="Bedrock Knowledge Base POC") as self.demo:
            gr.Markdown("# 📚 Bedrock Knowledge Base POC")
            gr.Markdown("Upload documents, monitor ingestion, and query your knowledge base with real-time feedback")

            with gr.Tabs():
                # Tab 1: Document Upload with Buffering
                with gr.TabItem("📤 Upload & Monitor"):
                    gr.Markdown("### Upload Document with Real-Time Processing Monitor")

                    with gr.Row():
                        with gr.Column(scale=1):
                            self.file_input = gr.File(
                                label="Upload Document",
                                file_types=[".pdf", ".txt", ".docx"]
                            )
                            self.tenant_input = gr.Textbox(
                                label="Tenant ID",
                                value="tenant-demo"
                            )
                            self.user_input = gr.Textbox(
                                label="User ID",
                                value="user-demo"
                            )
                            self.upload_button = gr.Button("🚀 Upload & Process", variant="primary")

                        with gr.Column(scale=2):
                            self.upload_progress = gr.Textbox(
                                label="📊 Progress Status",
                                interactive=False,
                                lines=2
                            )
                            self.processing_log = gr.Textbox(
                                label="📝 Processing Log (Real-Time Buffering)",
                                interactive=False,
                                lines=15,
                                max_lines=20
                            )

                    gr.Markdown("### Document Details")
                    with gr.Row():
                        self.doc_id_output = gr.Textbox(label="Document ID", interactive=False)
                        self.chunk_count_output = gr.Textbox(label="Chunks Stored", interactive=False)
                        self.status_output = gr.Textbox(label="Status", interactive=False)

                    gr.Markdown("### Sample Chunks Preview")
                    self.chunks_output = gr.Textbox(
                        label="First 5 Chunks",
                        interactive=False,
                        lines=10
                    )

                    self.upload_button.click(
                        fn=self.upload_with_monitoring,
                        inputs=[self.file_input, self.tenant_input, self.user_input],
                        outputs=[
                            self.upload_progress,
                            self.processing_log,
                            self.doc_id_output,
                            self.chunk_count_output,
                            self.status_output,
                            self.chunks_output
                        ]
                    )

                # Tab 2: Query Interface
                with gr.TabItem("🔍 Query Knowledge Base"):
                    gr.Markdown("### Ask Questions from Your Knowledge Base")

                    self.question_input = gr.Textbox(
                        label="Enter your question",
                        lines=3,
                        placeholder="What information would you like to retrieve?"
                    )

                    with gr.Row():
                        self.filter_tenant = gr.Textbox(label="Filter by Tenant ID (optional)")
                        self.filter_user = gr.Textbox(label="Filter by User ID (optional)")
                        self.filter_docs = gr.Textbox(
                            label="Filter by Document IDs (comma-separated, optional)",
                            placeholder="doc-id-1, doc-id-2"
                        )

                    self.ask_button = gr.Button("💬 Ask Question", variant="primary")
                    self.response_output = gr.Textbox(
                        label="🤖 Answer from RAG",
                        lines=10,
                        interactive=False
                    )

                    self.retrieval_details = gr.JSON(label="Retrieved Context Details")

                    self.ask_button.click(
                        fn=self.ask_with_filters,
                        inputs=[
                            self.question_input,
                            self.filter_tenant,
                            self.filter_user,
                            self.filter_docs
                        ],
                        outputs=[self.response_output, self.retrieval_details]
                    )

                # Tab 3: Document Status
                with gr.TabItem("📊 Document Status"):
                    gr.Markdown("### Check Document Processing Status")

                    self.status_s3_key = gr.Textbox(
                        label="Enter S3 Key",
                        placeholder="bedrock-poc-docs/document.pdf"
                    )
                    self.check_status_button = gr.Button("🔍 Check Status", variant="secondary")

                    self.status_details = gr.JSON(label="Document Status Details")
                    self.status_chunks = gr.Textbox(
                        label="Chunks",
                        interactive=False,
                        lines=10
                    )

                    self.check_status_button.click(
                        fn=self.check_document_status,
                        inputs=self.status_s3_key,
                        outputs=[self.status_details, self.status_chunks]
                    )

    # -------------------- Upload with Real-Time Monitoring --------------------
    def upload_with_monitoring(self, file_obj, tenant_id, user_id):
        """
        Upload document to S3 and monitor processing with step-by-step buffering
        Returns: progress, log, doc_id, chunk_count, status, chunks_preview
        """
        log_buffer = []

        def log(message):
            timestamp = datetime.now().strftime("%H:%M:%S")
            log_buffer.append(f"[{timestamp}] {message}")
            return "\n".join(log_buffer)

        try:
            if not file_obj:
                return "❌ Error", log("No file uploaded"), "", "", "", ""

            # Step 1: Upload to S3
            yield (
                "⬆️ Step 1/5: Uploading to S3...",
                log("Starting upload..."),
                "", "", "", ""
            )

            s3_key = self.s3_upload_prefix + os.path.basename(file_obj.name)

            self.s3_client.upload_file(
                Filename=file_obj.name,
                Bucket=self.s3_bucket,
                Key=s3_key
            )

            yield (
                "✅ Step 1/5: Uploaded to S3",
                log(f"✅ Uploaded to s3://{self.s3_bucket}/{s3_key}"),
                "", "", "", ""
            )

            time.sleep(1)

            # Step 2: Wait for Lambda trigger
            yield (
                "⏳ Step 2/5: Waiting for Lambda to process...",
                log("Lambda function triggered automatically by S3 event..."),
                "", "", "", ""
            )

            time.sleep(3)

            # Step 3: Poll for document in database
            yield (
                "🔍 Step 3/5: Checking database for document record...",
                log("Polling database for document status..."),
                "", "", "", ""
            )

            max_polls = 60
            poll_count = 0
            document_status = None

            while poll_count < max_polls:
                time.sleep(2)
                poll_count += 1

                document_status = get_document_status(s3_key=s3_key)

                if document_status:
                    status = document_status['status']
                    chunk_count = document_status.get('chunk_count', 0)
                    doc_id = document_status['document_id']

                    yield (
                        f"🔄 Step 4/5: Processing... ({status})",
                        log(f"Status: {status} | Chunks stored: {chunk_count}"),
                        doc_id,
                        str(chunk_count),
                        status,
                        ""
                    )

                    if status == 'completed':
                        yield (
                            "✅ Step 5/5: Processing Complete!",
                            log(f"✅ Processing complete! Total chunks: {chunk_count}"),
                            doc_id,
                            str(chunk_count),
                            status,
                            ""
                        )

                        # Fetch and display sample chunks
                        chunks = get_document_chunks(doc_id)

                        chunks_preview = "\n\n".join([
                            f"--- Chunk {c['chunk_index']} ({c['status']}) ---\n{c['chunk_text'][:300]}..."
                            for c in chunks[:5]
                        ])

                        yield (
                            "✅ Complete!",
                            log(f"Retrieved {len(chunks)} chunks from database"),
                            doc_id,
                            str(chunk_count),
                            status,
                            chunks_preview
                        )

                        break

                    elif status == 'failed':
                        error_msg = document_status.get('error_message', 'Unknown error')
                        yield (
                            "❌ Step 5/5: Processing Failed",
                            log(f"❌ Failed: {error_msg}"),
                            doc_id,
                            str(chunk_count),
                            status,
                            ""
                        )
                        break

                else:
                    yield (
                        f"⏳ Step 3/5: Waiting for document record... (attempt {poll_count}/{max_polls})",
                        log(f"Document not yet visible in database (attempt {poll_count})"),
                        "", "", "", ""
                    )

            if poll_count >= max_polls:
                yield (
                    "⚠️ Timeout",
                    log("⚠️ Polling timeout. Check document status tab for updates."),
                    "", "", "", ""
                )

        except Exception as e:
            yield (
                "❌ Error",
                log(f"❌ Error: {str(e)}"),
                "", "", "", ""
            )

    # -------------------- Data Model --------------------
    class Query(BaseModel):
        prompt: str

    # -------------------- Register Routes --------------------
    def _register_routes(self):
        @self.app.get("/")
        async def root():
            return {"message": "OpenAI Chat API is running!"}

        @self.app.post("/chat")
        async def chat(query: self.Query):
            return await self.chat_endpoint(query)

        @self.app.on_event("startup")
        async def startup_event():
            threading.Thread(
                target=lambda: self.demo.launch(
                    server_name="0.0.0.0", server_port=7860, show_error=True
                )
            ).start()

    # -------------------- Chat Endpoint --------------------
    async def chat_endpoint(self, query: Query):
        try:
            response = self.openai_client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[
                    {"role": "system", "content": "You are a helpful assistant."},
                    {"role": "user", "content": query.prompt},
                ],
            )
            answer = response.choices[0].message.content
            print(answer)
            return {"response": answer}
        except Exception as e:
            return {"error": str(e)}

    # -------------------- Bedrock Interaction --------------------
    async def ask_bedrock(self, user_input: str):
        try:
            response = self.bedrock_runtime.retrieve_and_generate(
                input={"text": user_input},
                retrieveAndGenerateConfiguration={
                    "type": "KNOWLEDGE_BASE",
                    "knowledgeBaseConfiguration": {
                        "knowledgeBaseId": self.knowledge_base_id,
                        "modelArn": self.model_arn,
                    },
                },
            )
            return response
        except Exception as e:
            return f"Error: {e}"

    # -------------------- Query Knowledge Base (via Bedrock) --------------------
    def ask_with_filters(self, user_input, filter_tenant, filter_user, filter_docs):
        """
        Query Bedrock Knowledge Base with metadata filtering.
        This uses the KNOWLEDGE BASE for retrieval (not direct database queries).
        Database is only used for document tracking/metadata.
        """
        try:
            if not user_input.strip():
                return "Please enter a question", {}

            print(f"🔍 Querying Bedrock Knowledge Base: {user_input}")

            # Build filters for Lambda
            filters = {}
            if filter_tenant:
                filters['tenant_id'] = filter_tenant
            if filter_user:
                filters['user_id'] = filter_user
            if filter_docs:
                doc_ids = [d.strip() for d in filter_docs.split(',')]
                filters['document_ids'] = doc_ids

            # Call Lambda which queries Bedrock Knowledge Base (retrieve_from_knowledge_base function)
            # This retrieves from bedrock_kb_documents table (managed by Bedrock)
            payload = {
                "action": "query",
                "query_text": user_input,
                "filters": filters,
                "top_k": 5
            }

            response = self.lambda_client.invoke(
                FunctionName=self.lambda_function,
                InvocationType='RequestResponse',
                Payload=json.dumps(payload)
            )

            result = json.loads(response['Payload'].read())

            if result.get('statusCode') == 200:
                body = json.loads(result['body'])
                retrieval_results = body.get('results', [])

                print(f"📚 Retrieved {len(retrieval_results)} results from Bedrock KB")

                # Format context from Bedrock Knowledge Base retrieval
                context = "\n\n".join([
                    f"[Document: {r.get('document_id', 'N/A')} | Score: {r['score']:.3f}]\n{r['content']}"
                    for r in retrieval_results[:3]
                ])

                if not context:
                    return "No relevant information found in the knowledge base.", {
                        "message": "No documents matched your query and filters.",
                        "filters_applied": filters
                    }

                # Generate answer using OpenAI with Bedrock KB context
                prompt = f"""
                You are a helpful AI assistant. Answer the user's question using ONLY the information
                provided from the knowledge base context below. Do not use any external knowledge.

                Knowledge Base Context (Retrieved from Bedrock):
                {context}

                User Question:
                {user_input}

                Instructions:
                - Provide a clear, concise answer based strictly on the context above
                - If the context doesn't contain enough information, say "I don't have enough information to answer that question."
                - Cite which document the information came from when possible
                """

                openai_response = self.openai_client.chat.completions.create(
                    model="gpt-4o-mini",
                    messages=[
                        {"role": "system", "content": "You are a helpful assistant that answers questions based on provided context."},
                        {"role": "user", "content": prompt},
                    ],
                    temperature=0.7,
                    max_tokens=500,
                )

                answer = openai_response.choices[0].message.content

                # Format retrieval details for display
                retrieval_details = {
                    "source": "Bedrock Knowledge Base",
                    "total_results": len(retrieval_results),
                    "filters_applied": filters if filters else "No filters",
                    "top_results": [
                        {
                            "rank": r['rank'],
                            "similarity_score": f"{r['score']:.4f}",
                            "document_id": r.get('document_id', 'N/A'),
                            "chunk_index": r.get('chunk_index', 'N/A'),
                            "content_preview": r['content'][:200] + "..."
                        }
                        for r in retrieval_results[:3]
                    ]
                }

                return answer, retrieval_details

            else:
                return f"Error querying knowledge base: {result}", {}

        except Exception as e:
            print(f"Error in ask_with_filters: {e}")
            return f"Error: {e}", {}

    # -------------------- Check Document Status --------------------
    def check_document_status(self, s3_key):
        """
        Check status of a specific document using main_handler functions.
        This queries the database for document TRACKING info (not for answering questions).
        """
        try:
            if not s3_key.strip():
                return {}, "Please enter an S3 key"

            # Use imported function from main_handler
            status = get_document_status(s3_key=s3_key)

            if not status:
                return {"error": "Document not found in database"}, ""

            # Get chunks using imported function
            chunks = get_document_chunks(status['document_id'])

            chunks_preview = "\n\n".join([
                f"--- Chunk {c['chunk_index']} ({c['status']}) ---\n{c['chunk_text'][:500]}"
                for c in chunks[:5]
            ])

            if len(chunks) > 5:
                chunks_preview += f"\n\n... and {len(chunks) - 5} more chunks"

            status_display = {
                "Document ID": status['document_id'],
                "Document Name": status['document_name'],
                "S3 Key": status['s3_key'],
                "Status": status['status'],
                "Chunk Count": status['chunk_count'],
                "Tenant ID": status['tenant_id'],
                "User ID": status['user_id'],
                "Project ID": status.get('project_id', 'N/A'),
                "Thread ID": status.get('thread_id', 'N/A'),
                "Ingestion Job ID": status.get('ingestion_job_id', 'N/A'),
                "Created At": str(status.get('created_at', 'N/A')),
                "Updated At": str(status.get('updated_at', 'N/A')),
                "Error Message": status.get('error_message') or "None"
            }

            return status_display, chunks_preview

        except Exception as e:
            print(f"Error checking document status: {e}")
            return {"error": str(e)}, ""

    # -------------------- Combined Bedrock + OpenAI --------------------
    async def ask_openai(self, user_input: str):
        try:
            print(f"🔍 Query: {user_input}")
            bedrock_resp = await self.ask_bedrock(user_input)
            print(f"📚 Bedrock Response: {bedrock_resp}")

            prompt = f"""
            You are an assistant. Use **only** the following knowledge base info to answer the user query.
            Do not use any other data, information, or assumptions outside this context.

            Knowledge Base Context:
            {bedrock_resp}

            User Query:
            {user_input}

            Answer concisely and clearly based strictly on the above knowledge base context.
            If the answer is not contained in the context, say "Sorry, I do not have enough information to answer that."
            """

            response = self.openai_client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[
                    {"role": "system", "content": "You are a helpful assistant."},
                    {"role": "user", "content": prompt},
                ],
                temperature=0.7,
                max_tokens=300,
            )

            return response.choices[0].message.content
        except Exception as e:
            return f"Error: {e}"


# -------------------- Run App --------------------
if __name__ == "__main__":
    # Run the FastAPI server
    chat_app = ChatApp()
    app = chat_app.app
    uvicorn.run(app, host="0.0.0.0", port=8000)

