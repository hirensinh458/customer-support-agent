// frontend/src/components/ChatWindow.jsx
// REPLACES the existing file — all original logic is untouched.
// New additions are marked with ── NEW ── comments.

import { useState, useRef, useEffect, useCallback } from "react";
import { MessageBubble }   from "./MessageBubble";
import { TypingIndicator } from "./TypingIndicator";
import { uploadDefectImage, deleteDefectImage } from "../api";  // ── NEW ──

const SUGGESTIONS = [
  "Where is my order?",
  "I'd like to return an item",
  "Change my delivery date",
  "Check my account details",
];

// ── NEW: detect if a bot reply is a return-submitted confirmation ─────────────
// The agent always says "return request has been submitted" (from agent/loop.py
// APPROVAL WORKFLOW instructions). We key off that phrase to know when to show
// the photo upload UI.
function isReturnSubmittedReply(text = "") {
  const lower = text.toLowerCase();
  return (
    lower.includes("return request") &&
    (lower.includes("submitted") || lower.includes("pending"))
  );
}

// ── NEW: extract pending_request_id from bot reply ────────────────────────────
// The agent reply doesn't contain the DB id, so we fetch it from the backend
// via a dedicated lightweight endpoint (see routes_additions.py).
// We store it in state once found so the upload/delete calls have it.

export function ChatWindow({ user, messages, loading, onSend, sessionId }) {
  const [input, setInput]   = useState("");
  const bottomRef           = useRef(null);
  const inputRef            = useRef(null);
  const messagesRef         = useRef(null);

  // ── NEW: defect photo state ────────────────────────────────────────────────
  const fileInputRef              = useRef(null);
  const [pendingRequestId, setPendingRequestId]   = useState(null);  // set when return confirmed
  const [photoPreview,     setPhotoPreview]       = useState(null);  // local object URL
  const [photoPublicId,    setPhotoPublicId]       = useState(null);  // cloudinary public_id
  const [photoUploading,   setPhotoUploading]     = useState(false);
  const [photoError,       setPhotoError]         = useState(null);
  const [showPhotoBtn,     setShowPhotoBtn]       = useState(false);  // shows + button

  // ── NEW: watch messages for a return-confirmed reply ──────────────────────
  // When the agent confirms a return request, show the photo upload button.
  // We also call the backend to get the pending_request_id for this session.
  useEffect(() => {
    const lastBotMsg = [...messages]
      .reverse()
      .find((m) => m.role === "assistant" && !m.isError);

    if (lastBotMsg && isReturnSubmittedReply(lastBotMsg.content)) {
      setShowPhotoBtn(true);
      // Fetch the pending_request_id for this session from the backend.
      // The backend finds the most recent pending return_request for this user.
      if (sessionId) {
        fetch(`/api/defect-image/pending-id?session_id=${encodeURIComponent(sessionId)}`, {
          headers: {
            Authorization: `Bearer ${localStorage.getItem("leafy_token")}`,
          },
        })
          .then((r) => r.ok ? r.json() : null)
          .then((data) => {
            if (data?.pending_request_id) {
              setPendingRequestId(data.pending_request_id);
            }
          })
          .catch(() => {});  // non-fatal — upload button stays, upload will fail gracefully
      }
    }
  }, [messages, sessionId]);

  // ── NEW: reset photo state when session changes ────────────────────────────
  useEffect(() => {
    setShowPhotoBtn(false);
    setPendingRequestId(null);
    setPhotoPreview(null);
    setPhotoPublicId(null);
    setPhotoError(null);
    setPhotoUploading(false);
  }, [sessionId]);

  // ── NEW: handle file selected from the hidden <input type="file"> ─────────
  const handleFileSelected = useCallback(async (e) => {
    const file = e.target.files?.[0];
    if (!fileInputRef.current) return;
    fileInputRef.current.value = "";  // reset so same file can be re-selected

    if (!file) return;

    // Client-side guards (backend also validates, this is just UX feedback)
    const ALLOWED = ["image/jpeg", "image/png", "image/webp"];
    if (!ALLOWED.includes(file.type)) {
      setPhotoError("Only JPEG, PNG, or WebP images are allowed.");
      return;
    }
    if (file.size > 10 * 1024 * 1024) {
      setPhotoError("Image must be under 10 MB.");
      return;
    }

    // Show a local preview immediately while uploading
    const objectUrl = URL.createObjectURL(file);
    setPhotoPreview(objectUrl);
    setPhotoError(null);
    setPhotoUploading(true);

    try {
      const result = await uploadDefectImage({
        pendingRequestId,
        file,
      });
      setPhotoPublicId(result.public_id);
    } catch (err) {
      // Upload failed — clear the preview and show error
      URL.revokeObjectURL(objectUrl);
      setPhotoPreview(null);
      setPhotoPublicId(null);
      setPhotoError(err.message || "Upload failed. Please try again.");
    } finally {
      setPhotoUploading(false);
    }
  }, [pendingRequestId]);

  // ── NEW: handle × button on image preview ─────────────────────────────────
  const handleRemovePhoto = useCallback(async () => {
    // Revoke the local object URL to free memory
    if (photoPreview) URL.revokeObjectURL(photoPreview);
    setPhotoPreview(null);
    setPhotoError(null);

    // If already uploaded to Cloudinary, delete it
    if (photoPublicId && pendingRequestId) {
      try {
        await deleteDefectImage({ pendingRequestId });
      } catch (_) {
        // Best-effort — don't surface this error to the user
      }
    }
    setPhotoPublicId(null);
  }, [photoPreview, photoPublicId, pendingRequestId]);

  // ── original: auto-scroll ─────────────────────────────────────────────────
  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages, loading]);

  // ── original: re-focus on session change ──────────────────────────────────
  useEffect(() => {
    if (!sessionId) return;
    const t = setTimeout(() => {
      inputRef.current?.focus();
      bottomRef.current?.scrollIntoView({ behavior: "instant" });
    }, 50);
    return () => clearTimeout(t);
  }, [sessionId]);

  // ── original: auto-grow textarea ──────────────────────────────────────────
  const handleInputChange = (e) => {
    const el = e.target;
    setInput(el.value);
    el.style.height = "auto";
    el.style.height = Math.min(el.scrollHeight, 120) + "px";
  };

  const handleSend = () => {
    const text = input.trim();
    if (!text || loading) return;
    setInput("");
    if (inputRef.current) {
      inputRef.current.style.height = "auto";
    }
    onSend(text);
  };

  const handleKey = (e) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      handleSend();
    }
  };

  const handleSuggestion = (s) => {
    if (loading) return;
    onSend(s);
    inputRef.current?.focus();
  };

  const isEmpty = messages.length === 0;

  return (
    <div className="chat-window">

      {/* ── Header (unchanged) ── */}
      <div className="chat-header">
        <div className="chat-header__brand">
          <div className="chat-header__avatar">
            <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.75">
              <path d="M12 2a5 5 0 1 0 0 10A5 5 0 0 0 12 2zM2 20c0-4 4.5-7 10-7s10 3 10 7"/>
            </svg>
            <span className="chat-header__dot" />
          </div>
          <div>
            <p className="chat-header__name">Support Agent</p>
            <p className="chat-header__status">Online &middot; replies instantly</p>
          </div>
        </div>
      </div>

      {/* ── Messages (unchanged) ── */}
      <div className="chat-messages" ref={messagesRef}>

        {isEmpty && (
          <div className="chat-empty">
            <div className="chat-empty__icon">
              <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.75">
                <path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/>
              </svg>
            </div>
            <h2 className="chat-empty__title">
              Hi {user?.name || "there"} 👋
            </h2>
            <p className="chat-empty__sub">
              How can I help you today?<br />
              Ask me anything about your orders or account.
            </p>
            <div className="suggestions">
              {SUGGESTIONS.map((s) => (
                <button
                  key={s}
                  className="suggestion-chip"
                  onClick={() => handleSuggestion(s)}
                  disabled={loading}
                >
                  {s}
                </button>
              ))}
            </div>
          </div>
        )}

        {messages.map((msg) => (
          <MessageBubble key={msg.id} message={msg} />
        ))}

        {loading && <TypingIndicator />}
        <div ref={bottomRef} />
      </div>

      {/* ── Input ── */}
      <div className="chat-input-area">

        {/* ── NEW: image preview strip (shown above input row when photo is selected) ── */}
        {photoPreview && (
          <div className="photo-preview-strip">
            <div className="photo-preview-wrap">
              <img
                src={photoPreview}
                alt="Defect photo preview"
                className="photo-preview-thumb"
              />
              {/* Uploading spinner overlay */}
              {photoUploading && (
                <div className="photo-preview-overlay">
                  <span className="photo-spinner" />
                </div>
              )}
              {/* × remove button */}
              {!photoUploading && (
                <button
                  className="photo-remove-btn"
                  onClick={handleRemovePhoto}
                  title="Remove photo"
                >
                  <svg width="10" height="10" viewBox="0 0 10 10" fill="none">
                    <path d="M1 1l8 8M9 1L1 9" stroke="currentColor" strokeWidth="1.75" strokeLinecap="round"/>
                  </svg>
                </button>
              )}
              {/* Uploaded checkmark */}
              {!photoUploading && photoPublicId && (
                <div className="photo-uploaded-badge" title="Uploaded">
                  <svg width="9" height="9" viewBox="0 0 9 9" fill="none">
                    <path d="M1.5 4.5L3.5 6.5L7.5 2.5" stroke="currentColor" strokeWidth="1.75" strokeLinecap="round" strokeLinejoin="round"/>
                  </svg>
                </div>
              )}
            </div>
            <p className="photo-preview-label">
              {photoUploading
                ? "Uploading…"
                : photoPublicId
                  ? "Photo attached to your return request"
                  : "Processing…"}
            </p>
          </div>
        )}

        {/* ── NEW: photo error message ── */}
        {photoError && (
          <p className="photo-error">{photoError}</p>
        )}

        <div className="input-row">

          {/* ── NEW: + button (shown only after return is submitted) ── */}
          {showPhotoBtn && (
            <>
              {/* Hidden real file input */}
              <input
                ref={fileInputRef}
                type="file"
                accept="image/jpeg,image/png,image/webp"
                style={{ display: "none" }}
                onChange={handleFileSelected}
              />
              <button
                className="attach-btn"
                onClick={() => fileInputRef.current?.click()}
                disabled={loading || photoUploading || !!photoPreview}
                title="Attach defect photo"
              >
                <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5">
                  <line x1="12" y1="5" x2="12" y2="19"/>
                  <line x1="5"  y1="12" x2="19" y2="12"/>
                </svg>
              </button>
            </>
          )}

          {/* ── original textarea (unchanged) ── */}
          <textarea
            ref={inputRef}
            className="chat-input"
            placeholder={loading ? "Waiting for reply…" : "Type a message…"}
            value={input}
            onChange={handleInputChange}
            onKeyDown={handleKey}
            rows={1}
            disabled={loading}
          />

          {/* ── original send button (unchanged) ── */}
          <button
            className={`send-btn ${input.trim() && !loading ? "send-btn--active" : ""}`}
            onClick={handleSend}
            disabled={!input.trim() || loading}
            title="Send message"
          >
            <svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5">
              <line x1="22" y1="2" x2="11" y2="13"/>
              <polygon points="22 2 15 22 11 13 2 9 22 2"/>
            </svg>
          </button>
        </div>

        {/* ── NEW: helper text shown once the + button appears ── */}
        {showPhotoBtn && !photoPreview && (
          <p className="photo-hint">
            Tap <strong>+</strong> to attach a photo of the defective item (optional)
          </p>
        )}

      </div>
    </div>
  );
}