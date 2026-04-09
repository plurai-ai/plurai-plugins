#!/usr/bin/env python3
"""Pluto Judge MCP Server — zero external dependencies.

Exposes tools for creating LLM-as-a-judge evaluators via the Pluto platform.
Uses only Python stdlib (urllib, json, ssl, uuid).
"""

import json
import ssl
import sys
import uuid
from urllib.request import Request, urlopen
from urllib.error import HTTPError

# ── MCP Protocol (stdio JSON-RPC) ──────────────────────────────────────────

def _write_msg(obj):
    out = json.dumps(obj)
    # Write as JSON line (Claude Code uses JSON-line protocol)
    sys.stdout.buffer.write(out.encode())
    sys.stdout.buffer.write(b"\n")
    sys.stdout.buffer.flush()

_next_req_id = 1000

def send_response(id, result):
    _write_msg({"jsonrpc": "2.0", "id": id, "result": result})

def send_error(id, code, message):
    _write_msg({"jsonrpc": "2.0", "id": id, "error": {"code": code, "message": message}})

def send_request(method, params):
    """Send a JSON-RPC request TO the client (e.g. elicitation) and read the response."""
    global _next_req_id
    req_id = _next_req_id
    _next_req_id += 1
    out_msg = {"jsonrpc": "2.0", "id": req_id, "method": method, "params": params}
    _log.debug("SEND_REQUEST out: %s", json.dumps(out_msg)[:500])
    _write_msg(out_msg)
    # Read the response (blocking)
    while True:
        msg = read_message()
        if msg is None:
            _log.debug("SEND_REQUEST: got None (EOF)")
            return None
        _log.debug("SEND_REQUEST in: %s", json.dumps(msg)[:500])
        # Match by id — it's our response
        if msg.get("id") == req_id:
            if "error" in msg:
                _log.debug("SEND_REQUEST error: %s", msg.get("error"))
                return None  # elicitation not supported or failed
            return msg.get("result")
        # If it's a different message (notification, etc.), skip and keep waiting

import logging as _log
_log.basicConfig(level=_log.WARNING)  # Set to DEBUG for troubleshooting

def elicit_form(message, schema):
    """Ask the user a question via MCP elicitation. Returns {action, content} or None."""
    params = {"message": message, "requestedSchema": schema}
    _log.debug("ELICIT REQUEST: %s", json.dumps(params, indent=2)[:1000])
    result = send_request("elicitation/create", params)
    _log.debug("ELICIT RESPONSE: %s", json.dumps(result, indent=2) if result else "None")
    return result

def read_message():
    while True:
        line = sys.stdin.buffer.readline()
        if not line:
            return None
        text = line.decode().strip()
        if not text:
            continue
        # JSON-line protocol (Claude Code)
        if text.startswith("{"):
            return json.loads(text)
        # Content-Length framed protocol (standard MCP)
        if text.lower().startswith("content-length:"):
            length = int(text.split(":", 1)[1].strip())
            sys.stdin.buffer.readline()  # blank line separator
            body = sys.stdin.buffer.read(length).decode()
            return json.loads(body)

# ── HTTP helpers (stdlib only) ─────────────────────────────────────────────

_SSL_CTX = ssl.create_default_context()

def http_request(method, url, body=None, headers=None, timeout=30):
    """Make an HTTP request, return parsed JSON."""
    data = json.dumps(body).encode() if body else None
    req = Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    if headers:
        for k, v in headers.items():
            req.add_header(k, v)
    with urlopen(req, timeout=timeout, context=_SSL_CTX) as resp:
        return json.loads(resp.read().decode())

def http_stream(url, body, headers, timeout=300):
    """POST and stream SSE lines, return list of parsed events."""
    data = json.dumps(body).encode()
    req = Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("Accept", "text/event-stream")
    for k, v in headers.items():
        req.add_header(k, v)
    events = []
    with urlopen(req, timeout=timeout, context=_SSL_CTX) as resp:
        for raw_line in resp:
            line = raw_line.decode().strip()
            if line.startswith("data: "):
                try:
                    events.append(json.loads(line[6:]))
                except json.JSONDecodeError:
                    pass
    return events

# ── Credentials ────────────────────────────────────────────────────────────

import os
import hashlib
import sqlite3
import shutil
import subprocess
import tempfile
import time

PLUTO_API = "https://pluto.plurai.ai/api/pluto"
AGENT_API = "https://pluto.plurai.ai/api/agent/api/copilotkit"

_agent_has_questions = False  # Set to True after pluto_send_message returns refinement questions
_classifier_by_thread = {}   # Track classifier ID per thread: {thread_id: classifier_id}
CLERK_FAPI = "https://clerk.plurai.ai/v1"

# ── Chrome cookie reader (hack for local dev/testing) ─────────────────────

_CHROME_SAFE_STORAGE = os.environ.get("CHROME_SAFE_STORAGE", "")
_CHROME_KEY = hashlib.pbkdf2_hmac(
    'sha1',
    _CHROME_SAFE_STORAGE.encode(),
    b'saltysalt', 1003, dklen=16
) if _CHROME_SAFE_STORAGE else None
_token_cache = {}  # {template: (jwt, expire_time)}

def _decrypt_chrome_cookie(enc_value):
    """Decrypt a Chrome v10-encrypted cookie value."""
    if enc_value[:3] != b'v10':
        return None
    if not _CHROME_KEY:
        return None
    key = _CHROME_KEY
    iv = b' ' * 16
    with tempfile.NamedTemporaryFile(delete=False) as f:
        f.write(enc_value[3:])
        f.flush()
        result = subprocess.run(
            ['openssl', 'enc', '-aes-128-cbc', '-d', '-nopad',
             '-K', key.hex(), '-iv', iv.hex(), '-in', f.name],
            capture_output=True
        )
        os.unlink(f.name)
    dec = result.stdout
    if not dec:
        return None
    pad_len = dec[-1]
    if 0 < pad_len <= 16:
        dec = dec[:-pad_len]
    value = dec.decode('utf-8', errors='replace')
    # Find JWT start (after possible garbage prefix from block cipher)
    jwt_start = value.find('eyJ')
    return value[jwt_start:] if jwt_start >= 0 else value

def _read_chrome_cookie(host_pattern, cookie_name):
    """Read a cookie from Chrome's cookie DB."""
    for profile in ['Profile 1', 'Default']:
        db_path = os.path.expanduser(f'~/Library/Application Support/Google/Chrome/{profile}/Cookies')
        if not os.path.exists(db_path):
            continue
        tmp = tempfile.mktemp(suffix='.db')
        shutil.copy2(db_path, tmp)
        try:
            conn = sqlite3.connect(tmp)
            cur = conn.cursor()
            cur.execute(
                "SELECT encrypted_value FROM cookies WHERE host_key LIKE ? AND name = ? ORDER BY expires_utc DESC LIMIT 1",
                (host_pattern, cookie_name)
            )
            row = cur.fetchone()
            conn.close()
            if row:
                return _decrypt_chrome_cookie(row[0])
        finally:
            os.unlink(tmp)
    return None

def _get_client_cookie_and_session_id():
    """Read __client JWT and extract session ID from __session JWT."""
    client_jwt = _read_chrome_cookie('%clerk.plurai.ai%', '__client')
    session_jwt = _read_chrome_cookie('%pluto.plurai.ai%', '__session')
    if not session_jwt:
        return None, None
    # Extract session ID from JWT payload
    import base64
    parts = session_jwt.split('.')
    if len(parts) < 2:
        return client_jwt, None
    p = parts[1] + '=' * (4 - len(parts[1]) % 4)
    payload = json.loads(base64.urlsafe_b64decode(p))
    return client_jwt, payload.get('sid')

def get_token(template=None):
    """Get a fresh Clerk JWT via Frontend API using Chrome cookies."""
    cache_key = template or '__default__'
    cached = _token_cache.get(cache_key)
    if cached and cached[1] > time.time():
        return cached[0]

    client_jwt, session_id = _get_client_cookie_and_session_id()
    if not session_id:
        raise RuntimeError("No active Pluto session found in Chrome. Log in at pluto.plurai.ai first.")

    path = f"{CLERK_FAPI}/client/sessions/{session_id}/tokens"
    if template:
        path += f"/{template}"
    path += "?_clerk_js_version=5.0.0"

    data = b''
    req = Request(path, data=data, method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    req.add_header("Origin", "https://pluto.plurai.ai")
    req.add_header("User-Agent", "Mozilla/5.0")
    if client_jwt:
        req.add_header("Cookie", f"__client={client_jwt}")

    with urlopen(req, timeout=10, context=_SSL_CTX) as resp:
        result = json.loads(resp.read().decode())

    jwt = result.get("jwt") or result.get("response", {}).get("jwt")
    if jwt:
        _token_cache[cache_key] = (jwt, time.time() + 50)  # cache for 50s (tokens last 60s)
    return jwt

def pluto_headers():
    return {"Authorization": f"Bearer {get_token()}"}

def agent_headers():
    return {"Authorization": f"Bearer {get_token('pluto-agent-authz')}"}

# ── Tool implementations ───────────────────────────────────────────────────

def tool_start_judge(args):
    """Create thread, send task to agent, and present refinement questions — all in one step."""
    _log.debug("tool_start_judge called, task_description length: %d", len(args.get("task_description", "")))
    name = args.get("name", "evaluator")
    # Enforce short names — truncate to max 5 words / 50 chars
    words = name.split()
    if len(words) > 5:
        name = " ".join(words[:5])
    if len(name) > 50:
        name = name[:50].rsplit(" ", 1)[0]
    task_description = args["task_description"]

    # Step 1: Create thread
    headers = pluto_headers()
    thread = http_request("POST", f"{PLUTO_API}/threads",
        body={"workflow": "with-data"}, headers=headers)
    thread_id = thread["id"]
    try:
        http_request("PATCH", f"{PLUTO_API}/threads/{thread_id}",
            body={"name": name}, headers=headers)
    except Exception:
        pass

    # Step 2: Send task description to agent
    a_headers = agent_headers()
    payload = {
        "method": "agent/run",
        "params": {"agentId": "agent"},
        "body": {
            "threadId": thread_id,
            "runId": str(uuid.uuid4()),
            "state": {},
            "messages": [{"id": str(uuid.uuid4()), "role": "user", "content": task_description}],
            "tools": [],
            "context": [],
            "forwardedProps": {},
        },
    }
    events = http_stream(AGENT_API, payload, a_headers, timeout=300)

    conversation = []
    for event in events:
        etype = event.get("type", "")
        if etype == "MESSAGES_SNAPSHOT":
            conversation = [
                {"role": m["role"], "content": m["content"]}
                for m in event.get("messages", [])
                if m.get("content") and m["content"] != "..."
            ]

    agent_response = ""
    for msg in reversed(conversation):
        if msg["role"] == "assistant":
            agent_response = msg["content"]
            break

    # Step 3: Enable pluto_ask_user, reset classifier from previous thread
    global _agent_has_questions, _start_judge_used
    _agent_has_questions = True
    _start_judge_used = True

    return {
        "thread_id": thread_id,
        "example_set_id": thread.get("exampleSetId", ""),
        "url": f"https://pluto.plurai.ai/thread/{thread_id}",
        "agent_response": agent_response,
        "action_required": "PRESENT_QUESTIONS_TO_USER",
        "instructions": (
            "The agent returned refinement questions. "
            "First call ToolSearch with query 'pluto_ask_user' to load the tool, "
            "then call pluto_ask_user with the questions rephrased as options. "
            "Do NOT present the questions as text."
        ),
    }



def tool_upload_data(args):
    """Upload labeled examples to a thread. ONLY use with data the user explicitly provided from a file."""
    example_set_id = args["example_set_id"]
    records = args["records"]  # [{"sample": "...", "label": "...", "reasoning": "..."}]
    file_name = args.get("file_name", "examples.csv")
    source = args.get("source", "")
    headers = pluto_headers()
    result = http_request("POST", f"{PLUTO_API}/example-sets/{example_set_id}/files",
        body={"fileName": file_name, "records": records}, headers=headers, timeout=60)
    return {"status": "uploaded", "count": len(records), "source": source}

_start_judge_used = False  # Track if pluto_start_judge was called


def _check_optimization_status(thread_id):
    """Check if optimization is already done or in progress. Returns a result dict or None."""
    classifier_id = _classifier_by_thread.get(thread_id)
    if not classifier_id:
        return None

    try:
        headers = pluto_headers()
        classifier = http_request("GET", f"{PLUTO_API}/classifiers/{classifier_id}", headers=headers)
        slug = classifier["slug"]
        version = classifier.get("defaultVersion", {}).get("number", "1.0.0")

        # Try to get optimization results (UUID first, then slug)
        opt = None
        for identifier in [classifier_id, slug]:
            try:
                opt = http_request("GET",
                    f"{PLUTO_API}/classifiers/{identifier}/versions/{version}/optimization",
                    headers=headers)
                break  # Got results, stop trying
            except HTTPError as e:
                if e.code == 404:
                    continue
                raise

        if opt:
            baseline = opt.get("baseline", {})
            optimized = opt.get("optimized", {})

            if optimized and optimized.get("accuracy") is not None:
                return {
                    "status": "already_optimized",
                    "message": "Optimization was already completed. Here are the results.",
                    "classifier_id": classifier_id,
                    "slug": slug,
                    "version": version,
                    "endpoint_url": f"https://run.plurai.ai/ioa/v1/{slug}/{version}",
                    "dashboard_url": f"https://pluto.plurai.ai/classifier/{slug}/{version}",
                    "baseline": {
                        "accuracy": baseline.get("accuracy"),
                        "precision": baseline.get("precision"),
                        "recall": baseline.get("recall"),
                    },
                    "optimized": {
                        "accuracy": optimized.get("accuracy"),
                        "precision": optimized.get("precision"),
                        "recall": optimized.get("recall"),
                    },
                }
            elif baseline and baseline.get("accuracy") is not None:
                return {
                    "status": "optimization_in_progress",
                    "message": "Optimization is already running. Baseline results are available. "
                               "Wait for optimization to complete, then call pluto_get_results.",
                    "classifier_id": classifier_id,
                    "baseline": {
                        "accuracy": baseline.get("accuracy"),
                        "precision": baseline.get("precision"),
                        "recall": baseline.get("recall"),
                    },
                }
    except Exception:
        pass  # Can't check — proceed normally

    return None  # No optimization found — proceed with sending the message


def tool_send_message(args):
    """Send a message to the Pluto agent and get the response."""
    _log.debug("tool_send_message called, message length: %d, message: %s", len(args.get("message", "")), args.get("message", "")[:100])
    thread_id = args["thread_id"]
    message = args["message"]

    # Block bare "Optimize" — must include [LLM] or [SLM]
    if message.strip().lower() == "optimize":
        return {
            "error": "Do not send 'Optimize' alone. You must send exactly 'Optimize [LLM]' or 'Optimize [SLM]'."
        }

    # If this is an optimization request, check if already done or in progress
    if message.strip().lower().startswith("optimize"):
        status = _check_optimization_status(thread_id)
        if status:
            return status

    headers = agent_headers()

    payload = {
        "method": "agent/run",
        "params": {"agentId": "agent"},
        "body": {
            "threadId": thread_id,
            "runId": str(uuid.uuid4()),
            "state": {},
            "messages": [{"id": str(uuid.uuid4()), "role": "user", "content": message}],
            "tools": [],
            "context": [],
            "forwardedProps": {},
        },
    }

    events = http_stream(AGENT_API, payload, headers, timeout=300)

    # Extract conversation and classifier_id from events
    conversation = []
    classifier_id = None
    for event in events:
        etype = event.get("type", "")
        if etype == "MESSAGES_SNAPSHOT":
            conversation = [
                {"role": m["role"], "content": m["content"]}
                for m in event.get("messages", [])
                if m.get("content") and m["content"] != "..."
            ]
        elif etype == "STATE_SNAPSHOT":
            snapshot = event.get("snapshot", {})
            if isinstance(snapshot, dict) and "classifier_id" in snapshot:
                classifier_id = snapshot["classifier_id"]

    # Get last assistant message
    agent_response = ""
    for msg in reversed(conversation):
        if msg["role"] == "assistant":
            agent_response = msg["content"]
            break

    result = {
        "agent_response": agent_response,
        "message_count": len(conversation),
    }
    global _agent_has_questions
    if classifier_id:
        result["classifier_id"] = classifier_id
        _classifier_by_thread[thread_id] = classifier_id

    # If the response contains refinement questions, wrap with instructions
    if "?" in agent_response and not classifier_id:
        _agent_has_questions = True
        result["action_required"] = "PRESENT_QUESTIONS_TO_USER"
        result["instructions"] = (
            "The agent returned refinement questions. You MUST call pluto_ask_user to present them. "
            "Do NOT answer these questions yourself. Do NOT output any text before calling pluto_ask_user.\n\n"
            "FORMAT RULES:\n"
            "- Labels question: option 1 label = the EXACT label names from brackets joined with ' / ' plus '(Recommended)'. "
            "Option 2 = suggest SPECIFIC alternative label names relevant to the task (e.g. 'pass / fail', 'safe / unsafe', 'grounded / hallucinated'). "
            "Do NOT just say 'Suggest different labels' — provide actual alternative names.\n"
            "- Other questions: 2-3 short options, labels under 8 words."
        )
    return result


def tool_ask_user(args):
    """Present questions to the user via interactive form and return their answers."""
    global _agent_has_questions
    if not _agent_has_questions:
        return {
            "error": "You must call pluto_start_judge first. "
                     "Do NOT ask your own questions."
        }
    _agent_has_questions = False  # Reset after use
    questions = args["questions"]

    properties = {}
    required = []
    for i, q in enumerate(questions):
        field_name = f"q{i+1}"
        required.append(field_name)
        title = q["question"]
        options = q.get("options", [])

        if options:
            properties[field_name] = {
                "type": "string",
                "title": title,
                "oneOf": [{"const": o["value"], "title": o["label"]} for o in options],
            }
        else:
            properties[field_name] = {
                "type": "string",
                "title": title,
            }

    schema = {
        "type": "object",
        "properties": properties,
        "required": required,
    }

    result = elicit_form("Please answer these questions:", schema)
    if result and result.get("action") == "accept":
        content = result.get("content", {})
        answers = {}
        for i, q in enumerate(questions):
            field_name = f"q{i+1}"
            answers[q["question"]] = content.get(field_name, "")
        return {"answers": answers, "action": "accepted"}

    # Elicitation was declined (VS Code doesn't support it).
    # Fall back: return the questions formatted for AskUserQuestion
    # so Claude can call it directly.
    ask_user_questions = []
    for q in questions:
        opts = q.get("options", [])
        ask_user_questions.append({
            "question": q["question"],
            "header": q["question"][:12],
            "options": [{"label": o["label"], "description": o.get("value", o["label"])} for o in opts],
            "multiSelect": False,
        })

    # Detect if this is an optimization question and add specific instructions
    is_optimization = any("optim" in q.get("question", "").lower() or "slm" in q.get("question", "").lower() or "llm" in q.get("question", "").lower() for q in questions)
    extra_instructions = ""
    if is_optimization:
        extra_instructions = (
            " IMPORTANT: After the user chooses, call pluto_send_message with EXACTLY "
            "message='Optimize [LLM]' or message='Optimize [SLM]'. "
            "One call only. These are hardcoded strings — do not modify them."
        )

    return {
        "action": "elicitation_unavailable",
        "fallback": "AskUserQuestion",
        "instructions": (
            "Elicitation is not available in this environment. "
            "You MUST now call the AskUserQuestion tool with the questions below. "
            "Use ToolSearch to load it first if needed. Do NOT answer the questions yourself."
            + extra_instructions
        ),
        "askUserQuestions": ask_user_questions,
    }

def tool_search_evaluators(args):
    """Search existing evaluators/classifiers on the Pluto platform."""
    headers = pluto_headers()
    classifiers = http_request("GET", f"{PLUTO_API}/classifiers", headers=headers)
    items = classifiers.get("items", [])

    results = []
    for c in items:
        slug = c.get("slug", "")
        version = c.get("defaultVersion", {}).get("number", "1.0.0")
        has_optimization = False
        for identifier in [c["id"], slug]:
            try:
                http_request("GET",
                    f"{PLUTO_API}/classifiers/{identifier}/versions/{version}/optimization",
                    headers=headers)
                has_optimization = True
                break
            except Exception:
                continue

        results.append({
            "id": c["id"],
            "name": c.get("name", ""),
            "description": (c.get("description") or "")[:200],
            "slug": slug,
            "labels": [p for p in (c.get("outputSchema", {}).get("properties", {}).get("label", {}).get("enum", []))],
            "endpoint_url": f"https://run.plurai.ai/ioa/v1/{slug}/{version}",
            "dashboard_url": f"https://pluto.plurai.ai/classifier/{slug}/{version}",
            "has_optimization": has_optimization,
            "created_at": c.get("createdAt", ""),
        })

    return {
        "count": len(results),
        "evaluators": results,
        "instructions": (
            "Show the user the existing evaluators. If one matches their task, "
            "ask if they want to reuse it (use its endpoint) or create a new one."
        ),
    }


def tool_get_results(args):
    """Get optimization results and endpoint info for a classifier."""
    classifier_id = args["classifier_id"]
    headers = pluto_headers()

    # Get classifier details
    classifier = http_request("GET", f"{PLUTO_API}/classifiers/{classifier_id}", headers=headers)
    slug = classifier["slug"]
    version = classifier.get("defaultVersion", {}).get("number", "1.0.0")

    # Get optimization results — try UUID first, then slug
    baseline = {}
    optimized = {}
    for identifier in [classifier_id, slug]:
        try:
            opt = http_request("GET",
                f"{PLUTO_API}/classifiers/{identifier}/versions/{version}/optimization",
                headers=headers)
            baseline = opt.get("baseline", {})
            optimized = opt.get("optimized", {})
            break
        except Exception:
            continue

    return {
        "classifier_id": classifier_id,
        "slug": slug,
        "version": version,
        "endpoint_url": f"https://run.plurai.ai/ioa/v1/{slug}/{version}",
        "dashboard_url": f"https://pluto.plurai.ai/classifier/{slug}/{version}",
        "baseline": {
            "accuracy": baseline.get("accuracy"),
            "precision": baseline.get("precision"),
            "recall": baseline.get("recall"),
        },
        "optimized": {
            "accuracy": optimized.get("accuracy"),
            "precision": optimized.get("precision"),
            "recall": optimized.get("recall"),
        },
    }

def tool_create_api_key(args):
    """Generate an API key for the endpoint."""
    name = args.get("name", "judge-endpoint")
    headers = pluto_headers()
    result = http_request("POST", f"{PLUTO_API}/api-keys",
        body={"name": name}, headers=headers)
    return {
        "api_key": result["secret"],
        "key_id": result["id"],
    }

# ── Tool registry ─────────────────────────────────────────────────────────

TOOLS = {
    "pluto_start_judge": {
        "fn": tool_start_judge,
        "description": "Start building an LLM-as-a-judge evaluator: creates a thread, sends the task to the Pluto agent, and returns refinement questions. This MUST be your first tool call.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Short name (2-5 words, e.g. 'health advice detection')"},
                "task_description": {"type": "string", "description": "1-2 sentences, max 150 chars. Include task + desired label names. No examples or criteria. Example: 'Classify responses as health_advice or safe.'"},
            },
            "required": ["name", "task_description"],
        },
    },
    "pluto_upload_data": {
        "fn": tool_upload_data,
        "description": "Upload labeled examples from a user-provided file. Only use when the user explicitly provides a data file path.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "example_set_id": {"type": "string", "description": "Example set ID from pluto_start_judge response"},
                "records": {
                    "type": "array",
                    "description": "Array of {sample, label, reasoning} objects read from the user's file",
                    "items": {
                        "type": "object",
                        "properties": {
                            "sample": {"type": "string"},
                            "label": {"type": "string"},
                            "reasoning": {"type": "string"},
                        },
                        "required": ["sample", "label"],
                    },
                },
                "file_name": {"type": "string", "description": "Original file name"},
            },
            "required": ["example_set_id", "records"],
        },
    },
    "pluto_send_message": {
        "fn": tool_send_message,
        "description": "Send a follow-up message to the Pluto agent. Only use AFTER pluto_start_judge. For: sending user answers, 'Optimize [LLM]', 'Optimize [SLM]'",
        "inputSchema": {
            "type": "object",
            "properties": {
                "thread_id": {"type": "string", "description": "Thread ID"},
                "message": {"type": "string", "description": "Message to send to the agent"},
            },
            "required": ["thread_id", "message"],
        },
    },
    "pluto_search_evaluators": {
        "fn": tool_search_evaluators,
        "description": "Search existing evaluators on the Pluto platform. Call this first to check if a relevant evaluator already exists before creating a new one.",
        "inputSchema": {
            "type": "object",
            "properties": {},
        },
    },
    "pluto_get_results": {
        "fn": tool_get_results,
        "description": "Get optimization results (accuracy, precision, recall) and endpoint URL",
        "inputSchema": {
            "type": "object",
            "properties": {
                "classifier_id": {"type": "string", "description": "Classifier ID from send_message response"},
            },
            "required": ["classifier_id"],
        },
    },
    "pluto_create_api_key": {
        "fn": tool_create_api_key,
        "description": "Generate an API key for the evaluator endpoint",
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Name for the API key"},
            },
        },
    },
    "pluto_ask_user": {
        "fn": tool_ask_user,
        "description": "Present questions to the user via interactive form UI. Use this to ask refinement questions, optimization choices, or any decision that needs user input. Each question can have selectable options.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "questions": {
                    "type": "array",
                    "description": "Array of questions to present",
                    "items": {
                        "type": "object",
                        "properties": {
                            "question": {"type": "string", "description": "The question text"},
                            "options": {
                                "type": "array",
                                "description": "Selectable options for this question",
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "label": {"type": "string", "description": "Display text for the option"},
                                        "value": {"type": "string", "description": "Value returned when selected"},
                                    },
                                    "required": ["label", "value"],
                                },
                            },
                        },
                        "required": ["question", "options"],
                    },
                },
            },
            "required": ["questions"],
        },
    },
}

# ── MCP message handler ───────────────────────────────────────────────────

def handle_message(msg):
    id = msg.get("id")
    method = msg.get("method")

    if method == "initialize":
        _log.debug("CLIENT INIT: %s", json.dumps(msg)[:1000])
        send_response(id, {
            "protocolVersion": "2025-03-26",
            "capabilities": {"tools": {"listChanged": False}, "elicitation": {}},
            "serverInfo": {"name": "pluto-judge", "version": "0.1.0"},
        })
    elif method == "notifications/initialized":
        pass  # no response needed
    elif method == "tools/list":
        tools = [
            {"name": name, "description": t["description"], "inputSchema": t["inputSchema"]}
            for name, t in TOOLS.items()
        ]
        send_response(id, {"tools": tools})
    elif method == "tools/call":
        tool_name = msg["params"]["name"]
        tool_args = msg["params"].get("arguments", {})
        if tool_name not in TOOLS:
            send_error(id, -32601, f"Unknown tool: {tool_name}")
            return
        try:
            result = TOOLS[tool_name]["fn"](tool_args)
            send_response(id, {"content": [{"type": "text", "text": json.dumps(result, indent=2)}]})
        except HTTPError as e:
            error_body = e.read().decode() if e.fp else str(e)
            send_error(id, -32000, f"HTTP {e.code}: {error_body}")
        except Exception as e:
            send_error(id, -32000, str(e))
    else:
        if id is not None:
            send_error(id, -32601, f"Unknown method: {method}")

# ── Main loop ─────────────────────────────────────────────────────────────

def main():
    while True:
        try:
            msg = read_message()
            if msg is None:
                break
            handle_message(msg)
        except Exception as e:
            _log.exception("Error handling message: %s", e)

if __name__ == "__main__":
    main()
