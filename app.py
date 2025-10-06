import os
from fastapi import FastAPI
from pydantic import BaseModel
from dotenv import load_dotenv
from openai import OpenAI
import gradio as gr

client = OpenAI(api_key="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")


# -------------------- FastAPI Setup --------------------
app = FastAPI(title="OpenAI Chat API")

# -------------------- Data Model --------------------
class Query(BaseModel):
    prompt: str

# -------------------- Endpoint --------------------
@app.post("/chat")
async def chat(query: Query):
    """
    Simple POST endpoint to get LLM response for a user query.
    """
    try:
        response = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
        {"role": "system", "content": "You are a helpful assistant."},
        {"role": "user", "content": "Hello!"}
        ])
        print(response.choices[0].message.content)
        answer = response.choices[0].message.content
        return {"response": answer}
    except Exception as e:
        return {"error": str(e)}

# -------------------- Gradio Interface --------------------
def ask_openai(user_input: str) -> str:
    try:
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": user_input}],
            max_tokens=200
        )
        return response.choices[0].message.content
    except Exception as e:
        return f"Error: {e}"

demo = gr.Interface(
    fn=ask_openai,
    inputs=gr.Textbox(label="Ask me anything"),
    outputs=gr.Textbox(label="Response"),
    title="🧠 Simple OpenAI Chatbot",
)

# -------------------- Mount Gradio UI --------------------
@app.get("/")
def read_root():
    return {"message": "OpenAI Chat API is running!"}

@app.on_event("startup")
async def startup_event():
    # Launch Gradio app in background thread when FastAPI starts
    import threading
    threading.Thread(target=lambda: demo.launch(server_name="0.0.0.0", server_port=7860, show_error=True)).start()

