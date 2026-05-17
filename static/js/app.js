/* ═══════════════════════════════════════════
   DOM refs
═══════════════════════════════════════════ */
const input          = document.getElementById("message-input");
const sendBtn        = document.getElementById("send-btn");
const chatContainer  = document.getElementById("chat-container");
const micBtn         = document.getElementById("mic-btn");
const uploadBtn      = document.getElementById("upload-btn");
const fileInput      = document.getElementById("file-input");
const filePreview    = document.getElementById("file-preview");
const fileName       = document.getElementById("file-name");
const fileClear      = document.getElementById("file-clear");
const newChatBtn     = document.getElementById("new-chat-btn");
const chatHistory    = document.getElementById("chat-history");
const sidebarEl      = document.getElementById("sidebar");
const sidebarToggle  = document.getElementById("sidebar-toggle");
const sidebarExpand  = document.getElementById("sidebar-expand");
const sidebarUser    = document.getElementById("sidebar-username");
const btnLogout      = document.getElementById("btn-logout");
const btnShowLogin   = document.getElementById("btn-show-login");
const appEl          = document.getElementById("app");
const authOverlay    = document.getElementById("auth-overlay");


/* ═══════════════════════════════════════════
   App state
═══════════════════════════════════════════ */
let conversationHistory = [];
let stagedFile          = null;
let activeSessionId     = null;   // null = guest
let isGuest             = true;


/* ═══════════════════════════════════════════
   Bootstrap — check existing session
═══════════════════════════════════════════ */
(async () => {
    try {
        const res  = await fetch("/auth/me");
        const data = await res.json();
        if (data.loggedIn) {
            launchApp(data.username, false);
        }
        // else: stay on auth overlay
    } catch {
        // stay on auth overlay
    }
})();


/* ═══════════════════════════════════════════
   Auth UI logic
═══════════════════════════════════════════ */

// Tab switching
document.querySelectorAll(".auth-tab").forEach(tab => {
    tab.addEventListener("click", () => {
        document.querySelectorAll(".auth-tab").forEach(t => t.classList.remove("active"));
        tab.classList.add("active");
        document.querySelectorAll(".auth-form").forEach(f => f.classList.add("hidden"));
        document.getElementById(`form-${tab.dataset.tab}`).classList.remove("hidden");
    });
});

document.getElementById("btn-login").addEventListener("click", async () => {
    const username = document.getElementById("login-username").value.trim();
    const password = document.getElementById("login-password").value.trim();
    const errEl    = document.getElementById("login-error");
    errEl.textContent = "";

    const res  = await fetch("/auth/login", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ username, password }),
    });
    const data = await res.json();
    if (data.ok) {
        launchApp(data.username, false);
    } else {
        errEl.textContent = data.error || "Login failed.";
    }
});

document.getElementById("btn-register").addEventListener("click", async () => {
    const username = document.getElementById("reg-username").value.trim();
    const password = document.getElementById("reg-password").value.trim();
    const errEl    = document.getElementById("reg-error");
    errEl.textContent = "";

    const res  = await fetch("/auth/register", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ username, password }),
    });
    const data = await res.json();
    if (data.ok) {
        launchApp(data.username, false);
    } else {
        errEl.textContent = data.error || "Registration failed.";
    }
});

document.getElementById("btn-guest").addEventListener("click", () => {
    launchApp("Guest", true);
});

// Allow Enter key in auth inputs
["login-username","login-password"].forEach(id => {
    document.getElementById(id).addEventListener("keypress", e => {
        if (e.key === "Enter") document.getElementById("btn-login").click();
    });
});
["reg-username","reg-password"].forEach(id => {
    document.getElementById(id).addEventListener("keypress", e => {
        if (e.key === "Enter") document.getElementById("btn-register").click();
    });
});


/* ═══════════════════════════════════════════
   Launch app
═══════════════════════════════════════════ */
async function launchApp(username, guest) {
    isGuest = guest;
    authOverlay.style.display = "none";
    appEl.style.display       = "flex";
    updateAccountFooter(username, guest);

    if (guest) {
        chatHistory.style.display = "none";
        await startNewSession();
    } else {
        chatHistory.style.display = "";
        await loadSessionList();
        // Restore last active session from sessionStorage (survives page refresh)
        const savedSid = parseInt(sessionStorage.getItem("activeSid") || "0");
        if (savedSid && document.querySelector(`.history-item[data-sid="${savedSid}"]`)) {
            await loadSession(savedSid);
        } else if (!activeSessionId) {
            await startNewSession();
        }
    }

    input.focus();
}

/* Update sidebar footer to reflect current auth state */
function updateAccountFooter(username, guest) {
    sidebarUser.textContent = username;
    if (guest) {
        btnShowLogin.classList.remove("hidden");
        btnLogout.classList.add("hidden");
    } else {
        btnShowLogin.classList.add("hidden");
        btnLogout.classList.remove("hidden");
    }
}


/* ═══════════════════════════════════════════
   Logout
═══════════════════════════════════════════ */
btnLogout.addEventListener("click", async () => {
    await fetch("/auth/logout", { method: "POST" });
    location.reload();
});

/* Guest "Login / Sign Up" — show auth overlay without page reload */
btnShowLogin.addEventListener("click", () => {
    appEl.style.display       = "none";
    authOverlay.style.display = "flex";
    // Reset auth form state
    document.getElementById("login-username").value = "";
    document.getElementById("login-password").value = "";
    document.getElementById("login-error").textContent = "";
    document.getElementById("reg-username").value = "";
    document.getElementById("reg-password").value = "";
    document.getElementById("reg-error").textContent = "";
    // Default to login tab
    document.querySelectorAll(".auth-tab").forEach(t => t.classList.remove("active"));
    document.querySelector('[data-tab="login"]').classList.add("active");
    document.querySelectorAll(".auth-form").forEach(f => f.classList.add("hidden"));
    document.getElementById("form-login").classList.remove("hidden");
});


/* ═══════════════════════════════════════════
   Sidebar collapse / expand
═══════════════════════════════════════════ */
sidebarToggle.addEventListener("click", () => {
    sidebarEl.classList.add("collapsed");
    sidebarExpand.classList.remove("hidden");
});

sidebarExpand.addEventListener("click", () => {
    sidebarEl.classList.remove("collapsed");
    sidebarExpand.classList.add("hidden");
});


/* ═══════════════════════════════════════════
   New Chat
═══════════════════════════════════════════ */
newChatBtn.addEventListener("click", async () => {
    conversationHistory = [];
    stagedFile          = null;
    chatContainer.innerHTML = "";
    hideFilePreview();
    input.value = "";
    await startNewSession();
    input.focus();
});


/* ═══════════════════════════════════════════
   Session management
═══════════════════════════════════════════ */
async function startNewSession() {
    if (isGuest) {
        activeSessionId = null;
        return;
    }
    try {
        const res  = await fetch("/sessions", { method: "POST" });
        const data = await res.json();
        activeSessionId = data.id;
        sessionStorage.setItem("activeSid", data.id);   // persist across refresh
        await loadSessionList();
        highlightActiveSession();
    } catch {
        activeSessionId = null;
    }
}

async function loadSessionList() {
    try {
        const res      = await fetch("/sessions");
        const sessions = await res.json();
        renderSessionList(sessions);
    } catch {
        chatHistory.innerHTML = "";
    }
}

function renderSessionList(sessions) {
    chatHistory.innerHTML = "";
    sessions.forEach(sess => {
        const item = document.createElement("div");
        item.className   = "history-item" + (sess.id === activeSessionId ? " active" : "");
        item.dataset.sid = sess.id;

        const titleSpan = document.createElement("span");
        titleSpan.textContent = sess.title || "New Chat";
        titleSpan.style.overflow     = "hidden";
        titleSpan.style.textOverflow = "ellipsis";
        titleSpan.style.flex         = "1";

        const delBtn = document.createElement("button");
        delBtn.className   = "del-btn";
        delBtn.textContent = "✕";
        delBtn.title       = "Delete";
        delBtn.addEventListener("click", async (e) => {
            e.stopPropagation();
            await deleteSession(sess.id);
        });

        item.appendChild(titleSpan);
        item.appendChild(delBtn);

        item.addEventListener("click", () => loadSession(sess.id));
        chatHistory.appendChild(item);
    });
}

function highlightActiveSession() {
    document.querySelectorAll(".history-item").forEach(el => {
        el.classList.toggle("active", parseInt(el.dataset.sid) === activeSessionId);
    });
}

async function loadSession(sid) {
    try {
        const res      = await fetch(`/sessions/${sid}/messages`);
        const messages = await res.json();

        chatContainer.innerHTML = "";
        conversationHistory     = [];
        activeSessionId         = sid;
        sessionStorage.setItem("activeSid", sid);   // persist across refresh
        highlightActiveSession();

        messages.forEach(m => {
            const rendered = m.role === "assistant"
                ? marked.parse(m.content)
                : null;
            addMessage(
                rendered || m.content,
                m.role === "assistant" ? "ai" : "user",
                m.role === "assistant",
            );
            conversationHistory.push({ role: m.role, content: m.content });
        });
    } catch {
        // silently fail
    }
}

async function deleteSession(sid) {
    await fetch(`/sessions/${sid}`, { method: "DELETE" });
    if (activeSessionId === sid) {
        sessionStorage.removeItem("activeSid");
        conversationHistory = [];
        chatContainer.innerHTML = "";
        await startNewSession();
    } else {
        await loadSessionList();
    }
}


/* ═══════════════════════════════════════════
   Upload
═══════════════════════════════════════════ */
uploadBtn.addEventListener("click", () => fileInput.click());

fileInput.addEventListener("change", () => {
    const file = fileInput.files[0];
    if (!file) return;
    stageFile(file);
    fileInput.value = "";
});

fileClear.addEventListener("click", () => {
    stagedFile = null;
    hideFilePreview();
});

function stageFile(file) {
    const reader = new FileReader();
    reader.onload = (e) => {
        const dataURI = e.target.result;
        const b64     = dataURI.split(",")[1];
        const type    = file.type === "application/pdf" ? "pdf" : "image";
        stagedFile    = { type, dataURI, b64, name: file.name };
        fileName.textContent = file.name;
        filePreview.classList.remove("hidden");
    };
    reader.readAsDataURL(file);
}

function hideFilePreview() {
    filePreview.classList.add("hidden");
    fileName.textContent = "";
}


/* ═══════════════════════════════════════════
   Voice (Web Speech API)
═══════════════════════════════════════════ */
const SpeechRecognition = window.SpeechRecognition || window.webkitSpeechRecognition;
let recognition = null;
let isRecording = false;

if (SpeechRecognition) {
    recognition = new SpeechRecognition();
    recognition.lang = "en-US";
    recognition.interimResults = false;
    recognition.maxAlternatives = 1;

    recognition.onresult = (e) => {
        input.value = e.results[0][0].transcript;
        input.focus();
    };
    recognition.onend  = () => { isRecording = false; micBtn.classList.remove("recording"); };
    recognition.onerror = () => { isRecording = false; micBtn.classList.remove("recording"); };

    micBtn.addEventListener("click", () => {
        if (isRecording) { recognition.stop(); }
        else             { recognition.start(); isRecording = true; micBtn.classList.add("recording"); }
    });
} else {
    micBtn.style.display = "none";
}


/* ═══════════════════════════════════════════
   Chat helpers
═══════════════════════════════════════════ */
function addMessage(content, sender, isHTML = false) {
    const msg = document.createElement("div");
    msg.classList.add("message", sender);
    if (isHTML) msg.innerHTML = content;
    else        msg.innerText = content;
    chatContainer.appendChild(msg);
    chatContainer.scrollTop = chatContainer.scrollHeight;
    return msg;
}

function addTypingIndicator() {
    const el = document.createElement("div");
    el.classList.add("message", "ai");
    el.id = "typing-indicator";
    el.innerText = "LUFFYX is thinking…";
    chatContainer.appendChild(el);
    chatContainer.scrollTop = chatContainer.scrollHeight;
}

function removeTypingIndicator() {
    document.getElementById("typing-indicator")?.remove();
}


/* ═══════════════════════════════════════════
   Send message
═══════════════════════════════════════════ */
sendBtn.addEventListener("click", sendMessage);
input.addEventListener("keypress", (e) => { if (e.key === "Enter") sendMessage(); });

async function sendMessage() {
    const message = input.value.trim();
    const file    = stagedFile;

    if (!message && !file) return;

    // Show user bubble
    if (file?.type === "image") {
        const imgHTML =
            `<img src="${file.dataURI}" class="chat-image" alt="${file.name}">` +
            (message ? `<div>${escapeHTML(message)}</div>` : "");
        addMessage(imgHTML, "user", true);
    } else if (file?.type === "pdf") {
        addMessage(`📄 ${file.name}${message ? " — " + message : ""}`, "user");
    } else {
        addMessage(message, "user");
    }

    const payload = {
        message,
        history: conversationHistory,
        session_id: activeSessionId,
    };
    if (file?.type === "image") payload.image    = file.dataURI;
    if (file?.type === "pdf")   { payload.pdf = file.b64; payload.pdf_name = file.name; }

    conversationHistory.push({ role: "user", content: message || `[File: ${file?.name}]` });

    input.value = "";
    stagedFile  = null;
    hideFilePreview();

    addTypingIndicator();

    try {
        const response = await fetch("/chat", {
            method:  "POST",
            headers: { "Content-Type": "application/json" },
            body:    JSON.stringify(payload),
        });

        removeTypingIndicator();

        const data    = await response.json();
        const aiReply = data.reply || data.error || "No response.";

        addMessage(marked.parse(aiReply), "ai", true);
        conversationHistory.push({ role: "assistant", content: aiReply });

        // Refresh session list so title updates
        if (!isGuest) await loadSessionList();
        highlightActiveSession();

    } catch (err) {
        console.error(err);
        removeTypingIndicator();
        addMessage("Error talking to LUFFYX. Please try again.", "ai");
    }
}


/* ═══════════════════════════════════════════
   Utility
═══════════════════════════════════════════ */
function escapeHTML(str) {
    return str
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;");
}