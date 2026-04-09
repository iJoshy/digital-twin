from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import os
from dotenv import load_dotenv
from typing import Optional, List, Dict, Any, Callable
import json
import uuid
from datetime import datetime
import re
import boto3
from botocore.exceptions import ClientError
from context import prompt
import sendgrid
from sendgrid.helpers.mail import Mail, Email, To, Content, Bc
import urllib.request, urllib.parse, urllib.error

# Load environment variables
load_dotenv()

app = FastAPI()

# Configure CORS
origins = os.getenv("CORS_ORIGINS", "http://localhost:3000").split(",")
app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)

# Initialize Bedrock client - see Q42 on https://edwarddonner.com/faq if the Region gives you problems
bedrock_client = boto3.client(
    service_name="bedrock-runtime", 
    region_name=os.getenv("DEFAULT_AWS_REGION", "eu-west-1")
)

# Bedrock model selection - see Q42 on https://edwarddonner.com/faq for more
BEDROCK_MODEL_ID = os.getenv("BEDROCK_MODEL_ID", "global.amazon.nova-2-lite-v1:0")

# Memory storage configuration
USE_S3 = os.getenv("USE_S3", "false").lower() == "true"
S3_BUCKET = os.getenv("S3_BUCKET", "")
MEMORY_DIR = os.getenv("MEMORY_DIR", "../memory")

# Initialize S3 client if needed
if USE_S3:
    s3_client = boto3.client("s3")


# Request/Response models
class ChatRequest(BaseModel):
    message: str
    session_id: Optional[str] = None


class ChatResponse(BaseModel):
    response: str
    session_id: str


class Message(BaseModel):
    role: str
    content: str
    timestamp: str


# Email & Push Tool creation

def send_email(body: str, recipient_email: str) -> Dict[str, Any]:
    """Send out an email with the given body."""
    emailkey = os.getenv("SENDGRID_API_KEY")
    emailfrom = os.getenv("SENDGRID_SENDER_EMAIL")
    emailto = os.getenv("RECIPIENT_EMAIL")
    if not emailkey or not emailfrom or not emailto:
        print("Sendgrid skipped: SENDGRID_API_KEY or SENDGRID_SENDER_EMAIL is missing")
        return {"status": "skipped", "reason": "missing sendgrid env vars"}

    try:
        sg = sendgrid.SendGridAPIClient(api_key=emailkey)
        from_email = Email(emailfrom)
        to_email = To(recipient_email)
        content = Content("text/html", body)
        mail = Mail(from_email, to_email, "Enquiry on Joshua Balogun's Digital Twin", content)
        mail.add_bc(Bc(emailto))
        sg.client.mail.send.post(request_body=mail.get())
        return {"status": "success", "recipient_email": recipient_email}
    except urllib.error.HTTPError as e:
        error_body = ""
        try:
            error_body = e.read().decode("utf-8", errors="ignore")
        except Exception:
            error_body = ""
        reason = f"{e}"
        if error_body:
            reason = f"{reason} | {error_body}"
        print(f"Sendgrid error: {reason}")
        return {"status": "error", "reason": reason}
    except Exception as e:
        print(f"Sendgrid error: {e}")
        return {"status": "error", "reason": str(e)}


send_email_json = {
    "name": "send_email",
    "description": "Use this tool to send an HTML email to the user.",
    "parameters": {
        "type": "object",
        "properties": {
            "body": {
                "type": "string",
                "description": "The HTML body of the email."
            },
            "recipient_email": {
                "type": "string",
                "description": "The recipient email address."
            },
        },
        "required": ["body", "recipient_email"],
        "additionalProperties": False,
    },
}


def push(text: str) -> Dict[str, Any]:
    token = os.getenv("PUSHOVER_TOKEN")
    user = os.getenv("PUSHOVER_USER")

    if not token or not user:
        print("Pushover skipped: PUSHOVER_TOKEN or PUSHOVER_USER is missing")
        return {"status": "skipped", "reason": "missing pushover env vars"}

    payload = urllib.parse.urlencode(
        {
            "token": token,
            "user": user,
            "message": text,
        }
    ).encode("utf-8")

    req = urllib.request.Request(
        "https://api.pushover.net/1/messages.json",
        data=payload,
        method="POST",
    )
    req.add_header("Content-Type", "application/x-www-form-urlencoded")

    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            body = resp.read().decode("utf-8", errors="ignore")
            print(f"Pushover status={resp.status}, body={body}")
            return {"status": "success", "http_status": resp.status}
    except Exception as e:
        print(f"Pushover error: {e}")
        return {"status": "error", "reason": str(e)}


def record_user_details(email, name="Name not provided", notes="not provided"):
    push(f"Recording {name} with email {email} and notes {notes}")
    return {"recorded": "ok"}

def record_unknown_question(question):
    push(f"Recording {question}")
    return {"recorded": "ok"}


record_user_details_json = {
    "name": "record_user_details",
    "description": "Use this tool to record that a user is interested in being in touch and provided an email address",
    "parameters": {
        "type": "object",
        "properties": {
            "email": {
                "type": "string",
                "description": "The email address of this user"
            },
            "name": {
                "type": "string",
                "description": "The user's name, if they provided it"
            }
            ,
            "notes": {
                "type": "string",
                "description": "Any additional information about the conversation that's worth recording to give context"
            }
        },
        "required": ["email"],
        "additionalProperties": False
    }
}

record_unknown_question_json = {
    "name": "record_unknown_question",
    "description": "Always use this tool to record any question that couldn't be answered as you didn't know the answer",
    "parameters": {
        "type": "object",
        "properties": {
            "question": {
                "type": "string",
                "description": "The question that couldn't be answered"
            },
        },
        "required": ["question"],
        "additionalProperties": False
    }
}

tool_schemas = [
    record_user_details_json,
    record_unknown_question_json,
    send_email_json,
]

bedrock_tools = [
    {
        "toolSpec": {
            "name": schema["name"],
            "description": schema["description"],
            "inputSchema": {"json": schema["parameters"]},
        }
    }
    for schema in tool_schemas
]

tool_handlers: Dict[str, Callable[..., Dict[str, Any]]] = {
    "record_user_details": record_user_details,
    "record_unknown_question": record_unknown_question,
    "send_email": send_email,
}


# Memory management functions
def get_memory_path(session_id: str) -> str:
    return f"{session_id}.json"


def load_conversation(session_id: str) -> List[Dict]:
    """Load conversation history from storage"""
    if USE_S3:
        try:
            response = s3_client.get_object(Bucket=S3_BUCKET, Key=get_memory_path(session_id))
            return json.loads(response["Body"].read().decode("utf-8"))
        except ClientError as e:
            if e.response["Error"]["Code"] == "NoSuchKey":
                return []
            raise
    else:
        # Local file storage
        file_path = os.path.join(MEMORY_DIR, get_memory_path(session_id))
        if os.path.exists(file_path):
            with open(file_path, "r") as f:
                return json.load(f)
        return []


def save_conversation(session_id: str, messages: List[Dict]):
    """Save conversation history to storage"""
    if USE_S3:
        s3_client.put_object(
            Bucket=S3_BUCKET,
            Key=get_memory_path(session_id),
            Body=json.dumps(messages, indent=2),
            ContentType="application/json",
        )
    else:
        # Local file storage
        os.makedirs(MEMORY_DIR, exist_ok=True)
        file_path = os.path.join(MEMORY_DIR, get_memory_path(session_id))
        with open(file_path, "w") as f:
            json.dump(messages, f, indent=2)


def extract_text_from_content(content: List[Dict[str, Any]]) -> str:
    text_parts = [item["text"] for item in content if "text" in item]
    return "\n".join(part.strip() for part in text_parts if part and part.strip()).strip()


def strip_thinking_blocks(text: str) -> str:
    """Remove model reasoning blocks like <thinking>...</thinking> from visible output."""
    if not text:
        return ""

    cleaned = re.sub(r"<thinking>.*?</thinking>", "", text, flags=re.IGNORECASE | re.DOTALL)
    return cleaned.strip()


def sanitize_content_blocks(content: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    sanitized: List[Dict[str, Any]] = []
    for block in content:
        if "text" in block:
            text = block.get("text", "")
            if isinstance(text, str) and text.strip():
                sanitized.append({"text": text})
        elif "toolUse" in block:
            sanitized.append({"toolUse": block["toolUse"]})
        elif "toolResult" in block:
            sanitized.append({"toolResult": block["toolResult"]})
    return sanitized


def execute_tool(name: str, tool_input: Dict[str, Any]) -> Dict[str, Any]:
    handler = tool_handlers.get(name)
    if not handler:
        return {"status": "error", "reason": f"Unknown tool: {name}"}

    try:
        return handler(**tool_input)
    except Exception as e:
        return {"status": "error", "reason": str(e)}


def call_bedrock(conversation: List[Dict], user_message: str) -> str:
    """Call AWS Bedrock with conversation history"""
    
    # Build messages in Bedrock format
    messages = []
    
    # Add conversation history (limit to last 25 exchanges)
    for msg in conversation[-50:]:
        content_text = msg.get("content", "")
        if not isinstance(content_text, str) or not content_text.strip():
            continue
        messages.append({
            "role": msg["role"],
            "content": [{"text": content_text}]
        })
    
    # Add current user message
    messages.append({
        "role": "user",
        "content": [{"text": user_message}]
    })
    
    try:
        # Tool loop: keep calling Bedrock until we get a final assistant text response
        for _ in range(5):
            response = bedrock_client.converse(
                modelId=BEDROCK_MODEL_ID,
                system=[{"text": prompt()}],
                messages=messages,
                toolConfig={"tools": bedrock_tools},
                inferenceConfig={
                    "maxTokens": 2000,
                    "temperature": 0.7,
                    "topP": 0.9
                }
            )

            output_message = response["output"]["message"]
            stop_reason = response.get("stopReason", "")

            if stop_reason != "tool_use":
                final_text = extract_text_from_content(output_message.get("content", []))
                final_text = strip_thinking_blocks(final_text)
                return final_text or "I completed that action."

            sanitized_output_content = sanitize_content_blocks(output_message.get("content", []))
            if not sanitized_output_content:
                raise HTTPException(status_code=500, detail="Model returned empty tool-use content")
            messages.append({"role": output_message["role"], "content": sanitized_output_content})

            tool_results = []
            for block in output_message.get("content", []):
                tool_use = block.get("toolUse")
                if not tool_use:
                    continue

                tool_name = tool_use["name"]
                tool_use_id = tool_use["toolUseId"]
                tool_input = tool_use.get("input", {})
                result = execute_tool(tool_name, tool_input)

                tool_result = {
                    "toolUseId": tool_use_id,
                    "content": [{"json": result}],
                }
                if result.get("status") == "error":
                    tool_result["status"] = "error"

                tool_results.append({"toolResult": tool_result})

            if not tool_results:
                break

            messages.append({"role": "user", "content": tool_results})

        raise HTTPException(status_code=500, detail="Bedrock tool loop exceeded maximum turns")
        
    except ClientError as e:
        error_code = e.response['Error']['Code']
        if error_code == 'ValidationException':
            # Handle message format issues
            print(f"Bedrock validation error: {e}")
            raise HTTPException(status_code=400, detail="Invalid message format for Bedrock")
        elif error_code == 'AccessDeniedException':
            print(f"Bedrock access denied: {e}")
            raise HTTPException(status_code=403, detail="Access denied to Bedrock model")
        else:
            print(f"Bedrock error: {e}")
            raise HTTPException(status_code=500, detail=f"Bedrock error: {str(e)}")


@app.get("/")
async def root():
    return {
        "message": "AI Digital Twin API (Powered by AWS Bedrock)",
        "memory_enabled": True,
        "storage": "S3" if USE_S3 else "local",
        "ai_model": BEDROCK_MODEL_ID
    }


@app.get("/health")
async def health_check():
    return {
        "status": "healthy", 
        "use_s3": USE_S3,
        "bedrock_model": BEDROCK_MODEL_ID
    }


@app.post("/chat", response_model=ChatResponse)
async def chat(request: ChatRequest):
    try:
        # Generate session ID if not provided
        session_id = request.session_id or str(uuid.uuid4())

        # Load conversation history
        conversation = load_conversation(session_id)

        # Call Bedrock for response
        assistant_response = call_bedrock(conversation, request.message)

        # Update conversation history
        conversation.append(
            {"role": "user", "content": request.message, "timestamp": datetime.now().isoformat()}
        )
        conversation.append(
            {
                "role": "assistant",
                "content": assistant_response,
                "timestamp": datetime.now().isoformat(),
            }
        )

        # Save conversation
        save_conversation(session_id, conversation)

        return ChatResponse(response=assistant_response, session_id=session_id)

    except HTTPException:
        raise
    except Exception as e:
        print(f"Error in chat endpoint: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/conversation/{session_id}")
async def get_conversation(session_id: str):
    """Retrieve conversation history"""
    try:
        conversation = load_conversation(session_id)
        return {"session_id": session_id, "messages": conversation}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
