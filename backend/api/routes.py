# backend/api/routes.py

import logging
import uuid
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from fastapi import File, UploadFile
from backend.services.cloudinary_service import upload_image, delete_image

from backend.agent.loop import run_agent
from backend.agent.schemas import ChatRequest, ChatResponse, Message, Role
from backend.api.dependencies import (
    get_current_user,
    get_groq,
    get_policy,
    get_conversations,
    get_tools,
)
from backend.policies.file_store import FilePolicyStore
from backend.services.llm_base import LLMBase
from backend.services.conversation_store import ConversationStore
from backend.tools.base import BaseTool
from backend.database import get_db
from motor.motor_asyncio import AsyncIOMotorDatabase
from bson import ObjectId

logger = logging.getLogger(__name__)
router = APIRouter()


class ChatInput(BaseModel):
    """What the frontend sends — no email needed, comes from JWT."""
    message:    str = Field(..., min_length=1, max_length=2000)
    session_id: str
    order_id:   str | None = None


@router.post("/chat", response_model=ChatResponse)
async def chat(
    body:          ChatInput,
    current_user:  dict              = Depends(get_current_user),
    llm:           LLMBase           = Depends(get_groq),
    policy:        FilePolicyStore   = Depends(get_policy),
    conversations: ConversationStore = Depends(get_conversations),
    db:            AsyncIOMotorDatabase = Depends(get_db),
    tools:         list[BaseTool]    = Depends(get_tools),
):
    try:
        # Ensure conversation document exists for this session
        conv = await conversations.get_or_create(
            session_id = body.session_id,
            user_id    = str(current_user["_id"]),
        )

        request = ChatRequest(
            message    = body.message,
            session_id = body.session_id,
            user_email = current_user.get("email"),
            order_id   = body.order_id,
        )

        # ── Reconstruct full history including tool messages ─────────────────
        #
        # Each turn in the DB is stored as an ordered sequence:
        #   1. {role: "user",      content: "..."}
        #   2. {role: "assistant", content: "__tool_calls__:[...]"}  ← tool decision
        #   3. {role: "tool",      content: "...", tool_call_id: "...", name: "..."}
        #   4. {role: "assistant", content: "final reply text"}
        #
        # groq_service._build_messages() knows how to decode entries 2 and 3
        # back into the proper Groq API format. We must not drop them.
        #
        # Roles we skip:
        #   "notification" — admin push messages, not part of LLM conversation
        #
        history: list[Message] = []
        for m in conv.get("messages", []):
            role_str = m.get("role", "")

            if role_str == "notification":
                continue

            try:
                role = Role(role_str)
            except ValueError:
                # Unknown role — skip gracefully rather than crashing
                logger.warning(f"Unknown message role in history: '{role_str}' — skipping")
                continue

            history.append(Message(
                role         = role,
                content      = m["content"],
                tool_call_id = m.get("tool_call_id"),   # only set on role=tool messages
                name         = m.get("name"),            # only set on role=tool messages
            ))

        response = await run_agent(
            request      = request,
            llm          = llm,
            policy_store = policy,
            tools        = tools,
            history      = history,
        )

        # Link the pending_request to this session if a date-change was made.
        # (Allows admin approval to push a WebSocket notification to the right customer.)
        TOOLS_NEEDING_SESSION = {"change_delivery_date", "initiate_return", "change_order_item"}

        if db is not None and any(
            (tc.tool_name in TOOLS_NEEDING_SESSION) or
            (tc.tool_name == "tool_invoke" and tc.arguments.get("tool_id") in TOOLS_NEEDING_SESSION)
            for tc in response.tool_calls
        ):
            from pymongo import DESCENDING
            await db.pending_requests.find_one_and_update(
                {
                    "user_id":    ObjectId(str(current_user["_id"])),
                    "status":     "pending",
                    "session_id": None,
                },
                {"$set": {"session_id": body.session_id}},
                sort=[(("created_at", DESCENDING))],
            )

        # ── Persist the full turn (user + tool sequence + reply) ────────────
        await conversations.append_turn(
            session_id   = body.session_id,
            user_message = body.message,
            bot_reply    = response.message,
            tool_calls   = response.tool_calls,
            tool_results = response.tool_results,   # was missing before — now passed
        )

        return ChatResponse(
            reply         = response.message,
            session_id    = body.session_id,
            was_escalated = response.was_escalated,
        )

    except Exception as e:
        logger.exception(f"Chat failed — session={body.session_id}")
        raise HTTPException(status_code=500, detail="Something went wrong.")


@router.get("/conversations")
async def get_conversations_history(
    current_user:  dict              = Depends(get_current_user),
    conversations: ConversationStore = Depends(get_conversations),
):
    """
    Returns last 5 conversations for the logged-in user.
    Called when the frontend loads after login.
    """
    history = await conversations.get_history(
        user_id = str(current_user["_id"]),
        limit   = 20,
    )
    return {"conversations": history}


@router.post("/conversations/close")
async def close_conversation(
    body:          dict              = {},
    current_user:  dict              = Depends(get_current_user),
    conversations: ConversationStore = Depends(get_conversations),
):
    """Called when user logs out to mark session as closed."""
    session_id = body.get("session_id")
    if session_id:
        await conversations.close_session(session_id)
    return {"status": "closed"}


@router.get("/session/new")
async def new_session():
    return {"session_id": str(uuid.uuid4())}


@router.get("/health/deep")
async def deep_health(
    llm:    LLMBase        = Depends(get_groq),
    policy: FilePolicyStore = Depends(get_policy),
):
    return {
        "status":       "ok",
        "llm":          llm.__class__.__name__,
        "policy_store": policy.__class__.__name__,
    }

# ── Defect Image Upload ───────────────────────────────────────────────────────
 
@router.post("/defect-image/upload")
async def upload_defect_image(
    pending_request_id: str,
    file:               UploadFile        = File(...),
    current_user:       dict              = Depends(get_current_user),
    db:                 AsyncIOMotorDatabase = Depends(get_db),
):
    """
    Upload a defect photo for a return request to Cloudinary.
 
    Rules (mirrors the existing tool ownership checks in mongo_tools.py):
    - pending_request must belong to the authenticated user
    - pending_request must be type "return_request" and status "pending"
    - file must be an image (jpeg / png / webp) and ≤ 10 MB
 
    On success: stores cloudinary_url + cloudinary_public_id on the
    pending_request document and returns the URL to the frontend.
    """
    # ── Guard: DB must be Mongo (this feature uses the pending_requests collection) ──
    if db is None:
        raise HTTPException(status_code=503, detail="Feature not available in this DB mode.")
 
    # ── Validate file type ────────────────────────────────────────────────────
    ALLOWED_TYPES = {"image/jpeg", "image/png", "image/webp"}
    MAX_SIZE_BYTES = 10 * 1024 * 1024  # 10 MB
 
    if file.content_type not in ALLOWED_TYPES:
        raise HTTPException(
            status_code=400,
            detail="Invalid file type. Only JPEG, PNG, and WebP images are allowed."
        )
 
    file_bytes = await file.read()
    if len(file_bytes) > MAX_SIZE_BYTES:
        raise HTTPException(
            status_code=400,
            detail="File too large. Maximum size is 10 MB."
        )
 
    # ── Verify ownership and eligibility ─────────────────────────────────────
    try:
        req_oid = ObjectId(pending_request_id)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid pending_request_id.")
 
    req = await db.pending_requests.find_one({"_id": req_oid})
 
    if not req:
        raise HTTPException(status_code=404, detail="Return request not found.")
 
    # Ownership check — same pattern used in mongo_tools.py initiate_return
    if str(req.get("user_id")) != str(current_user["_id"]):
        raise HTTPException(status_code=403, detail="Access denied.")
 
    if req.get("type") != "return_request":
        raise HTTPException(
            status_code=400,
            detail="Defect photos can only be attached to return requests."
        )
 
    if req.get("status") != "pending":
        raise HTTPException(
            status_code=400,
            detail="Cannot upload photos for a request that is no longer pending."
        )
 
    # ── Upload to Cloudinary ──────────────────────────────────────────────────
    # Use pending_request_id as the public_id so we can always find and delete it.
    # Cloudinary folder: leafy/defect_photos/{user_id}/{pending_request_id}
    try:
        cloud_result = await upload_image(
            file_bytes = file_bytes,
            public_id  = pending_request_id,
            folder     = f"leafy/defect_photos/{str(current_user['_id'])}",
        )
    except Exception as e:
        logger.exception(f"Cloudinary upload failed — request={pending_request_id}")
        raise HTTPException(status_code=502, detail="Image upload failed. Please try again.")
 
    # ── Persist URLs on the pending_request document ──────────────────────────
    await db.pending_requests.update_one(
        {"_id": req_oid},
        {"$set": {
            "cloudinary_url":       cloud_result["secure_url"],
            "cloudinary_public_id": cloud_result["public_id"],
        }}
    )
 
    logger.info(
        f"Defect image uploaded — request={pending_request_id} "
        f"user={current_user.get('email')} url={cloud_result['secure_url']}"
    )
 
    return {
        "url":       cloud_result["secure_url"],
        "public_id": cloud_result["public_id"],
    }
 
 
# ── Defect Image Delete ───────────────────────────────────────────────────────
 
@router.delete("/defect-image/{pending_request_id}")
async def delete_defect_image(
    pending_request_id: str,
    current_user:       dict              = Depends(get_current_user),
    db:                 AsyncIOMotorDatabase = Depends(get_db),
):
    """
    Remove a defect photo from Cloudinary and clear the URL from the
    pending_request document.
 
    Called when the customer presses × on their uploaded image preview.
    Silently succeeds even if Cloudinary delete fails (image may already be gone).
    """
    if db is None:
        raise HTTPException(status_code=503, detail="Feature not available in this DB mode.")
 
    try:
        req_oid = ObjectId(pending_request_id)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid pending_request_id.")
 
    req = await db.pending_requests.find_one(
        {"_id": req_oid},
        {"user_id": 1, "cloudinary_public_id": 1, "status": 1}
    )
 
    if not req:
        raise HTTPException(status_code=404, detail="Return request not found.")
 
    if str(req.get("user_id")) != str(current_user["_id"]):
        raise HTTPException(status_code=403, detail="Access denied.")
 
    if req.get("status") != "pending":
        raise HTTPException(
            status_code=400,
            detail="Cannot modify photos for a request that is no longer pending."
        )
 
    public_id = req.get("cloudinary_public_id")
 
    # ── Delete from Cloudinary (best-effort — doesn't raise on failure) ───────
    if public_id:
        await delete_image(public_id)
 
    # ── Clear from DB ─────────────────────────────────────────────────────────
    await db.pending_requests.update_one(
        {"_id": req_oid},
        {"$unset": {
            "cloudinary_url":       "",
            "cloudinary_public_id": "",
        }}
    )
 
    logger.info(
        f"Defect image deleted — request={pending_request_id} "
        f"user={current_user.get('email')}"
    )
 
    return {"status": "deleted"}

@router.get("/defect-image/pending-id")
async def get_pending_return_id(
    session_id:   str,
    current_user: dict              = Depends(get_current_user),
    db:           AsyncIOMotorDatabase = Depends(get_db),
):
    """
    Return the _id of the most recent pending return_request for the current user
    that is linked to this session.
 
    Called by ChatWindow.jsx right after the agent confirms a return so the
    frontend has the ID it needs for upload/delete calls.
    """
    if db is None:
        raise HTTPException(status_code=503, detail="Feature not available in this DB mode.")
 
    req = await db.pending_requests.find_one(
        {
            "user_id":    ObjectId(str(current_user["_id"])),
            "type":       "return_request",
            "status":     "pending",
            "session_id": session_id,
        },
        sort=[("created_at", -1)],   # most recent first
    )
 
    if not req:
        raise HTTPException(status_code=404, detail="No pending return request found for this session.")
 
    return {"pending_request_id": str(req["_id"])}