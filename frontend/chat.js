/* Personal Signals chat — talks to /.netlify/functions/chat */
(function () {
    "use strict";

    const ENDPOINT = "/.netlify/functions/chat";
    const messagesEl = document.getElementById("messages");
    const form = document.getElementById("input").form || document.getElementById("chat-form");
    const input = document.getElementById("input");
    const sendBtn = document.getElementById("send");
    const chips = document.getElementById("chips");
    const fileInput = document.getElementById("file-input");
    const attachBtn = document.getElementById("attach");
    const imgPreview = document.getElementById("img-preview");
    const imgThumb = document.getElementById("img-thumb");
    const imgRemove = document.getElementById("img-remove");

    // Conversation history sent to the function (role: user|assistant)
    const history = [];

    // Pending screenshot to send with the next message (data URL string)
    let pendingImage = null;

    // ── Image handling: downscale to keep the payload small ──────────────────
    function loadAndDownscale(file) {
        return new Promise((resolve, reject) => {
            const reader = new FileReader();
            reader.onerror = reject;
            reader.onload = () => {
                const img = new Image();
                img.onerror = reject;
                img.onload = () => {
                    const maxW = 1280;
                    const scale = Math.min(1, maxW / img.width);
                    const w = Math.round(img.width * scale), h = Math.round(img.height * scale);
                    const canvas = document.createElement("canvas");
                    canvas.width = w; canvas.height = h;
                    canvas.getContext("2d").drawImage(img, 0, 0, w, h);
                    resolve(canvas.toDataURL("image/jpeg", 0.82));
                };
                img.src = reader.result;
            };
            reader.readAsDataURL(file);
        });
    }
    function clearImage() {
        pendingImage = null;
        imgPreview.classList.add("hidden");
        fileInput.value = "";
    }
    attachBtn.addEventListener("click", () => fileInput.click());
    imgRemove.addEventListener("click", clearImage);
    fileInput.addEventListener("change", async () => {
        const file = fileInput.files && fileInput.files[0];
        if (!file) return;
        try {
            pendingImage = await loadAndDownscale(file);
            imgThumb.src = pendingImage;
            imgPreview.classList.remove("hidden");
        } catch {
            addBubble("bot", "⚠️ Couldn't read that image. Try a PNG/JPG screenshot.");
        }
    });

    // ── PIN gate (only if the function enforces CHAT_TOKEN) ───────────────
    // Stored locally so you enter it once. Sent as x-chat-token.
    function getToken() {
        let t = localStorage.getItem("chatToken") || "";
        return t;
    }
    function ensureToken() {
        // Lazy: we only prompt if the server rejects with 401.
        const t = prompt("Enter chat PIN (set as CHAT_TOKEN in Netlify):") || "";
        if (t) localStorage.setItem("chatToken", t);
        return t;
    }

    // ── Clock ──────────────────────────────────────────────────────────────
    function tickClock() {
        const d = new Date(Date.now() + 5.5 * 3600 * 1000);
        const el = document.getElementById("clock");
        if (el) el.textContent = `${String(d.getUTCHours()).padStart(2, "0")}:${String(d.getUTCMinutes()).padStart(2, "0")} IST`;
    }
    tickClock(); setInterval(tickClock, 30000);

    // ── Rendering ────────────────────────────────────────────────────────────
    function escapeHtml(s) {
        return s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
    }
    // tiny markdown: **bold**, line breaks, bullet lines
    function renderMarkdown(text) {
        const safe = escapeHtml(text);
        const blocks = safe.split(/\n{2,}/).map(b => {
            const withBold = b.replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>");
            return "<p>" + withBold.replace(/\n/g, "<br>") + "</p>";
        });
        return blocks.join("");
    }

    function addBubble(role, text, imageDataUrl) {
        const wrap = document.createElement("div");
        wrap.className = "flex animate-fade-in " + (role === "user" ? "justify-end" : "justify-start");
        const b = document.createElement("div");
        b.className = (role === "user" ? "bubble-user" : "bubble-bot") + " px-3.5 py-2.5 text-sm leading-relaxed max-w-[85%]";
        if (role === "user") {
            if (imageDataUrl) {
                const im = document.createElement("img");
                im.src = imageDataUrl;
                im.className = "rounded-lg mb-1.5 max-h-44 w-auto";
                b.appendChild(im);
            }
            const t = document.createElement("div");
            t.textContent = text;
            b.appendChild(t);
        } else {
            b.innerHTML = renderMarkdown(text);
        }
        wrap.appendChild(b);
        messagesEl.appendChild(wrap);
        messagesEl.scrollTop = messagesEl.scrollHeight;
        return b;
    }

    function addTyping() {
        const wrap = document.createElement("div");
        wrap.className = "flex justify-start animate-fade-in";
        wrap.id = "typing";
        wrap.innerHTML = '<div class="bubble-bot px-4 py-3 typing"><span></span><span></span><span></span></div>';
        messagesEl.appendChild(wrap);
        messagesEl.scrollTop = messagesEl.scrollHeight;
    }
    function removeTyping() {
        const t = document.getElementById("typing");
        if (t) t.remove();
    }

    // ── Send ─────────────────────────────────────────────────────────────────
    let busy = false;
    async function send(text) {
        text = (text || "").trim();
        const img = pendingImage;
        // allow sending with just an image (no text) — use a default ask
        if ((!text && !img) || busy) return;
        if (!text && img) text = "Analyse this screenshot and tell me: should I trade now? Give the verdict.";
        busy = true; sendBtn.disabled = true;

        addBubble("user", text, img);
        history.push({ role: "user", content: text });
        input.value = "";
        input.style.height = "auto";
        clearImage();
        addTyping();

        try {
            const headers = { "Content-Type": "application/json" };
            const tok = getToken();
            if (tok) headers["x-chat-token"] = tok;
            const payload = { messages: history };
            if (img) payload.image = img;  // data URL; function parses it

            let resp = await fetch(ENDPOINT, {
                method: "POST", headers, body: JSON.stringify(payload),
            });

            // If the server enforces a PIN and we don't have a valid one, ask once.
            if (resp.status === 401) {
                const t = ensureToken();
                if (t) {
                    resp = await fetch(ENDPOINT, {
                        method: "POST",
                        headers: { "Content-Type": "application/json", "x-chat-token": t },
                        body: JSON.stringify(payload),
                    });
                }
            }

            removeTyping();
            const data = await resp.json().catch(() => ({}));
            if (!resp.ok) {
                addBubble("bot", "⚠️ " + (data.error || `Error ${resp.status}`));
            } else {
                addBubble("bot", data.reply || "(no reply)");
                history.push({ role: "assistant", content: data.reply || "" });
            }
        } catch (e) {
            removeTyping();
            addBubble("bot", "⚠️ Network error: " + e.message);
        } finally {
            busy = false; sendBtn.disabled = false;
            input.focus();
        }
    }

    // ── Wiring ─────────────────────────────────────────────────────────────
    form.addEventListener("submit", (e) => { e.preventDefault(); send(input.value); });
    input.addEventListener("keydown", (e) => {
        if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send(input.value); }
    });
    input.addEventListener("input", () => {
        input.style.height = "auto";
        input.style.height = Math.min(input.scrollHeight, 128) + "px";
    });
    chips.addEventListener("click", (e) => {
        const btn = e.target.closest(".chip");
        if (btn) send(btn.dataset.q);
    });

    // Greeting
    addBubble("bot", "Hi 👋 I'm your signals assistant. Ask me about **trend**, or **entry / exit / SL** for NIFTY, Bank Nifty or Sensex — or tap a chip below.");
})();
