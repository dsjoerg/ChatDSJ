from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
import os
import threading
from dotenv import load_dotenv
from app.slack.app import create_slack_app, IS_DUMMY_APP
from slack_bolt.adapter.socket_mode import SocketModeHandler


# Load environment variables
load_dotenv()

slack_app = create_slack_app()
app = FastAPI()

# Disable CORS. Do not remove this for full-stack development.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Allows all origins
    allow_credentials=True,
    allow_methods=["*"],  # Allows all methods
    allow_headers=["*"],  # Allows all headers
)

# Start the Slack bot in a separate thread when FastAPI starts
@app.on_event("startup")
async def startup_event():
    slack_app_token = os.environ.get("SLACK_APP_TOKEN")
    if IS_DUMMY_APP or not slack_app_token:
        print("Slack bot is in dummy mode or SLACK_APP_TOKEN missing — skipping Socket Mode.")
        return

    def start_slack():
        from slack_bolt.adapter.socket_mode import SocketModeHandler
        print(f"Starting Slack SocketModeHandler...")
        handler = SocketModeHandler(slack_app, slack_app_token)
        handler.start()

    threading.Thread(target=start_slack, daemon=True).start()
    print("Slack bot thread launched.")

@app.get("/healthz")
async def healthz():
    return {"status": "ok"}

@app.get("/test-chatgpt")
async def test_chatgpt():
    from app.slack.app import get_openai_response
    test_prompt = "Can you help me with my Python code?"
    response_text, usage = get_openai_response([], test_prompt) # Pass empty history for simplicity
    response = {"response": response_text, "usage": usage}
    return {"response": response}

@app.get("/test-openai")
async def test_openai():
    """Test endpoint to diagnose OpenAI API issues"""
    from app.slack.app import openai_client, logger
    
    if not openai_client:
        return {"status": "error", "message": "OpenAI client not initialized"}
    
    try:
        response = openai_client.chat.completions.create(
            model="gpt-4o",
            messages=[
                {"role": "system", "content": "You are a helpful assistant."},
                {"role": "user", "content": "Say hello world"}
            ],
            max_tokens=20
        )
        return {
            "status": "success", 
            "response": response.choices[0].message.content,
            "model": response.model,
            "usage": response.usage.model_dump() if hasattr(response, "usage") else None
        }
    except Exception as e:
        error_message = str(e)
        logger.error(f"OpenAI API test error: {error_message}")
        return {"status": "error", "message": error_message}
