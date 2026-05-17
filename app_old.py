"""
Lufyx — Streamlit chatbot
Architecture notes
──────────────────
• st.chat_input() is HIDDEN via CSS; all input comes from a custom fixed
  toolbar built with st.text_input + st.button inside a position:fixed div.
• Active image is stored in session_state.active_image = {name, b64} and
  persists across follow-up messages.  It is cleared only on New Chat or
  when a different image is uploaded.
• File-uploader re-fire is guarded by session_state.last_uploaded_name.
• Text box is cleared after send by deleting its session_state key + rerun.
"""
import base64
import io
import os
import base64
from datetime import datetime

import fitz
import streamlit as st
from dotenv import load_dotenv
from groq import Groq
from streamlit_mic_recorder import mic_recorder
from tavily import TavilyClient

from database import conn, cursor

# ══════════════════════════════════════════════════════════════════════════════
# BOOT
# ══════════════════════════════════════════════════════════════════════════════
st.set_page_config(page_title="Lufyx", page_icon="🏴‍☠️", layout="wide")
load_dotenv()

groq_client = Groq(api_key=os.getenv("GROQ_API_KEY"))
tavily      = TavilyClient(api_key=os.getenv("TAVILY_API_KEY"))

# ══════════════════════════════════════════════════════════════════════════════
# DATABASE
# ══════════════════════════════════════════════════════════════════════════════
def _init_db():
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS chat_sessions (
            id       INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL,
            title    TEXT NOT NULL,
            created  TEXT NOT NULL
        )""")
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS chat_messages (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id INTEGER NOT NULL,
            role       TEXT NOT NULL,
            content    TEXT NOT NULL,
            msg_type   TEXT NOT NULL DEFAULT 'text',
            created    TEXT NOT NULL,
            FOREIGN KEY (session_id) REFERENCES chat_sessions(id)
        )""")
    try:
        cursor.execute(
            "ALTER TABLE chat_messages ADD COLUMN msg_type TEXT NOT NULL DEFAULT 'text'"
        )
        conn.commit()
    except Exception:
        pass
    conn.commit()

_init_db()


def db_create_session(username: str, title: str = "New Chat") -> int:
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    cursor.execute(
        "INSERT INTO chat_sessions (username, title, created) VALUES (?,?,?)",
        (username, title, now),
    )
    conn.commit()
    return cursor.lastrowid


def db_save_message(session_id: int, role: str, content: str, msg_type: str = "text"):
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    cursor.execute(
        "INSERT INTO chat_messages (session_id, role, content, msg_type, created)"
        " VALUES (?,?,?,?,?)",
        (session_id, role, content, msg_type, now),
    )
    conn.commit()


def db_load_messages(session_id: int) -> list[dict]:
    cursor.execute(
        "SELECT role, content, msg_type FROM chat_messages"
        " WHERE session_id=? ORDER BY id ASC",
        (session_id,),
    )
    return [{"role": r, "content": c, "type": t} for r, c, t in cursor.fetchall()]


def db_get_sessions(username: str) -> list[tuple]:
    cursor.execute(
        "SELECT id, title, created FROM chat_sessions"
        " WHERE username=? ORDER BY id DESC",
        (username,),
    )
    return cursor.fetchall()


def db_update_title(session_id: int, title: str):
    cursor.execute(
        "UPDATE chat_sessions SET title=? WHERE id=?", (title[:40], session_id)
    )
    conn.commit()


def db_delete_session(session_id: int):
    cursor.execute("DELETE FROM chat_messages WHERE session_id=?", (session_id,))
    cursor.execute("DELETE FROM chat_sessions WHERE id=?",         (session_id,))
    conn.commit()


# ══════════════════════════════════════════════════════════════════════════════
# FILE UTILITIES
# ══════════════════════════════════════════════════════════════════════════════
def pdf_to_text(raw: bytes) -> str:
    doc = fitz.open(stream=raw, filetype="pdf")
    return "".join(p.get_text() for p in doc).strip()


def to_data_uri(raw: bytes, mime: str) -> str:
    return f"data:{mime};base64,{base64.b64encode(raw).decode()}"


# ══════════════════════════════════════════════════════════════════════════════
# SESSION-STATE INITIALISATION
# ══════════════════════════════════════════════════════════════════════════════
_WELCOME = {"role": "assistant",
            "content": "Yo! 👊 I'm Lufyx — your nakama AI. Ask me anything!",
            "type": "text"}

DEFAULTS = {
    # auth
    "logged_in":  False,
    "username":   "",
    "auth_view":  None,
    # chat
    "messages":    [_WELCOME],
    "session_id":  None,
    # persistent image context — survives follow-up questions
    "active_image": None,        # {name: str, b64: str} | None
    # persistent PDF context
    "pdf_context": "",
    "pdf_name":    "",
    # pending uploads (staged before send)
    "pending_image": None,       # {name, b64} | None
    "pending_pdf":   None,       # {name, text} | None
    # file-uploader duplicate guard
    "last_uploaded_name": None,
    # voice transcription
    "voice_prefill": "",
    # toolbar input key version (incremented to reset st.text_input)
    "input_version": 0,
    # set True after voice transcribe so pipeline auto-fires without button click
    "send_triggered": False,
}

for k, v in DEFAULTS.items():
    if k not in st.session_state:
        st.session_state[k] = v

# ── Restore login from query-param after page refresh ─────────────────────────
if not st.session_state.logged_in:
    qp_user = st.query_params.get("user", "")
    if qp_user:
        cursor.execute("SELECT username FROM users WHERE username=?", (qp_user,))
        if cursor.fetchone():
            st.session_state.logged_in = True
            st.session_state.username  = qp_user
            sessions = db_get_sessions(qp_user)
            if sessions:
                sid = sessions[0][0]
                st.session_state.session_id = sid
                st.session_state.messages   = db_load_messages(sid)
            else:
                sid = db_create_session(qp_user)
                st.session_state.session_id = sid
                st.session_state.messages   = [
                    {"role": "assistant",
                     "content": f"Welcome back, {qp_user}! 👋",
                     "type": "text"}
                ]


# ══════════════════════════════════════════════════════════════════════════════
# HELPERS — new chat reset
# ══════════════════════════════════════════════════════════════════════════════
def _reset_chat(username: str):
    """Create a fresh session and wipe all chat + image state."""
    sid = db_create_session(username)
    st.session_state.update({
        "session_id":         sid,
        "messages":           [_WELCOME],
        "active_image":       None,   # ← clears image context
        "pdf_context":        "",
        "pdf_name":           "",
        "pending_image":      None,
        "pending_pdf":        None,
        "last_uploaded_name": None,
        "voice_prefill":      "",
        "input_version":      st.session_state.input_version + 1,
    })


# ══════════════════════════════════════════════════════════════════════════════
# CSS
# ══════════════════════════════════════════════════════════════════════════════
st.markdown("""
<style>
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&display=swap');

/* ═══════════════════════════════════════════════════
   LUFYX — Dark Cinematic Anime Theme
   Palette: deep navy bg, warm orange/red accent
   ═══════════════════════════════════════════════════ */

/* ── Root variables ── */
:root {
    --bg-deep:      #0d0f14;
    --bg-panel:     #13161e;
    --bg-glass:     rgba(255,255,255,0.035);
    --bg-glass2:    rgba(255,255,255,0.06);
    --accent:       #e8472a;
    --accent-warm:  #f5693b;
    --accent-glow:  rgba(232,71,42,0.25);
    --accent-dim:   rgba(232,71,42,0.12);
    --gold:         #d4a84b;
    --text-primary: #f0ece4;
    --text-muted:   rgba(240,236,228,0.50);
    --border:       rgba(255,255,255,0.07);
    --border-warm:  rgba(232,71,42,0.22);
    --radius-md:    14px;
    --radius-lg:    20px;
    --sidebar-w:    268px;
}

/* ── Global reset & font ── */
html, body, [class*="css"] {
    font-family: 'Inter', sans-serif !important;
}

/* ── Page background ── */
.stApp {
    background: var(--bg-deep) !important;
}

/* Subtle radial glow from bottom-right (warmth) */
.stApp::before {
    content: "";
    position: fixed;
    inset: 0;
    background:
        radial-gradient(ellipse 70% 55% at 92% 88%, rgba(232,71,42,0.07) 0%, transparent 70%),
        radial-gradient(ellipse 50% 40% at 8% 10%, rgba(212,168,75,0.04) 0%, transparent 60%);
    pointer-events: none;
    z-index: 0;
}

/* ── Main content area ── */
.block-container {
    padding-top: 1.4rem !important;
    padding-bottom: 110px !important;
    max-width: 100% !important;
    position: relative;
    z-index: 1;
}

/* ── Hide default Streamlit chrome ── */
#MainMenu, footer, header { display: none !important; }
.stChatFloatingInputContainer { display: none !important; }
h1 { margin-bottom: 0; }

/* ═══════════════════════════════════════════════════
   LUFYX TITLE
   ═══════════════════════════════════════════════════ */
.lufyx-title {
    text-align: center;
    font-size: 2rem !important;
    font-weight: 700 !important;
    letter-spacing: -0.5px;
    margin: 0 0 2px 0 !important;
    line-height: 1.2;
}
.t-lufy {
    color: var(--text-primary);
}
.t-x {
    color: var(--accent);
}
.guest-caption {
    text-align: center;
    font-size: 0.78rem;
    color: var(--text-muted);
    margin: 0 0 12px 0;
}

/* ═══════════════════════════════════════════════════
   SIDEBAR
   ═══════════════════════════════════════════════════ */
section[data-testid="stSidebar"] {
    min-width: var(--sidebar-w) !important;
    max-width: var(--sidebar-w) !important;
    background: var(--bg-panel) !important;
    border-right: 1px solid var(--border) !important;
}
section[data-testid="stSidebar"] > div {
    background: transparent !important;
    padding: 18px 14px 16px !important;
}

/* Sidebar logo */
.sidebar-logo {
    font-size: 1.45rem;
    font-weight: 700;
    letter-spacing: -0.3px;
    margin-bottom: 2px;
    padding: 4px 0 8px 2px;
}
.logo-lufy { color: var(--text-primary); }
.logo-x    { color: var(--accent); }

/* Sidebar caption */
section[data-testid="stSidebar"] .stCaption {
    color: var(--text-muted) !important;
    font-size: 0.75rem !important;
    margin-bottom: 4px !important;
}

/* Sidebar divider */
.sdiv {
    border: none;
    border-top: 1px solid var(--border);
    margin: 10px 0;
}

/* Sidebar success badge (logged-in user) */
section[data-testid="stSidebar"] .stAlert {
    background: rgba(232,71,42,0.08) !important;
    border: 1px solid var(--border-warm) !important;
    border-radius: 10px !important;
    color: #f5a882 !important;
}

/* ── Sidebar buttons: New Chat ── */
section[data-testid="stSidebar"] .stButton button {
    width: 100%;
    margin-bottom: 5px;
    border-radius: 10px;
    font-size: 0.84rem;
    font-weight: 500;
    transition: all 0.18s ease;
}

/* New Chat button — accent filled */
section[data-testid="stSidebar"] .stButton:first-of-type button {
    background: var(--accent) !important;
    border: none !important;
    color: #fff !important;
}
section[data-testid="stSidebar"] .stButton:first-of-type button:hover {
    background: var(--accent-warm) !important;
    transform: translateY(-1px);
    box-shadow: 0 4px 16px var(--accent-glow);
}

/* History chat buttons */
.history-btn button {
    text-align: left !important;
    justify-content: flex-start !important;
    background: var(--bg-glass) !important;
    border: 1px solid var(--border) !important;
    color: var(--text-primary) !important;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
    padding: 6px 10px !important;
}
.history-btn button:hover {
    background: var(--bg-glass2) !important;
    border-color: var(--border-warm) !important;
}

/* Logout / secondary sidebar buttons */
section[data-testid="stSidebar"] [data-testid="stBaseButton-secondary"] button {
    background: var(--bg-glass) !important;
    border: 1px solid var(--border) !important;
    color: var(--text-muted) !important;
}
section[data-testid="stSidebar"] [data-testid="stBaseButton-secondary"] button:hover {
    border-color: var(--border-warm) !important;
    color: var(--text-primary) !important;
}

/* Sidebar info box */
section[data-testid="stSidebar"] .stInfo {
    background: rgba(212,168,75,0.07) !important;
    border: 1px solid rgba(212,168,75,0.18) !important;
    border-radius: 10px !important;
    font-size: 0.8rem !important;
    color: #e8d5a3 !important;
}

/* ── Past chats label ── */
section[data-testid="stSidebar"] strong {
    color: var(--text-muted) !important;
    font-size: 0.72rem !important;
    text-transform: uppercase !important;
    letter-spacing: 0.08em !important;
}

/* ── Sidebar text inputs (login/signup) ── */
section[data-testid="stSidebar"] [data-testid="stTextInput"] input {
    background: rgba(255,255,255,0.04) !important;
    border: 1px solid var(--border) !important;
    border-radius: 8px !important;
    color: var(--text-primary) !important;
    font-size: 0.85rem !important;
}
section[data-testid="stSidebar"] [data-testid="stTextInput"] input:focus {
    border-color: var(--border-warm) !important;
    box-shadow: 0 0 0 2px var(--accent-dim) !important;
}

/* ═══════════════════════════════════════════════════
   CHAT BUBBLES
   ═══════════════════════════════════════════════════ */
.stChatMessage {
    border-radius: var(--radius-lg) !important;
    padding: 12px 16px !important;
    margin-bottom: 8px !important;
    border: 1px solid var(--border) !important;
    backdrop-filter: blur(8px) !important;
    transition: box-shadow 0.2s ease;
}

/* Assistant bubble — glass with warm left border */
[data-testid="stChatMessage-assistant"] {
    background: rgba(19,22,30,0.85) !important;
    border-left: 3px solid var(--accent) !important;
}

/* User bubble — slightly lighter */
[data-testid="stChatMessage-user"] {
    background: rgba(30,24,20,0.7) !important;
    border-left: 3px solid var(--gold) !important;
}

/* Chat message text */
.stChatMessage p, .stChatMessage li, .stChatMessage td {
    color: var(--text-primary) !important;
    line-height: 1.65 !important;
    font-size: 0.92rem !important;
}

/* Inline code */
.stChatMessage code {
    background: rgba(232,71,42,0.10) !important;
    color: #f5a882 !important;
    border-radius: 4px !important;
    padding: 1px 5px !important;
    font-size: 0.85em !important;
}

/* Code blocks */
.stChatMessage pre {
    background: rgba(0,0,0,0.45) !important;
    border: 1px solid var(--border) !important;
    border-radius: 10px !important;
    padding: 12px 16px !important;
}

/* ── Inline images ── */
.chat-image {
    max-width: 320px;
    border-radius: 12px;
    margin-top: 8px;
    border: 1px solid var(--border-warm);
}

/* ── Active-image banner ── */
.img-banner {
    display: inline-flex;
    align-items: center;
    gap: 8px;
    background: rgba(232,71,42,0.08);
    border: 1px solid var(--border-warm);
    border-radius: 10px;
    padding: 5px 12px;
    font-size: 0.8rem;
    color: #f5a882;
    margin-bottom: 8px;
}

/* ── Pending badges ── */
.pending-badge {
    display: inline-flex;
    align-items: center;
    gap: 6px;
    background: rgba(232,71,42,0.10);
    border: 1px solid var(--border-warm);
    border-radius: 8px;
    padding: 3px 10px;
    font-size: 0.77rem;
    color: #f5a882;
}
.voice-badge {
    display: inline-flex;
    align-items: center;
    gap: 6px;
    background: rgba(212,168,75,0.10);
    border: 1px solid rgba(212,168,75,0.28);
    border-radius: 8px;
    padding: 3px 10px;
    font-size: 0.77rem;
    color: #e8d5a3;
}

/* ═══════════════════════════════════════════════════
   FIXED BOTTOM TOOLBAR
   ═══════════════════════════════════════════════════ */

/* Reset inner wrapper spacing */
[data-testid="stVerticalBlockBorderWrapper"][data-key="toolbar"],
[data-testid="stVerticalBlockBorderWrapper"][data-key="toolbar"] > div,
[data-testid="stVerticalBlockBorderWrapper"][data-key="toolbar"] [data-testid="stVerticalBlock"] {
    padding: 0 !important;
    margin: 0 !important;
}

/* Main toolbar bar */
[data-testid="stVerticalBlockBorderWrapper"][data-key="toolbar"] {
    position: fixed !important;
    bottom: 0 !important;
    left: 0 !important;
    right: 0 !important;
    z-index: 200 !important;
    background: rgba(13,15,20,0.92) !important;
    backdrop-filter: blur(20px) !important;
    border-top: 1px solid var(--border-warm) !important;
    padding: 10px 20px 14px !important;
}
@media (min-width: 768px) {
    [data-testid="stVerticalBlockBorderWrapper"][data-key="toolbar"] {
        left: var(--sidebar-w) !important;
    }
}

/* Columns: vertically centred */
[data-testid="stVerticalBlockBorderWrapper"][data-key="toolbar"] [data-testid="stHorizontalBlock"] {
    gap: 8px !important;
    align-items: center !important;
}
[data-testid="stVerticalBlockBorderWrapper"][data-key="toolbar"] [data-testid="stColumn"] {
    padding: 0 !important;
}

/* ── Upload column ── */
[data-testid="stVerticalBlockBorderWrapper"][data-key="toolbar"] [data-testid="stColumn"]:first-child {
    flex: 0 0 52px !important;
    max-width: 52px !important;
    min-width: 52px !important;
    display: flex !important;
    align-items: center !important;
    justify-content: center !important;
    padding: 0 !important;
}

/* ── File uploader: clip to 42x42 ── */
[data-testid="stVerticalBlockBorderWrapper"][data-key="toolbar"] [data-testid="stFileUploader"] {
    width: 42px !important;
    height: 42px !important;
    min-height: 0 !important;
    overflow: hidden !important;
}
[data-testid="stVerticalBlockBorderWrapper"][data-key="toolbar"] [data-testid="stFileUploader"] > label {
    display: none !important;
}
[data-testid="stVerticalBlockBorderWrapper"][data-key="toolbar"] [data-testid="stFileUploader"] > section {
    width: 42px !important;
    height: 42px !important;
    min-height: 0 !important;
    padding: 0 !important;
    margin: 0 !important;
    border: none !important;
    background: transparent !important;
    overflow: hidden !important;
    display: flex !important;
    align-items: center !important;
    justify-content: center !important;
}
[data-testid="stVerticalBlockBorderWrapper"][data-key="toolbar"] [data-testid="stFileUploaderDropzone"] {
    display: none !important;
}
[data-testid="stVerticalBlockBorderWrapper"][data-key="toolbar"] [data-testid="stFileUploader"] > section > * {
    display: none !important;
}
[data-testid="stVerticalBlockBorderWrapper"][data-key="toolbar"] [data-testid="stFileUploader"] > section > button {
    display: flex !important;
    align-items: center !important;
    justify-content: center !important;
    width: 42px !important;
    height: 42px !important;
    min-width: 42px !important;
    min-height: 42px !important;
    border-radius: 50% !important;
    background: rgba(255,255,255,0.06) !important;
    border: 1px solid rgba(255,255,255,0.12) !important;
    padding: 0 !important;
    overflow: hidden !important;
    cursor: pointer !important;
    transition: background 0.18s, box-shadow 0.18s !important;
}
[data-testid="stVerticalBlockBorderWrapper"][data-key="toolbar"] [data-testid="stFileUploader"] > section > button:hover {
    background: var(--accent-dim) !important;
    border-color: var(--border-warm) !important;
    box-shadow: 0 0 10px var(--accent-glow) !important;
}
[data-testid="stVerticalBlockBorderWrapper"][data-key="toolbar"] [data-testid="stFileUploader"] > section > button > span {
    display: none !important;
}
[data-testid="stVerticalBlockBorderWrapper"][data-key="toolbar"] [data-testid="stFileUploader"] > section > button::before {
    content: "📎";
    font-size: 1.1rem;
    line-height: 1;
}

/* ── Hide mic audio playback ── */
[data-testid="stVerticalBlockBorderWrapper"][data-key="toolbar"] audio { display: none !important; }

/* ── Hide hidden form submit ── */
[data-testid="stVerticalBlockBorderWrapper"][data-key="toolbar"] [data-testid="stFormSubmitButton"] {
    display: none !important;
}
[data-testid="stVerticalBlockBorderWrapper"][data-key="toolbar"] [data-testid="stForm"] {
    border: none !important;
    padding: 0 !important;
    background: transparent !important;
}
[data-testid="stVerticalBlockBorderWrapper"][data-key="toolbar"] [data-testid="stForm"] > div:first-child {
    padding: 0 !important;
}

/* ── Mic button ── */
[data-testid="stVerticalBlockBorderWrapper"][data-key="toolbar"] [data-testid="stBaseButton-secondary"]:first-of-type,
[data-testid="stVerticalBlockBorderWrapper"][data-key="toolbar"] .mic-col button {
    border-radius: 50% !important;
    width: 42px !important;
    height: 42px !important;
    min-width: 42px !important;
    padding: 0 !important;
    font-size: 1.1rem !important;
    background: rgba(255,255,255,0.06) !important;
    border: 1px solid rgba(255,255,255,0.12) !important;
    color: var(--text-primary) !important;
    transition: background 0.18s, box-shadow 0.18s;
}
[data-testid="stVerticalBlockBorderWrapper"][data-key="toolbar"] .mic-col button:hover {
    background: var(--accent-dim) !important;
    border-color: var(--border-warm) !important;
    box-shadow: 0 0 10px var(--accent-glow) !important;
}

/* ── Text input ── */
[data-testid="stVerticalBlockBorderWrapper"][data-key="toolbar"] [data-testid="stTextInput"] {
    width: 100% !important;
}
[data-testid="stVerticalBlockBorderWrapper"][data-key="toolbar"] [data-testid="stTextInput"] label,
[data-testid="stVerticalBlockBorderWrapper"][data-key="toolbar"] [data-testid="InputInstructions"] {
    display: none !important;
}
[data-testid="stVerticalBlockBorderWrapper"][data-key="toolbar"] [data-testid="stTextInput"] input {
    border-radius: 26px !important;
    padding: 11px 20px !important;
    font-size: 0.93rem !important;
    background: rgba(255,255,255,0.05) !important;
    border: 1px solid rgba(255,255,255,0.10) !important;
    color: var(--text-primary) !important;
    width: 100% !important;
    transition: border-color 0.2s, box-shadow 0.2s;
}
[data-testid="stVerticalBlockBorderWrapper"][data-key="toolbar"] [data-testid="stTextInput"] input:focus {
    border-color: var(--border-warm) !important;
    box-shadow: 0 0 0 3px var(--accent-dim) !important;
    outline: none !important;
}
[data-testid="stVerticalBlockBorderWrapper"][data-key="toolbar"] [data-testid="stTextInput"] input::placeholder {
    color: rgba(240,236,228,0.30) !important;
}

/* ── Send button ── */
[data-testid="stVerticalBlockBorderWrapper"][data-key="toolbar"] .send-col button {
    border-radius: 50% !important;
    width: 42px !important;
    height: 42px !important;
    min-width: 42px !important;
    padding: 0 !important;
    font-size: 1.15rem !important;
    background: var(--accent) !important;
    border: none !important;
    color: #fff !important;
    display: flex !important;
    align-items: center !important;
    justify-content: center !important;
    transition: background 0.18s, transform 0.12s, box-shadow 0.18s;
    box-shadow: 0 2px 12px var(--accent-glow);
}
[data-testid="stVerticalBlockBorderWrapper"][data-key="toolbar"] .send-col button:hover {
    background: var(--accent-warm) !important;
    transform: scale(1.08);
    box-shadow: 0 4px 20px var(--accent-glow);
}

/* ═══════════════════════════════════════════════════
   SCROLLBAR
   ═══════════════════════════════════════════════════ */
::-webkit-scrollbar { width: 5px; }
::-webkit-scrollbar-track { background: transparent; }
::-webkit-scrollbar-thumb {
    background: rgba(232,71,42,0.25);
    border-radius: 99px;
}
::-webkit-scrollbar-thumb:hover { background: rgba(232,71,42,0.45); }

</style>
""", unsafe_allow_html=True)


# ══════════════════════════════════════════════════════════════════════════════
# SIDEBAR
# ══════════════════════════════════════════════════════════════════════════════
with st.sidebar:
    st.markdown('<div class="sidebar-logo"><span class="logo-lufy">Lufy</span><span class="logo-x">x</span></div>', unsafe_allow_html=True)
    st.caption("Your nakama AI ⚓")
    st.markdown('<div class="sdiv"></div>', unsafe_allow_html=True)

    if st.session_state.logged_in:
        st.success(f"👤  {st.session_state.username}")
        st.markdown('<div class="sdiv"></div>', unsafe_allow_html=True)

        if st.button("✏️  New Chat"):
            _reset_chat(st.session_state.username)
            st.rerun()

        st.markdown('<div class="sdiv"></div>', unsafe_allow_html=True)

        sessions = db_get_sessions(st.session_state.username)
        if sessions:
            st.markdown("**🕘 Past Chats**")
            for sid, title, _ in sessions:
                is_active = (sid == st.session_state.session_id)
                col_a, col_b = st.columns([5, 1])
                with col_a:
                    st.markdown('<div class="history-btn">', unsafe_allow_html=True)
                    if st.button(
                        f"{'▶ ' if is_active else ''}{title}", key=f"sess_{sid}"
                    ):
                        st.session_state.session_id  = sid
                        st.session_state.messages    = db_load_messages(sid)
                        st.session_state.active_image = None
                        st.session_state.pdf_context = ""
                        st.session_state.pdf_name    = ""
                        st.rerun()
                    st.markdown("</div>", unsafe_allow_html=True)
                with col_b:
                    if st.button("🗑", key=f"del_{sid}"):
                        db_delete_session(sid)
                        if st.session_state.session_id == sid:
                            st.session_state.session_id  = None
                            st.session_state.messages    = [_WELCOME]
                            st.session_state.active_image = None
                        st.rerun()
        else:
            st.caption("No saved chats yet.")

        st.markdown('<div class="sdiv"></div>', unsafe_allow_html=True)
        if st.button("🚪  Logout"):
            for k, v in DEFAULTS.items():
                st.session_state[k] = v
            st.query_params.clear()
            st.rerun()

    else:
        st.info("💬 Chatting as **Guest**\nLogin to save chats & upload files.")
        st.markdown('<div class="sdiv"></div>', unsafe_allow_html=True)

        c1, c2 = st.columns(2)
        with c1:
            if st.button("🔑 Login"):
                st.session_state.auth_view = "login"
        with c2:
            if st.button("📝 Sign Up"):
                st.session_state.auth_view = "signup"

        if st.session_state.auth_view == "login":
            st.markdown('<div class="sdiv"></div>', unsafe_allow_html=True)
            st.markdown("#### 🔑 Login")
            uname = st.text_input("Username", key="login_username")
            pwd   = st.text_input("Password", type="password", key="login_password")
            lc1, lc2 = st.columns(2)
            with lc1:
                login_btn = st.button("Login", key="do_login")
            with lc2:
                if st.button("Cancel", key="cancel_login"):
                    st.session_state.auth_view = None
                    st.rerun()
            if login_btn:
                cursor.execute(
                    "SELECT * FROM users WHERE username=? AND password=?", (uname, pwd)
                )
                if cursor.fetchone():
                    st.session_state.logged_in  = True
                    st.session_state.username   = uname
                    st.session_state.auth_view  = None
                    st.query_params["user"]     = uname
                    sid = db_create_session(uname)
                    st.session_state.session_id = sid
                    st.session_state.messages   = [
                        {"role": "assistant",
                         "content": f"Welcome back, {uname}! 👋",
                         "type": "text"}
                    ]
                    st.rerun()
                else:
                    st.error("Invalid username or password")

        elif st.session_state.auth_view == "signup":
            st.markdown('<div class="sdiv"></div>', unsafe_allow_html=True)
            st.markdown("#### 📝 Create Account")
            new_u = st.text_input("Username", key="signup_username")
            new_p = st.text_input("Password", type="password", key="signup_password")
            sc1, sc2 = st.columns(2)
            with sc1:
                signup_btn = st.button("Sign Up", key="do_signup")
            with sc2:
                if st.button("Cancel", key="cancel_signup"):
                    st.session_state.auth_view = None
                    st.rerun()
            if signup_btn:
                try:
                    cursor.execute(
                        "INSERT INTO users (username, password) VALUES (?,?)",
                        (new_u, new_p),
                    )
                    conn.commit()
                    st.session_state.logged_in  = True
                    st.session_state.username   = new_u
                    st.session_state.auth_view  = None
                    st.query_params["user"]     = new_u
                    sid = db_create_session(new_u)
                    st.session_state.session_id = sid
                    st.session_state.messages   = [
                        {"role": "assistant",
                         "content": f"Welcome, {new_u}! 👋",
                         "type": "text"}
                    ]
                    st.rerun()
                except Exception:
                    st.error("Username already exists.")

        st.markdown('<div class="sdiv"></div>', unsafe_allow_html=True)
        if st.button("🗑️  Clear Chat"):
            st.session_state.messages    = [_WELCOME]
            st.session_state.active_image = None
            st.rerun()


# ══════════════════════════════════════════════════════════════════════════════
# TITLE
# ══════════════════════════════════════════════════════════════════════════════
st.markdown('<h1 class="lufyx-title"><span class="t-lufy">Lufy</span><span class="t-x">x</span></h1>', unsafe_allow_html=True)
if not st.session_state.logged_in:
    st.markdown('<p class="guest-caption">Chatting as <b>Guest</b> · Login to save chats &amp; upload files</p>', unsafe_allow_html=True)

# ── Active image context banner ────────────────────────────────────────────────
if st.session_state.active_image:
    st.markdown(
        f'<div class="img-banner">🖼️ Image context active: '
        f'<b>{st.session_state.active_image["name"]}</b> '
        f'— follow-up questions use this image</div>',
        unsafe_allow_html=True,
    )

# ── Pending-upload / voice badges ─────────────────────────────────────────────
badges = []
if st.session_state.pending_image:
    badges.append(
        f'<span class="pending-badge">🖼️ <b>{st.session_state.pending_image["name"]}</b>'
        f' staged — press ➤ to send</span>'
    )
if st.session_state.pending_pdf:
    badges.append(
        f'<span class="pending-badge">📄 <b>{st.session_state.pending_pdf["name"]}</b>'
        f' staged — press ➤ to send</span>'
    )
if st.session_state.voice_prefill:
    preview = st.session_state.voice_prefill[:60]
    if len(st.session_state.voice_prefill) > 60:
        preview += "…"
    badges.append(f'<span class="voice-badge">🎤 {preview}</span>')

if badges:
    bc, xc = st.columns([11, 1])
    with bc:
        st.markdown("  ".join(badges), unsafe_allow_html=True)
    with xc:
        if st.button("✕", key="clear_pending"):
            st.session_state.pending_image      = None
            st.session_state.pending_pdf        = None
            st.session_state.voice_prefill      = ""
            st.session_state.last_uploaded_name = None
            st.rerun()

# ══════════════════════════════════════════════════════════════════════════════
# CHAT HISTORY
# ══════════════════════════════════════════════════════════════════════════════
for msg in st.session_state.messages:
    with st.chat_message(msg["role"]):
        t = msg.get("type", "text")
        if t == "image":
            st.markdown(
                f'<img src="{msg["content"]}" class="chat-image" />',
                unsafe_allow_html=True,
            )
        elif t == "pdf_text":
            st.markdown(
                f'📄 **{msg.get("fname", "document.pdf")}** uploaded'
                f' — ask me anything about it.'
            )
        else:
            st.markdown(msg["content"])


# ══════════════════════════════════════════════════════════════════════════════
# FIXED BOTTOM TOOLBAR
# Layout: [ 📎 ] [ 🎤 ] [ text input ──────────── ] [ ➤ ]
#
# Architecture:
#   • st.container(key="toolbar") renders into Streamlit's stBottom slot,
#     which CSS fixes to the viewport bottom.
#   • st.columns() inside gives a true flex row — no custom HTML wrappers.
#   • st.form() wraps the text input + send button so that pressing Enter
#     natively submits the form (form_submit_button handles it), exactly
#     like a real HTML form — no JavaScript needed.
#   • The form submit value and the send button value are OR-ed together
#     so either mechanism triggers the response pipeline.
# ══════════════════════════════════════════════════════════════════════════════
with st.container(key="toolbar"):
    upload_col, mic_col, input_col, send_col = st.columns(
        [0.10, 0.06, 0.76, 0.08], vertical_alignment="center"
    )

    # ── 📎 File uploader ──────────────────────────────────────────────────────
    with upload_col:
        uploaded_file = st.file_uploader(
            "",
            type=["pdf", "png", "jpg", "jpeg", "webp"],
            label_visibility="collapsed",
            key="hidden_uploader",
        )

    # ── 🎤 Mic recorder ───────────────────────────────────────────────────────
    with mic_col:
        st.markdown('<span class="mic-col">', unsafe_allow_html=True)
        audio = mic_recorder(
            start_prompt="🎤",
            stop_prompt="⏹",
            just_once=True,
            use_container_width=False,
            key="mic_recorder",
        )
        st.markdown("</span>", unsafe_allow_html=True)

    # ── Text input + Enter-to-send via st.form ────────────────────────────────
    # st.form submits on Enter natively (no JS required).
    # form_submit_button is hidden via CSS; the visible ➤ button in send_col
    # sets its own flag. Both are OR-ed in should_respond below.
    with input_col:
        with st.form(key="chat_form", clear_on_submit=True, border=False):
            input_key = f"user_input_{st.session_state.input_version}"
            typed = st.text_input(
                "Message",
                value=st.session_state.voice_prefill,
                placeholder="Ask Lufyx anything…",
                label_visibility="collapsed",
                key=input_key,
            )
            # Hidden form submit — activated by Enter key
            enter_send = st.form_submit_button(
                "Send", use_container_width=False
            )

    # ── ➤ Send button ─────────────────────────────────────────────────────────
    with send_col:
        st.markdown('<span class="send-col">', unsafe_allow_html=True)
        click_send = st.button("➤", key="send_btn")
        st.markdown("</span>", unsafe_allow_html=True)

# Combine both send signals into one flag used by the pipeline
send = enter_send or click_send


# ══════════════════════════════════════════════════════════════════════════════
# STEP 1 — Handle mic transcription
# After a successful transcribe we:
#   1. Store text in voice_prefill  → pre-fills the text input on next render
#   2. Set send_triggered = True    → pipeline fires automatically (no button click needed)
# ══════════════════════════════════════════════════════════════════════════════
if audio and audio.get("bytes"):
    with st.spinner("Transcribing…"):
        try:
            af = io.BytesIO(audio["bytes"])
            af.name = "voice.wav"
            result = groq_client.audio.transcriptions.create(
                model="whisper-large-v3", file=af, response_format="text"
            )
            txt = result.strip() if isinstance(result, str) else result.text.strip()
            if txt:
                st.session_state.voice_prefill  = txt
                st.session_state.send_triggered = True   # auto-send on next rerun
                st.rerun()
        except Exception as e:
            st.error(f"Voice error: {e}")


# ══════════════════════════════════════════════════════════════════════════════
# STEP 2 — Handle file upload → stage as pending
#
# Guard: only process if the filename is different from last processed.
# This prevents re-firing on every rerun while the uploader still holds the file.
# ══════════════════════════════════════════════════════════════════════════════
if uploaded_file is not None:
    if not st.session_state.logged_in:
        st.toast("🔒 Login required to upload files.", icon="🔒")
    elif uploaded_file.name != st.session_state.last_uploaded_name:
        raw  = uploaded_file.read()
        mime = uploaded_file.type
        st.session_state.last_uploaded_name = uploaded_file.name

        if mime == "application/pdf":
            text = pdf_to_text(raw)
            if text:
                st.session_state.pending_pdf = {"name": uploaded_file.name,
                                                "text": text[:8000]}
                st.rerun()
            else:
                st.toast("Could not extract PDF text — may be image-based.", icon="⚠️")
        else:
            b64 = to_data_uri(raw, mime)
            st.session_state.pending_image = {"name": uploaded_file.name, "b64": b64}
            st.rerun()


# ══════════════════════════════════════════════════════════════════════════════
# STEP 3 — Resolve prompt
# A send is valid when:
#   • The ➤ button was clicked (send=True), OR
#   • send_triggered=True (set by voice transcription for auto-send)
# AND there is content to send: typed text OR a pending file.
# ══════════════════════════════════════════════════════════════════════════════
# On a voice-triggered rerun, typed still holds voice_prefill value because
# st.text_input was rendered with value=voice_prefill this run.
prompt = typed.strip() if typed else ""

# Consume send_triggered flag — must happen before st.stop() so it always clears
voice_auto_send = st.session_state.send_triggered
if voice_auto_send:
    st.session_state.send_triggered = False
    # On a voice auto-send run, voice_prefill IS the prompt (input rendered with it)
    if not prompt and st.session_state.voice_prefill:
        prompt = st.session_state.voice_prefill.strip()

has_pending = bool(st.session_state.pending_image or st.session_state.pending_pdf)

should_respond = (send or voice_auto_send) and (bool(prompt) or has_pending)

if not should_respond:
    st.stop()   # nothing to do this run — skip the pipeline below


# ══════════════════════════════════════════════════════════════════════════════
# STEP 4 — Response pipeline
# ══════════════════════════════════════════════════════════════════════════════

# ── A) Consume pending image ───────────────────────────────────────────────────
#      Store it in active_image so follow-ups can reference it.
#      Only replace active_image if a NEW image is pending.
if st.session_state.pending_image:
    img = st.session_state.pending_image
    st.session_state.pending_image = None

    # Replace active image context (new image = new context)
    st.session_state.active_image = img

    # Show in chat
    img_msg = {"role": "user", "content": img["b64"], "type": "image"}
    st.session_state.messages.append(img_msg)
    with st.chat_message("user"):
        st.markdown(
            f'<img src="{img["b64"]}" class="chat-image" />', unsafe_allow_html=True
        )
    if st.session_state.logged_in and st.session_state.session_id:
        db_save_message(st.session_state.session_id, "user", img["b64"], "image")

# ── B) Consume pending PDF ─────────────────────────────────────────────────────
if st.session_state.pending_pdf:
    pdf = st.session_state.pending_pdf
    st.session_state.pending_pdf = None

    st.session_state.pdf_context = pdf["text"]
    st.session_state.pdf_name    = pdf["name"]

    pdf_msg = {"role": "user", "content": pdf["text"],
               "type": "pdf_text", "fname": pdf["name"]}
    st.session_state.messages.append(pdf_msg)
    with st.chat_message("user"):
        st.markdown(f'📄 **{pdf["name"]}** uploaded — ask me anything about it.')
    if st.session_state.logged_in and st.session_state.session_id:
        db_save_message(st.session_state.session_id, "user",
                        f"[PDF: {pdf['name']}]", "pdf_text")

# ── C) User text message ───────────────────────────────────────────────────────
if prompt:
    st.session_state.messages.append({"role": "user", "content": prompt, "type": "text"})
    with st.chat_message("user"):
        st.markdown(prompt)
    if st.session_state.logged_in and st.session_state.session_id:
        db_save_message(st.session_state.session_id, "user", prompt, "text")
        # Auto-title session on first user text message
        cursor.execute(
            "SELECT COUNT(*) FROM chat_messages"
            " WHERE session_id=? AND role='user' AND msg_type='text'",
            (st.session_state.session_id,),
        )
        if cursor.fetchone()[0] == 1:
            db_update_title(st.session_state.session_id, prompt)

# ── D) Build effective prompt ──────────────────────────────────────────────────
if prompt:
    effective_prompt = prompt
elif st.session_state.active_image:
    effective_prompt = "Please analyse this image and describe it in detail."
else:
    effective_prompt = "Please summarise the uploaded PDF document."

# ── E) Build and call the AI ───────────────────────────────────────────────────
with st.spinner("Lufyx is thinking…"):

    # Optional web search
    SEARCH_WORDS = {
        "today","latest","current","news","score","ipl","match",
        "football","soccer","cricket","nba","fifa","epl","uefa",
        "weather","stock","crypto","price","recent","live","won",
    }
    web_ctx = ""
    if any(w in effective_prompt.lower() for w in SEARCH_WORDS):
        try:
            web_ctx = str(tavily.search(query=effective_prompt).get("results", ""))
        except Exception:
            web_ctx = ""

    pdf_section = ""
    if st.session_state.pdf_context:
        pdf_section = (
            f"\n\n### 📄 PDF: {st.session_state.pdf_name}\n"
            f"{st.session_state.pdf_context}"
        )

    system_msg = f"""You are Lufyx — a brilliant, friendly AI assistant.

### 💬 Style
Warm and concise. Elaborate only when asked.

### 💻 Code
Complete, runnable, commented. State the language. Explain fixes clearly.

### 🔢 Math
Step-by-step, one step per line. Final answer on its own line.

### 📖 Explanations
Numbered steps or short paragraphs. Analogies for hard concepts.
End with a one-line summary.

### 🌐 Web context
{web_ctx or "None."}
{pdf_section}
"""

    api_msgs = [{"role": "system", "content": system_msg}]

    # Recent text history (images are sent inline, not replayed)
    text_hist = [
        m for m in st.session_state.messages[-20:]
        if m.get("type", "text") in ("text", "pdf_text")
    ][-10:]

    active_img = st.session_state.active_image  # may be set from this turn or a prior turn

    if active_img:
        # ── Vision path ──────────────────────────────────────────────────────
        # Include text history (minus the current user turn which we add below)
        for m in text_hist[:-1]:
            api_msgs.append({"role": m["role"], "content": m["content"]})

        # Build multimodal user message: image + text
        api_msgs.append({
            "role": "user",
            "content": [
                {"type": "image_url", "image_url": {"url": active_img["b64"]}},
                {"type": "text",      "text": effective_prompt},
            ],
        })
        response = groq_client.chat.completions.create(
            model="meta-llama/llama-4-scout-17b-16e-instruct",
            messages=api_msgs,
        )
    else:
        # ── Text / PDF path ───────────────────────────────────────────────────
        for m in text_hist:
            api_msgs.append({"role": m["role"], "content": m["content"]})
        response = groq_client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=api_msgs,
        )

    reply = response.choices[0].message.content

# ── F) Display and persist AI reply ────────────────────────────────────────────
st.session_state.messages.append({"role": "assistant", "content": reply, "type": "text"})
with st.chat_message("assistant"):
    st.markdown(reply)
if st.session_state.logged_in and st.session_state.session_id:
    db_save_message(st.session_state.session_id, "assistant", reply, "text")

# ── G) Reset toolbar input for next turn ───────────────────────────────────────
# Incrementing input_version changes the widget key, causing Streamlit to
# render a fresh (empty) text_input on the next run.
st.session_state.voice_prefill = ""
st.session_state.input_version += 1
st.rerun()