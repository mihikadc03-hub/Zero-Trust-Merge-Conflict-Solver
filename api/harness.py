"""
Zero-Trust Merge Conflict Resolver - Model Harness
FastAPI server that calls LM Studio native API at /api/v1/chat.
All inference is 100% offline - source code never leaves the machine.

Prerequisites:
  1. Open LM Studio -> load a model (e.g. qwen2.5-coder-3b-instruct)
  2. Go to "Local Server" tab -> Start Server
  3. Start this harness: uvicorn harness:app --reload --port 8000

Env vars (optional):
  LM_STUDIO_URL   default: http://localhost:1234
  MODEL_NAME      default: qwen2.5-coder-3b-instruct
"""

import os
import re
from contextlib import asynccontextmanager

import requests as http
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

# ---------------------------------------------------------------------------
# LM Studio config
# ---------------------------------------------------------------------------
LM_STUDIO_URL = os.environ.get("LM_STUDIO_URL", "http://localhost:1234")
MODEL_NAME    = os.environ.get("MODEL_NAME",    "qwen2.5-coder-3b-instruct")
LM_CHAT_URL   = f"{LM_STUDIO_URL}/api/v1/chat"


# ---------------------------------------------------------------------------
# Lifespan: verify LM Studio is reachable on startup
# ---------------------------------------------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    try:
        r = http.get(f"{LM_STUDIO_URL}/api/v1/models", timeout=3)
        r.raise_for_status()
        models = [m.get("id", m) for m in r.json().get("data", [])]
        print(f"\nLM Studio reachable at {LM_STUDIO_URL}")
        print(f"Models available: {models}\n")
    except Exception:
        print(
            f"\nWARNING: Cannot reach LM Studio at {LM_STUDIO_URL}.\n"
            "Make sure LM Studio is open and the Local Server is started.\n"
        )
    yield


app = FastAPI(
    title="Zero-Trust Merge Conflict Resolver",
    description="Resolves Git merge conflicts via LM Studio. No data leaves your machine.",
    version="1.0.0",
    lifespan=lifespan,
)


# ---------------------------------------------------------------------------
# Request / Response schemas
# ---------------------------------------------------------------------------
class ConflictRequest(BaseModel):
    filename: str
    before: str   # ~50 lines of context before the conflict block
    ours: str     # HEAD version of the conflicting lines
    theirs: str   # incoming branch version of the conflicting lines
    after: str    # ~50 lines of context after the conflict block


class RefineRequest(BaseModel):
    filename: str
    before: str
    ours: str
    theirs: str
    after: str
    previous_resolution: str  # what the model returned last time
    feedback: str             # developer instruction for the new attempt


class ResolveResponse(BaseModel):
    resolved: str


# ---------------------------------------------------------------------------
# Prompt engineering
# ---------------------------------------------------------------------------
SYSTEM_PROMPT = (
    "You are a precise, silent code merge engine. "
    "Your ONLY output is the minimum lines needed to replace the conflict block.\n"
    "STRICT RULES:\n"
    "- Output ONLY the replacement lines for the conflict block. Nothing else.\n"
    "- Output the MINIMUM code required - do NOT output surrounding functions, classes, or file content.\n"
    "- The conflict block is only the lines between <<<<<<< and >>>>>>>. Output only those replacement lines.\n"
    "- No explanations, no commentary, no apologies.\n"
    "- No markdown fences (no ```, no ```cpp, no ```python).\n"
    "- No conflict markers (<<<<<<<, =======, >>>>>>>).\n"
    "- Preserve original indentation exactly.\n"
    "- Do NOT output the surrounding function signature or closing braces unless they were part of the conflict."
)


def _resolve_input(req: ConflictRequest) -> str:
    return (
        f"File: {req.filename}\n\n"
        f"CONTEXT BEFORE the conflict (READ-ONLY, do NOT output these lines):\n{req.before}\n"
        f"OUR VERSION (HEAD) - these are the lines between <<<<<<< and =======:\n{req.ours}\n"
        f"THEIR VERSION (incoming) - these are the lines between ======= and >>>>>>>:\n{req.theirs}\n"
        f"CONTEXT AFTER the conflict (READ-ONLY, do NOT output these lines):\n{req.after}\n\n"
        "Output ONLY the lines that replace the conflict block (what was between <<<<<<< and >>>>>>>).\n"
        "Do NOT output any lines from the CONTEXT BEFORE or CONTEXT AFTER sections.\n"
        "No conflict markers. No prose. Just the replacement lines."
    )


def _refine_input(req: RefineRequest) -> str:
    return (
        f"File: {req.filename}\n\n"
        f"CONTEXT BEFORE (do NOT output this):\n{req.before}\n"
        f"CONTEXT AFTER (do NOT output this):\n{req.after}\n\n"
        f"CURRENT CODE (this is YOUR previous resolution - edit this, do NOT revert to the original conflict):\n"
        f"{req.previous_resolution}\n\n"
        f"DEVELOPER FEEDBACK: {req.feedback}\n\n"
        "Apply the developer feedback to the CURRENT CODE above. "
        "Output ONLY the updated code block. No markers. No prose."
    )


# ---------------------------------------------------------------------------
# LM Studio native API call + sanitisation
# ---------------------------------------------------------------------------
def _sanitize(text: str) -> str:
    """Remove markdown fences or stray conflict markers the model might produce."""
    text = text.strip()
    # Strip ```language ... ``` blocks
    text = re.sub(r"^```[a-zA-Z]*\r?\n?", "", text)
    text = re.sub(r"\r?\n?```\s*$", "", text)
    # Drop stray conflict marker lines
    clean = [
        ln for ln in text.splitlines()
        if not ln.startswith(("<<<<<<< ", "=======", ">>>>>>> "))
    ]
    return "\n".join(clean).strip()


def _call_lm_studio(system_prompt: str, user_input: str) -> str:
    """
    Calls LM Studio native API:
      POST /api/v1/chat
      { "model": "...", "system_prompt": "...", "input": "..." }
    """
    payload = {
        "model":         MODEL_NAME,
        "system_prompt": system_prompt,
        "input":         user_input,
    }

    try:
        response = http.post(
            LM_CHAT_URL,
            json=payload,
            headers={"Content-Type": "application/json"},
            timeout=180,
        )
        response.raise_for_status()
    except http.exceptions.ConnectionError:
        raise HTTPException(
            status_code=503,
            detail=(
                f"Cannot reach LM Studio at {LM_STUDIO_URL}. "
                "Is the Local Server running?"
            ),
        )
    except http.exceptions.HTTPError as e:
        raise HTTPException(status_code=502, detail=f"LM Studio error: {e}")

    data = response.json()

    # LM Studio REST API v1: output is a list of {"type": "message", "content": "..."}
    raw = ""
    output = data.get("output")
    if isinstance(output, list):
        # Find the first message-type item
        for item in output:
            if isinstance(item, dict) and item.get("type") == "message":
                raw = item.get("content", "")
                break
    elif isinstance(output, str):
        raw = output
    else:
        # Fallback: OpenAI-compatible format
        raw = (
            data.get("choices", [{}])[0]
                .get("message", {})
                .get("content", "")
        )

    if not raw:
        raise HTTPException(
            status_code=502,
            detail=f"Could not extract content from LM Studio response: {data}",
        )

    return _sanitize(raw)


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------
@app.post("/resolve", response_model=ResolveResponse, summary="Initial AI resolution")
def resolve_conflict(req: ConflictRequest):
    """
    First-pass resolution.
    Sends the conflict context to the local LM Studio model and returns resolved code.
    """
    resolved = _call_lm_studio(SYSTEM_PROMPT, _resolve_input(req))
    return ResolveResponse(resolved=resolved)


@app.post("/refine", response_model=ResolveResponse, summary="Refine with developer feedback")
def refine_resolution(req: RefineRequest):
    """
    Regenerates the resolution based on the previous attempt + developer feedback.
    Called when the developer types [F] in the interactive CLI.
    """
    resolved = _call_lm_studio(SYSTEM_PROMPT, _refine_input(req))
    return ResolveResponse(resolved=resolved)


@app.get("/health", summary="Health check")
def health():
    """Check if the harness is up and LM Studio is reachable."""
    reachable = False
    models = []
    try:
        r = http.get(f"{LM_STUDIO_URL}/api/v1/models", timeout=3)
        r.raise_for_status()
        models = [m.get("id", m) for m in r.json().get("data", [])]
        reachable = True
    except Exception:
        pass
    return {
        "status": "ok",
        "lm_studio_url": LM_STUDIO_URL,
        "lm_studio_reachable": reachable,
        "model": MODEL_NAME,
        "available_models": models,
    }


