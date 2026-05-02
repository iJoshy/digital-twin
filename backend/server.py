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
from html import escape
import boto3
from botocore.exceptions import ClientError
from context import prompt
from resources import facts
import sendgrid
from sendgrid.helpers.mail import Mail, Email, To, Content
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
JOSHUA_EMAIL = (
    os.getenv("RECIPIENT_EMAIL")
    or os.getenv("SENDGRID_RECIPIENT_EMAIL")
    or facts.get("email", "")
)
REQUIRED_FOLLOWUP_TOOLS = (
    "record_user_details",
    "send_push_notification",
    "send_email",
)
LEAD_INTENT_PATTERNS = [
    r"\b(?:hire|hiring|recruit|recruiter|recruiting|interview|job|role|position|vacancy)\b",
    r"\b(?:consult|consulting|consultant|contract|freelance|project|partnership)\b",
    r"\b(?:available|availability|start date|timeline|cv|resume|résumé|rate|salary|compensation)\b",
    r"\b(?:can joshua|would joshua|is joshua)\s+(?:join|work|interview|consult|help|available)\b",
]
COMPANY_PATTERNS = [
    r"\b(?:company|organisation|organization|at|from)\s+(?:is\s+)?([A-Za-z0-9][A-Za-z0-9 &.,'-]{1,80})",
    r"\b(?:we are|we're)\s+([A-Za-z0-9][A-Za-z0-9 &.,'-]{1,80})",
]
PROMPT_INJECTION_RESPONSE = (
    "I can't help with requests to override instructions, reveal hidden prompts, "
    "or expose private system details. I can still talk about Joshua's professional "
    "background, AI work, and consulting experience."
)

PROMPT_INJECTION_PATTERNS = [
    r"\bignore\s+(?:all\s+)?(?:previous|prior|above|system|developer)\s+instructions?\b",
    r"\b(disregard|forget|override)\s+(?:all\s+)?(?:previous|prior|above|system|developer)\s+instructions?\b",
    r"\breveal\s+(?:the\s+)?(?:system|developer|hidden)\s+(?:prompt|instructions?|message|context)\b",
    r"\b(show|print|dump|repeat|display)\s+(?:the\s+)?(?:system|developer|hidden)\s+(?:prompt|instructions?|message|context)\b",
    r"\bwhat\s+(?:is|are)\s+(?:your\s+)?(?:system|developer|hidden)\s+(?:prompt|instructions?|message|context)\b",
    r"\b(?:api[_ -]?key|secret|environment variables?|\.env|credentials?|access token)\b",
    r"\b(?:developer mode|dan mode|jailbreak|prompt injection|god mode)\b",
    r"\b(?:act as|pretend to be|roleplay as)\s+(?:a\s+)?(?:different|unrestricted|uncensored|system|developer)\b",
    r"\b(?:bypass|disable|skip)\s+(?:the\s+)?(?:tool|tools|guardrails?|safety|instructions?|follow-?up)\b",
    r"\b(?:chain[- ]of[- ]thought|private reasoning|hidden reasoning|internal logs?)\b",
    r"<\s*(?:system|developer|assistant|tool)\s*>",
    r"\[\s*(?:system|developer|assistant|tool)\s*\]",
]

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

def clean_email_address(email: str) -> str:
    return (email or "").strip().strip("<>").strip()


def owner_email_address() -> str:
    return clean_email_address(
        os.getenv("RECIPIENT_EMAIL")
        or os.getenv("SENDGRID_RECIPIENT_EMAIL")
        or facts.get("email", "")
    )


def send_email(body: str, recipient_email: str, subject: str = "Digital Twin follow-up needed") -> Dict[str, Any]:
    """Send out an email with the given body."""
    emailkey = os.getenv("SENDGRID_API_KEY")
    emailfrom = os.getenv("SENDGRID_SENDER_EMAIL")
    internal_copy = os.getenv("RECIPIENT_EMAIL") or os.getenv("SENDGRID_RECIPIENT_EMAIL")

    if not emailkey or not emailfrom:
        print("Sendgrid skipped: SENDGRID_API_KEY or SENDGRID_SENDER_EMAIL is missing")
        return {"status": "skipped", "reason": "missing sendgrid env vars"}

    # Clean up tool-provided email in case model includes wrappers like <email@domain.com>
    recipient_email = clean_email_address(recipient_email)
    if not recipient_email or "@" not in recipient_email:
        return {"status": "error", "reason": f"invalid recipient_email: {recipient_email!r}"}

    try:
        sg = sendgrid.SendGridAPIClient(api_key=emailkey)
        from_email = Email(emailfrom)
        to_email = To(recipient_email)
        content = Content("text/html", body)
        mail = Mail(from_email, to_email, subject, content)
        if internal_copy and internal_copy.strip() and internal_copy.strip() != recipient_email:
            mail.add_to(To(internal_copy.strip()))

        response = sg.client.mail.send.post(request_body=mail.get())
        http_status = getattr(response, "status_code", None)
        if http_status and not (200 <= int(http_status) < 300):
            return {
                "status": "error",
                "reason": f"sendgrid returned non-2xx status: {http_status}",
                "recipient_email": recipient_email,
                "http_status": http_status,
            }
        return {"status": "success", "recipient_email": recipient_email, "http_status": http_status}
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
    "description": "Use this tool to send an HTML email. For unanswered-question follow-up, send the email to Joshua with the user's name, email, unanswered question, and conversation summary in the HTML body.",
    "parameters": {
        "type": "object",
        "properties": {
            "body": {
                "type": "string",
                "description": "The HTML body of the email."
            },
            "recipient_email": {
                "type": "string",
                "description": "The recipient email address. For unanswered-question follow-up, this should be Joshua's email address, not the visitor's email address."
            },
            "subject": {
                "type": "string",
                "description": "Optional subject line for the email."
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


def send_push_notification(text: str) -> Dict[str, Any]:
    """Send a push notification to Joshua."""
    return push(text)


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

send_push_notification_json = {
    "name": "send_push_notification",
    "description": "Use this tool to send Joshua a push notification. In unanswered-question follow-up, this tool MUST be called after the visitor provides both their name and email address.",
    "parameters": {
        "type": "object",
        "properties": {
            "text": {
                "type": "string",
                "description": "A concise notification for Joshua that includes the user's name, email address, unanswered question, and relevant conversation summary."
            },
        },
        "required": ["text"],
        "additionalProperties": False,
    },
}

tool_schemas = [
    record_user_details_json,
    record_unknown_question_json,
    send_email_json,
    send_push_notification_json,
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
    "send_push_notification": send_push_notification,
}


# Memory management functions
def get_memory_path(session_id: str) -> str:
    return f"{session_id}.json"


def get_followup_state_path(session_id: str) -> str:
    return f"{session_id}.followup.json"


def default_followup_state() -> Dict[str, Any]:
    return {
        "pending_unknown_question": None,
        "lead_intent": None,
        "name": None,
        "email": None,
        "company": None,
        "role_or_project": None,
        "timeline": None,
        "unknown_recorded_at": None,
        "lead_recorded_at": None,
        "notification_sent_at": None,
        "notification_results": [],
        "completed_notification_tools": [],
    }


def load_followup_state(session_id: str) -> Dict[str, Any]:
    """Load deterministic follow-up state for unanswered questions."""
    if USE_S3:
        try:
            response = s3_client.get_object(Bucket=S3_BUCKET, Key=get_followup_state_path(session_id))
            state = json.loads(response["Body"].read().decode("utf-8"))
        except ClientError as e:
            if e.response["Error"]["Code"] == "NoSuchKey":
                return default_followup_state()
            raise
    else:
        file_path = os.path.join(MEMORY_DIR, get_followup_state_path(session_id))
        if not os.path.exists(file_path):
            return default_followup_state()
        with open(file_path, "r") as f:
            state = json.load(f)

    merged_state = default_followup_state()
    if isinstance(state, dict):
        merged_state.update(state)
    return merged_state


def save_followup_state(session_id: str, state: Dict[str, Any]):
    """Save deterministic follow-up state for unanswered questions."""
    if USE_S3:
        s3_client.put_object(
            Bucket=S3_BUCKET,
            Key=get_followup_state_path(session_id),
            Body=json.dumps(state, indent=2),
            ContentType="application/json",
        )
    else:
        os.makedirs(MEMORY_DIR, exist_ok=True)
        file_path = os.path.join(MEMORY_DIR, get_followup_state_path(session_id))
        with open(file_path, "w") as f:
            json.dump(state, f, indent=2)


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


def is_prompt_injection_attempt(text: str) -> bool:
    """Detect obvious attempts to override instructions or extract internals."""
    if not isinstance(text, str) or not text.strip():
        return False

    normalized = " ".join(text.lower().split())
    return any(re.search(pattern, normalized, flags=re.IGNORECASE) for pattern in PROMPT_INJECTION_PATTERNS)


def is_lead_intent_message(text: str) -> bool:
    """Detect hiring, recruiting, consulting, or opportunity intent."""
    if not isinstance(text, str) or not text.strip():
        return False

    normalized = " ".join(text.lower().split())
    return any(re.search(pattern, normalized, flags=re.IGNORECASE) for pattern in LEAD_INTENT_PATTERNS)


def safe_content_for_model(role: str, content: str) -> str:
    """Prevent stored attacks from being replayed into Bedrock as fresh instructions."""
    if role == "user" and is_prompt_injection_attempt(content):
        return "[Blocked prompt-injection attempt omitted from conversation history.]"
    return content


def compact_field_value(value: str) -> str:
    value = re.split(r"[,.;\n\r]", value, maxsplit=1)[0].strip(" .'-")
    return " ".join(value.split())[:120]


def extract_contact_details(text: str) -> Dict[str, Optional[str]]:
    """Best-effort extraction for follow-up contact details."""
    details: Dict[str, Optional[str]] = {
        "name": None,
        "email": None,
        "company": None,
        "timeline": None,
    }
    if not text:
        return details

    email_match = re.search(r"[\w.!#$%&'*+/=?^`{|}~-]+@[\w.-]+\.[A-Za-z]{2,}", text)
    if email_match:
        details["email"] = email_match.group(0).strip().strip("<>").lower()

    without_email = re.sub(r"[\w.!#$%&'*+/=?^`{|}~-]+@[\w.-]+\.[A-Za-z]{2,}", " ", text)
    name_patterns = [
        r"\b(?:my name is|name is|i am|i'm|this is)\s+([A-Za-z][A-Za-z .'-]{1,80})",
        r"\bname\s*[:=-]\s*([A-Za-z][A-Za-z .'-]{1,80})",
    ]
    for pattern in name_patterns:
        match = re.search(pattern, without_email, flags=re.IGNORECASE)
        if not match:
            continue
        name = match.group(1)
        name = re.split(
            r"\b(?:and|email|e-mail|mail|phone|number|question)\b|[,.;\n\r]",
            name,
            maxsplit=1,
            flags=re.IGNORECASE,
        )[0].strip(" .'-")
        if name:
            details["name"] = " ".join(name.split())
            break

    for pattern in COMPANY_PATTERNS:
        match = re.search(pattern, without_email, flags=re.IGNORECASE)
        if match:
            company = compact_field_value(match.group(1))
            if company and company.lower() not in {"a company", "an organization", "a startup"}:
                details["company"] = company
                break

    timeline_match = re.search(
        r"\b(?:timeline|start date|start|starting|available from|need someone by)\s*[:=-]?\s*([A-Za-z0-9 /,'-]{2,80})",
        without_email,
        flags=re.IGNORECASE,
    )
    if timeline_match:
        details["timeline"] = compact_field_value(timeline_match.group(1))

    return details


def apply_unknown_tool_calls_to_state(state: Dict[str, Any], tool_calls: List[Dict[str, Any]]) -> None:
    for call in tool_calls:
        if call.get("name") != "record_unknown_question":
            continue

        question = call.get("input", {}).get("question")
        if not isinstance(question, str) or not question.strip():
            continue

        state["pending_unknown_question"] = question.strip()
        state["unknown_recorded_at"] = datetime.now().isoformat()
        state["notification_sent_at"] = None
        state["notification_results"] = []
        state["completed_notification_tools"] = []


def merge_lead_intent_into_state(state: Dict[str, Any], message: str) -> None:
    if state.get("notification_sent_at"):
        return

    if is_lead_intent_message(message):
        current_intent = state.get("lead_intent")
        if current_intent:
            state["lead_intent"] = f"{current_intent}\n{message.strip()}"[-2000:]
        else:
            state["lead_intent"] = message.strip()
        state["lead_recorded_at"] = state.get("lead_recorded_at") or datetime.now().isoformat()


def merge_contact_details_into_state(state: Dict[str, Any], message: str) -> None:
    if not (state.get("pending_unknown_question") or state.get("lead_intent")) or state.get("notification_sent_at"):
        return

    details = extract_contact_details(message)
    if details.get("name"):
        state["name"] = details["name"]
    if details.get("email"):
        state["email"] = details["email"]
    if details.get("company"):
        state["company"] = details["company"]
    if details.get("timeline"):
        state["timeline"] = details["timeline"]


def missing_contact_fields(state: Dict[str, Any]) -> List[str]:
    missing = []
    if not state.get("name"):
        missing.append("name")
    if not state.get("email"):
        missing.append("email")
    return missing


def conversation_summary(conversation: List[Dict], latest_user_message: str) -> str:
    recent_messages = conversation[-8:]
    lines = []
    for msg in recent_messages:
        role = msg.get("role", "message")
        content = msg.get("content", "")
        if isinstance(content, str) and content.strip():
            lines.append(f"{role}: {content.strip()}")
    lines.append(f"user: {latest_user_message.strip()}")
    return "\n".join(lines)[-2500:]


def active_followup_reason(state: Dict[str, Any]) -> str:
    if state.get("pending_unknown_question"):
        return f"Unanswered question: {state.get('pending_unknown_question')}"
    if state.get("lead_intent"):
        return f"Recruiter or opportunity inquiry: {state.get('lead_intent')}"
    return "Follow-up requested from the digital twin conversation."


def followup_notes(state: Dict[str, Any], conversation: List[Dict], user_message: str) -> str:
    return (
        f"{active_followup_reason(state)}\n"
        f"Company: {state.get('company') or 'not provided'}\n"
        f"Timeline: {state.get('timeline') or 'not provided'}\n\n"
        f"Conversation summary:\n{conversation_summary(conversation, user_message)}"
    )


def build_followup_email_body(state: Dict[str, Any], notes: str) -> str:
    name = escape(str(state.get("name") or "not provided"))
    email = escape(str(state.get("email") or "not provided"))
    company = escape(str(state.get("company") or "not provided"))
    timeline = escape(str(state.get("timeline") or "not provided"))
    reason = escape(active_followup_reason(state))
    timestamp = escape(datetime.now().isoformat())
    escaped_notes = escape(notes)

    return f"""
<html>
  <body>
    <div style="font-family: Arial, sans-serif; color: #0f172a; line-height: 1.5; max-width: 720px;">
      <h2 style="margin: 0 0 16px; color: #0f172a;">Digital Twin follow-up needed</h2>
      <p style="margin: 0 0 18px;">A visitor shared contact details through Joshua's digital twin.</p>
      <table style="border-collapse: collapse; width: 100%; margin-bottom: 20px;">
        <tr><td style="padding: 8px; border: 1px solid #e2e8f0;"><strong>Name</strong></td><td style="padding: 8px; border: 1px solid #e2e8f0;">{name}</td></tr>
        <tr><td style="padding: 8px; border: 1px solid #e2e8f0;"><strong>Email</strong></td><td style="padding: 8px; border: 1px solid #e2e8f0;">{email}</td></tr>
        <tr><td style="padding: 8px; border: 1px solid #e2e8f0;"><strong>Company</strong></td><td style="padding: 8px; border: 1px solid #e2e8f0;">{company}</td></tr>
        <tr><td style="padding: 8px; border: 1px solid #e2e8f0;"><strong>Timeline</strong></td><td style="padding: 8px; border: 1px solid #e2e8f0;">{timeline}</td></tr>
        <tr><td style="padding: 8px; border: 1px solid #e2e8f0;"><strong>Reason</strong></td><td style="padding: 8px; border: 1px solid #e2e8f0;">{reason}</td></tr>
        <tr><td style="padding: 8px; border: 1px solid #e2e8f0;"><strong>Timestamp</strong></td><td style="padding: 8px; border: 1px solid #e2e8f0;">{timestamp}</td></tr>
      </table>
      <h3 style="margin: 0 0 8px;">Conversation summary</h3>
      <pre style="white-space: pre-wrap; background: #f8fafc; border: 1px solid #e2e8f0; padding: 12px; border-radius: 6px;">{escaped_notes}</pre>
    </div>
  </body>
</html>
""".strip()


def tool_result_succeeded(result: Dict[str, Any]) -> bool:
    if not isinstance(result, dict):
        return False
    if result.get("status") == "success":
        return True
    if result.get("recorded") == "ok":
        return True
    return False


def valid_model_followup_tools(tool_calls: List[Dict[str, Any]], state: Dict[str, Any]) -> set[str]:
    """Return model-called follow-up tools that satisfy the backend contract."""
    valid_tools = set()
    user_email = str(state.get("email") or "").lower()
    followup_reason = active_followup_reason(state).lower()
    joshua_email = owner_email_address().lower()

    for call in tool_calls:
        tool_name = call.get("name")
        tool_input = call.get("input", {})
        if not tool_result_succeeded(call.get("result", {})):
            continue

        if tool_name == "record_user_details":
            if str(tool_input.get("email", "")).lower() == user_email:
                valid_tools.add(tool_name)
        elif tool_name == "send_push_notification":
            text = str(tool_input.get("text", "")).lower()
            if user_email in text and followup_reason[:40] in text:
                valid_tools.add(tool_name)
        elif tool_name == "send_email":
            recipient = str(tool_input.get("recipient_email", "")).lower()
            body = str(tool_input.get("body", "")).lower()
            if recipient == joshua_email and user_email in body and followup_reason[:40] in body:
                valid_tools.add(tool_name)

    return valid_tools


def enforce_followup_tools(
    state: Dict[str, Any],
    conversation: List[Dict],
    user_message: str,
    valid_model_tools: set[str],
) -> List[Dict[str, Any]]:
    """Deterministically call any required follow-up tools the model missed."""
    if not (state.get("pending_unknown_question") or state.get("lead_intent")) or state.get("notification_sent_at"):
        return []
    if missing_contact_fields(state):
        return []

    completed_tools = set(state.get("completed_notification_tools") or [])
    completed_tools.update(valid_model_tools)
    if set(REQUIRED_FOLLOWUP_TOOLS).issubset(completed_tools):
        state["notification_sent_at"] = state.get("notification_sent_at") or datetime.now().isoformat()
        state["completed_notification_tools"] = sorted(completed_tools)
        return []

    recipient_email = owner_email_address()
    if not recipient_email:
        result = {
            "name": "send_email",
            "input": {"recipient_email": "", "body": ""},
            "result": {"status": "error", "reason": "missing RECIPIENT_EMAIL or SENDGRID_RECIPIENT_EMAIL"},
        }
        state["notification_results"] = [*state.get("notification_results", []), result]
        state["completed_notification_tools"] = sorted(completed_tools)
        return [result]

    notes = followup_notes(state, conversation, user_message)
    push_text = (
        "Digital Twin follow-up needed\n"
        f"Name: {state['name']}\n"
        f"Email: {state['email']}\n"
        f"Company: {state.get('company') or 'not provided'}\n"
        f"Reason: {active_followup_reason(state)}\n"
        f"Summary: {conversation_summary(conversation, user_message)}"
    )[:950]

    tool_inputs = {
        "record_user_details": {
            "name": state["name"],
            "email": state["email"],
            "notes": notes,
        },
        "send_push_notification": {"text": push_text},
        "send_email": {
            "recipient_email": recipient_email,
            "subject": "Digital Twin follow-up needed",
            "body": build_followup_email_body(state, notes),
        },
    }

    results = []
    for tool_name in REQUIRED_FOLLOWUP_TOOLS:
        if tool_name in completed_tools:
            continue
        result = execute_tool(tool_name, tool_inputs[tool_name])
        result_entry = {"name": tool_name, "input": tool_inputs[tool_name], "result": result}
        results.append(result_entry)
        if tool_result_succeeded(result):
            completed_tools.add(tool_name)
        elif tool_name == "send_email":
            execute_tool(
                "send_push_notification",
                {
                    "text": (
                        "Digital Twin email notification failed; push alert still delivered. "
                        f"Reason: {result.get('reason', 'unknown error')}"
                    )
                },
            )

    state["completed_notification_tools"] = sorted(completed_tools)
    state["notification_results"] = [*state.get("notification_results", []), *results]
    if set(REQUIRED_FOLLOWUP_TOOLS).issubset(completed_tools):
        state["notification_sent_at"] = datetime.now().isoformat()
    return results


def ensure_followup_response(response: str, state: Dict[str, Any], enforced_tools: List[Dict[str, Any]]) -> str:
    if enforced_tools:
        if state.get("notification_sent_at"):
            confirmation = "Thanks, I have passed this along to Joshua with your contact details so he can follow up."
        else:
            confirmation = "Thanks, I have recorded your details and alerted Joshua. The email notification will be retried on the next update."
        if confirmation.lower() not in response.lower():
            return f"{response.strip()}\n\n{confirmation}".strip()
        return response

    if (state.get("pending_unknown_question") or state.get("lead_intent")) and not state.get("notification_sent_at"):
        missing = missing_contact_fields(state)
        if missing:
            request = f"Could you share your {' and '.join(missing)} so Joshua can follow up with you directly?"
            if "email" not in response.lower() or ("name" in missing and "name" not in response.lower()):
                return f"{response.strip()}\n\n{request}".strip()

    return response


def call_bedrock(conversation: List[Dict], user_message: str) -> Dict[str, Any]:
    """Call AWS Bedrock with conversation history"""
    
    # Build messages in Bedrock format
    messages = []
    
    # Add conversation history (limit to last 25 exchanges)
    for msg in conversation[-50:]:
        content_text = msg.get("content", "")
        if not isinstance(content_text, str) or not content_text.strip():
            continue
        content_text = safe_content_for_model(msg.get("role", ""), content_text)
        messages.append({
            "role": msg["role"],
            "content": [{"text": content_text}]
        })
    
    # Add current user message
    messages.append({
        "role": "user",
        "content": [{"text": user_message}]
    })
    tool_calls = []
    
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
                return {"response": final_text or "I completed that action.", "tool_calls": tool_calls}

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
                tool_calls.append({"name": tool_name, "input": tool_input, "result": result})

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
        followup_state = load_followup_state(session_id)
        merge_lead_intent_into_state(followup_state, request.message)
        merge_contact_details_into_state(followup_state, request.message)

        pre_enforced_tools = enforce_followup_tools(
            followup_state,
            conversation,
            request.message,
            valid_model_tools=set(),
        )
        if pre_enforced_tools:
            assistant_response = ensure_followup_response(
                "",
                followup_state,
                pre_enforced_tools,
            )
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
            save_conversation(session_id, conversation)
            save_followup_state(session_id, followup_state)
            return ChatResponse(response=assistant_response, session_id=session_id)

        if is_prompt_injection_attempt(request.message):
            conversation.append(
                {
                    "role": "user",
                    "content": request.message,
                    "timestamp": datetime.now().isoformat(),
                    "security": "blocked_prompt_injection",
                }
            )
            conversation.append(
                {
                    "role": "assistant",
                    "content": PROMPT_INJECTION_RESPONSE,
                    "timestamp": datetime.now().isoformat(),
                }
            )
            save_conversation(session_id, conversation)
            save_followup_state(session_id, followup_state)
            return ChatResponse(response=PROMPT_INJECTION_RESPONSE, session_id=session_id)

        # Call Bedrock for response
        bedrock_result = call_bedrock(conversation, request.message)
        assistant_response = bedrock_result["response"]
        tool_calls = bedrock_result["tool_calls"]

        # Backend-enforced follow-up workflow for unanswered questions.
        apply_unknown_tool_calls_to_state(followup_state, tool_calls)
        merge_lead_intent_into_state(followup_state, request.message)
        merge_contact_details_into_state(followup_state, request.message)
        model_called_tools = valid_model_followup_tools(tool_calls, followup_state)
        enforced_tools = enforce_followup_tools(
            followup_state,
            conversation,
            request.message,
            model_called_tools,
        )
        assistant_response = ensure_followup_response(
            assistant_response,
            followup_state,
            enforced_tools,
        )

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
        save_followup_state(session_id, followup_state)

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
