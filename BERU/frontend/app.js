const API_BASE = "/api/v1";

const state = {
    conversationId: null,
    activeAgent: null,
    auth: { enabled: false, authenticated: false, mode: "" },
    tools: [],
    tasks: [],
    triggers: [],
    monitor: null,
    sending: false,
};

const pendingConfirms = new Map();

const STATUS = {
    ws: { ok: false, label: "disconnected" },
    server: { ok: false, label: "unknown" },
    auth: { ok: false, label: "unknown" },
    model: { ok: false, label: "unknown" },
};

const $ = (id) => document.getElementById(id);

function setChipText(id, cls, text) {
    const chip = $(id);
    chip.querySelector(".dot").className = "dot";
    chip.classList.remove("ok", "limited", "bad");
    if (cls) chip.classList.add(cls);
    const label = chip.querySelector(":scope > .chip-label");
    if (label) label.textContent = text;
}

async function apiFetch(path, options = {}) {
    const headers = Object.assign({ "Content-Type": "application/json" }, options.headers || {});
    const key = sessionStorage.getItem("beru_api_key");
    if (key) headers["X-API-Key"] = key;
    const init = {
        method: options.method || "GET",
        headers,
        credentials: "same-origin",
    };
    if (options.body !== undefined) init.body = JSON.stringify(options.body);
    let resp;
    try {
        resp = await fetch(API_BASE + path, init);
    } catch (err) {
        return { status: 0, ok: false, data: { error: { message: String(err) } } };
    }
    let data = null;
    try {
        data = await resp.json();
    } catch (err) {
        data = null;
    }
    return { status: resp.status, ok: resp.ok, data };
}

const esc = (s) => String(s == null ? "" : s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

function fmtTime(iso) {
    if (!iso) return "";
    const d = new Date(iso);
    return isNaN(d.getTime()) ? "" : d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

function renderChips() {
    setChipText("chip-server", STATUS.server.ok ? "ok" : "bad", STATUS.server.label);
    setChipText("chip-ws", STATUS.ws.ok ? "ok" : (STATUS.ws.label === "connecting" ? "limited" : "bad"), STATUS.ws.label);
    setChipText("chip-auth", STATUS.auth.ok ? "ok" : (STATUS.auth.enabled ? "limited" : "ok"), STATUS.auth.label);
    setChipText("chip-model", STATUS.model.ok ? "ok" : "limited", STATUS.model.label);
}

function renderAuthBanner() {
    const banner = $("banner-auth");
    const needsLogin = STATUS.auth.enabled && !STATUS.auth.authenticated;
    banner.classList.toggle("visible", !!needsLogin);
}

async function refreshAuth() {
    const r = await apiFetch("/auth/status");
    if (r.status === 200 || r.status === 401) {
        const d = r.data || {};
        state.auth = { enabled: !!d.auth_enabled, authenticated: !!d.authenticated, mode: d.mode || "" };
        if (!state.auth.enabled) {
            STATUS.auth = { ok: true, label: "open (localhost)" };
        } else if (state.auth.authenticated) {
            STATUS.auth = { ok: true, label: "connected" };
        } else {
            STATUS.auth = { ok: false, label: "not connected", enabled: true };
        }
    } else {
        STATUS.auth = { ok: false, label: "error", enabled: state.auth.enabled };
    }
    renderChips();
    renderAuthBanner();
}

async function loginWithKey() {
    const input = $("auth-key-input");
    const key = input.value.trim();
    if (!key) return;
    const r = await apiFetch("/auth/login", { method: "POST", body: { api_key: key } });
    if (r.status === 200) {
        sessionStorage.setItem("beru_api_key", key);
        input.value = "";
        await refreshAuth();
        reconnect();
    } else {
        input.value = "";
        const msg = (r.data && r.data.error && r.data.error.message) || "Login failed.";
        appendMessage("system", "Auth: " + msg);
    }
}

async function logout() {
    await apiFetch("/auth/logout", { method: "POST" });
    sessionStorage.removeItem("beru_api_key");
    await refreshAuth();
    reconnect();
}

function attachAuthInputs() {
    const btn = $("auth-login-btn");
    const input = $("auth-key-input");
    btn.addEventListener("click", loginWithKey);
    input.addEventListener("keydown", (e) => {
        if (e.key === "Enter") loginWithKey();
    });
}

function renderSidebarAgents(agents) {
    const list = $("agent-list");
    list.innerHTML = "";
    agents.forEach((agent) => {
        const card = document.createElement("div");
        card.className = "agent-card" + (state.activeAgent === agent.name ? " active" : "");
        card.innerHTML =
            '<div class="name">' + esc(agent.name) + "</div>" +
            '<div class="desc">' + esc(agent.description) + "</div>" +
            '<div class="caps">' + esc((agent.capabilities || []).join(" · ")) + "</div>";
        card.addEventListener("click", () => {
            state.activeAgent = agent.name;
            renderSidebarAgents(agents);
        });
        list.appendChild(card);
    });
}

function renderQuickStat() {
    const toolCounts = { available: 0, limited: 0, unavailable: 0 };
    state.tools.forEach((t) => { toolCounts[t.availability] = (toolCounts[t.availability] || 0) + 1; });
    $("quick-stat").innerHTML =
        '<div class="row"><span class="label">Model</span><span class="value">' + esc(STATUS.model.label) + "</span></div>" +
        '<div class="row"><span class="label">Tools available</span><span class="value">' + (toolCounts.available || 0) + "</span></div>" +
        '<div class="row"><span class="label">Tools limited</span><span class="value" style="color:var(--warning)">' + (toolCounts.limited || 0) + "</span></div>" +
        '<div class="row"><span class="label">Tools unavailable</span><span class="value" style="color:var(--error)">' + (toolCounts.unavailable || 0) + "</span></div>" +
        '<div class="row"><span class="label">Tasks scheduled</span><span class="value">' + state.tasks.length + "</span></div>";
}

function appendMessage(role, content, meta) {
    const area = $("chat-area");
    const el = document.createElement("div");
    el.className = "message " + role;
    if (meta) el.innerHTML = '<div class="meta">' + esc(meta) + "</div>" + esc(content);
    else el.textContent = content;
    area.appendChild(el);
    area.scrollTop = area.scrollHeight;
    return el;
}

function appendStreamMessage(role, content, meta) {
    const area = $("chat-area");
    const el = document.createElement("div");
    el.className = "message " + role;
    if (meta) el.innerHTML = '<div class="meta">' + esc(meta) + "</div>";
    el.appendChild(document.createTextNode(content || ""));
    area.appendChild(el);
    area.scrollTop = area.scrollHeight;
    return el;
}

let activeStreamEl = null;
let typingVisible = false;

function setTyping(on) {
    typingVisible = on;
    $("typing").style.display = on ? "block" : "none";
}

function registerConfirmation(p) {
    if (!p || !p.confirmation_id) return;
    if (!pendingConfirms.has(p.confirmation_id)) {
        pendingConfirms.set(p.confirmation_id, {
            confirmation_id: p.confirmation_id,
            conversation_id: p.conversation_id,
            agent: p.agent,
            tool: p.tool,
            arguments: p.arguments || {},
            status: "pending",
            resultText: "",
            error: null,
        });
    }
    renderConfirmations();
}

function renderConfirmations() {
    const list = $("confirm-list");
    const hasPending = Array.from(pendingConfirms.values()).some((c) => c.status === "pending");
    $("confirm-overlay").classList.toggle("visible", hasPending);
    list.innerHTML = "";
    pendingConfirms.forEach((rec) => {
        const row = document.createElement("div");
        row.className = "confirm-card";
        let actions = "";
        if (rec.status === "pending") {
            actions = '<div class="actions">' +
                '<button class="btn small" data-cid="' + esc(rec.confirmation_id) + '" data-act="approve">Approve</button>' +
                '<button class="btn small secondary" data-cid="' + esc(rec.confirmation_id) + '" data-act="deny">Deny</button>' +
                "</div>";
        }
        let resultHtml = "";
        if (rec.status === "approved") resultHtml = '<div class="result approved">Approved — ' + esc(rec.resultText || "executed") + "</div>";
        else if (rec.status === "denied") resultHtml = '<div class="result denied">Denied — the tool was not executed.</div>';
        else if (rec.status === "error") resultHtml = '<div class="result error">' + esc(rec.error || "request failed") + "</div>";

        row.innerHTML =
            "<div><span class=\"tool\">" + esc(rec.tool) + "</span> &middot; " +
            "agent <span class=\"tool\">" + esc(rec.agent) + "</span></div>" +
            '<div class="args">' + esc(JSON.stringify(rec.arguments, null, 2)) + "</div>" +
            actions + resultHtml;
        list.appendChild(row);
    });

    list.querySelectorAll("button[data-cid]").forEach((btn) => {
        btn.addEventListener("click", (e) => {
            const cid = btn.getAttribute("data-cid");
            const act = btn.getAttribute("data-act");
            resolveConfirmation(cid, act);
        });
    });
}

async function resolveConfirmation(confirmationId, action) {
    const rec = pendingConfirms.get(confirmationId);
    if (!rec || rec.status !== "pending") return;
    const path = action === "approve" ? "/chat/confirm" : "/chat/deny";
    const r = await apiFetch(path, {
        method: "POST",
        body: { conversation_id: rec.conversation_id, confirmation_id: rec.confirmation_id },
    });
    if (action === "approve") {
        if (r.status === 200) {
            rec.status = "approved";
            rec.resultText = (r.data.message && r.data.message.content) || "";
            if (rec.resultText) appendMessage("assistant", rec.resultText, "approved · " + rec.tool);
            if (r.data.conversation_id) state.conversationId = r.data.conversation_id;
        } else {
            rec.status = "error";
            rec.error = (r.data && r.data.error && r.data.error.message) || "Approval failed.";
        }
    } else {
        if (r.status === 200) {
            rec.status = "denied";
            appendMessage("system", "Denied tool call: " + rec.tool);
        } else {
            rec.status = "error";
            rec.error = (r.data && r.data.error && r.data.error.message) || "Deny failed.";
        }
    }
    renderConfirmations();
}

function toolCategory(name) {
    if (name.indexOf("browser_") === 0) return "Browser";
    if (["mouse_position", "mouse_move", "mouse_click", "mouse_double_click",
         "mouse_scroll", "screen_size", "screenshot", "keyboard_type",
         "keyboard_press", "keyboard_hotkey", "clipboard_read", "clipboard_write",
         "list_processes", "find_process", "list_windows", "focus_window",
         "close_window", "list_monitors", "list_hotkeys", "register_hotkey",
         "unregister_hotkey"].indexOf(name) !== -1) return "Desktop";
    if (["get_system_info", "run_command", "launch_app", "send_notification", "clock"].indexOf(name) !== -1) return "System";
    if (["voice_listen", "voice_speak"].indexOf(name) !== -1) return "Voice";
    if (["web_search"].indexOf(name) !== -1) return "Web";
    if (["create_flashcard", "search_knowledge", "lookup_scripture", "meditation_timer"].indexOf(name) !== -1) return "Study";
    if (["document_generator", "file_analyser", "code_analyser", "code_formatter"].indexOf(name) !== -1) return "Code & Docs";
    if (["calendar"].indexOf(name) !== -1) return "Calendar";
    return "Other";
}

let toolsAvailabilityFilter = "all";
let toolsSearch = "";

function renderTools() {
    const host = $("tab-tools");
    if (!state.tools.length) {
        host.innerHTML = '<div class="empty">No tools reported by the backend.</div>';
        return;
    }

    const availOpts = ["all", "available", "limited", "unavailable"];
    const filterBar =
        '<div class="panel-title">Filter</div>' +
        '<div class="item">' +
        '<div class="form-row"><input id="tools-search" placeholder="Search tools..." value="' + esc(toolsSearch) + '"></div>' +
        '<div class="actions" style="display:flex;gap:6px;flex-wrap:wrap">' +
        availOpts.map(function (a) {
            const active = toolsAvailabilityFilter === a ? ' style="outline:2px solid var(--accent)"' : "";
            return '<button class="btn small secondary" data-avail-filter="' + a + '"' + active + ">" + a + "</button>";
        }).join("") +
        "</div></div>";

    const q = toolsSearch.trim().toLowerCase();

    const filtered = state.tools.filter(function (t) {
        if (toolsAvailabilityFilter !== "all" && t.availability !== toolsAvailabilityFilter) return false;
        if (!q) return true;
        return (t.name + " " + (t.description || "")) .toLowerCase().indexOf(q) !== -1;
    });

    let body = "";
    if (!filtered.length) {
        body = '<div class="empty">No tools match the current filter.</div>';
    } else {
        const byCat = {};
        filtered.forEach(function (t) {
            const cat = toolCategory(t.name);
            (byCat[cat] = byCat[cat] || []).push(t);
        });
        const cats = Object.keys(byCat).sort();
        body = cats.map(function (cat) {
            const tools = byCat[cat].sort(function (a, b) { return a.name < b.name ? -1 : 1; });
            return '<div class="panel-title">' + esc(cat) +
                ' <span class="hint">(' + tools.length + ")</span></div>" +
                tools.map(function (t) {
                    const permBadges = (t.permissions || []).map(function (p) {
                        return '<span class="chip" style="margin-right:4px">' + esc(p) + "</span>";
                    }).join("");
                    const conf = t.requires_confirmation
                        ? '<span class="chip" style="margin-left:4px;color:var(--warning)">confirmation</span>'
                        : "";
                    return '<div class="item"><div class="head">' +
                        '<span class="title">' + esc(t.name) + "</span>" +
                        '<span class="av ' + esc(t.availability) + '">' + avGlyph(t.availability) + "</span></div>" +
                        '<div class="sub">' + esc(t.description) + "</div>" +
                        "<div>" + permBadges + conf + "</div>" +
                        "</div>";
                }).join("");
        }).join("");
    }

    host.innerHTML = filterBar + body;

    const $on = (id, fn) => { const el = $(id); if (el) el.addEventListener("input", fn); };
    $on("tools-search", (e) => { toolsSearch = e.target.value; renderTools(); });
    document.querySelectorAll("[data-avail-filter]").forEach(function (btn) {
        btn.addEventListener("click", function () {
            toolsAvailabilityFilter = btn.getAttribute("data-avail-filter");
            renderTools();
        });
    });
}

function avGlyph(a) {
    if (a === "available") return "\u{1F7E2} " + a;
    if (a === "limited") return "\u{1F7E1} " + a;
    return "\u{1F534} " + a;
}

function renderLlm(info) {
    const host = $("tab-settings");
    const last = info.last_test;
    const testLine = last
        ? '<div class="row"><span class="label">Last test</span><span class="value ' + (last.ok ? "ok-text" : "bad-text") + '">' +
          (last.ok ? "ok" : "failed") + " (" + (last.latency_ms != null ? last.latency_ms + " ms" : "n/a") + ")" + "</span></div>" +
          (last.note ? '<div class="row"><span class="label">Note</span><span class="value" style="color:var(--text-secondary);font-size:0.72rem">' + esc(last.note) + "</span></div>" : "") +
          (last.error ? '<div class="row"><span class="label">Error</span><span class="value bad-text" style="font-size:0.72rem">' + esc(last.error) + "</span></div>" : "")
        : '<div class="row"><span class="label">Last test</span><span class="value" style="color:var(--text-secondary)">not run</span></div>';

    const connected = last ? (last.ok && last.connected) : false;
    const connectedHtml = last
        ? (last.connected ? '<span class="ok-text">Connected</span>' : '<span class="warn-text">Not connected</span>')
        : '<span class="hint">Unknown — run a test</span>';

    host.innerHTML =
        '<div class="panel-title">Connection</div>' +
        '<div class="item"><div class="head"><span class="title">Status</span>' + connectedHtml + "</div></div>" +
        '<div class="item">' +
        '<div class="head"><span class="title">Provider</span><span class="value" style="color:var(--accent)">' + esc(info.provider) + "</span></div>" +
        '<div class="sub">Model: ' + esc(info.model || "—") + "</div>" +
        '<div class="sub">Base URL: ' + esc(info.base_url || "—") + "</div>" +
        '<div class="sub">API key configured: ' + (info.has_api_key ? "yes" : "no") + "</div>" +
        '<div class="sub">Configured for a real model: ' + (info.configured ? "yes" : "no") + "</div>" +
        testLine +
        "</div>" +
        '<button class="btn small" id="llm-test-btn">Run connection test</button>' +
        "</div>";

    const authCard =
        '<div class="panel-title">Access</div>' +
        '<div class="item"><div class="head"><span class="title">API authentication</span>' +
        (state.auth.enabled ? (state.auth.authenticated ? '<span class="ok-text">Connected</span>' : '<span class="bad-text">Disconnected</span>') : '<span class="ok-text">Open (no key)</span>') +
        "</div>" +
        (state.auth.enabled
            ? '<div class="sub">Mode: ' + esc(state.auth.mode || "none") + "</div>"
            : '<div class="sub">No BERU_API_KEY configured — running open. Intended for localhost only.</div>') +
        "</div>";
    if (state.auth.enabled) {
        authCard += '<div class="form-row"><input id="settings-key" type="password" placeholder="API key (new session)" autocomplete="off"></div>' +
            '<button class="btn small" id="settings-login-btn">Connect</button> ';
        if (state.auth.authenticated) authCard += '<button class="btn small secondary" id="settings-logout-btn">Disconnect</button>';
    }
    host.innerHTML += authCard;

    const signOut = $("settings-logout-btn");
    if (signOut) signOut.addEventListener("click", logout);
    const loginBtn = $("settings-login-btn");
    if (loginBtn) loginBtn.addEventListener("click", settingsLogin);
    const testBtn = $("llm-test-btn");
    if (testBtn) testBtn.addEventListener("click", runLlmTest);
}

async function settingsLogin() {
    const input = $("settings-key");
    const key = input.value.trim();
    if (!key) return;
    const r = await apiFetch("/auth/login", { method: "POST", body: { api_key: key } });
    if (r.status === 200) {
        sessionStorage.setItem("beru_api_key", key);
        input.value = "";
        await refreshAuth();
        await loadLlm();
        reconnect();
    } else {
        input.value = "";
        const msg = (r.data && r.data.error && r.data.error.message) || "Login failed.";
        appendMessage("system", "Auth: " + msg);
    }
}

async function runLlmTest() {
    const btn = $("llm-test-btn");
    btn.disabled = true;
    btn.textContent = "Testing...";
    const r = await apiFetch("/llm/test", { method: "POST" });
    btn.disabled = false;
    btn.textContent = "Run connection test";
    if (r.status === 200) {
        const info = Object.assign({ last_test: r.data }, await apiFetch("/llm").then((x) => x.data || {}));
        renderLlm(info);
    } else {
        const msg = (r.data && r.data.error && r.data.error.message) || "Test failed.";
        appendMessage("system", "LLM test: " + msg);
    }
}

async function loadLlm() {
    const r = await apiFetch("/llm");
    if (r.status === 200) renderLlm(r.data);
}

function renderTasks() {
    const host = $("tab-automation");
    const taskRows = state.tasks.length
        ? state.tasks.map((t) => {
            const nextRun = t.cancelled || t.paused ? "—" : (t.next_run ? fmtTime(t.next_run) : "—");
            const retention =
                (t.retention_days != null ? " · retain " + t.retention_days + "d" : "") +
                (t.keep_last != null ? " · keep " + t.keep_last : "");
            const actions =
                '<button class="btn small secondary" data-act="run" data-id="' + esc(t.id) + '">Run</button>' +
                (t.status === "paused" || t.status === "cancelled"
                    ? '<button class="btn small secondary" data-act="resume" data-id="' + esc(t.id) + '">Resume</button>'
                    : '') +
                (t.status === "active" || t.status === "pending" || t.status === "running"
                    ? '<button class="btn small secondary" data-act="pause" data-id="' + esc(t.id) + '">Pause</button>'
                    : '') +
                '<button class="btn small secondary" data-act="cancel" data-id="' + esc(t.id) + '">Cancel</button>' +
                '<button class="btn small secondary" data-act="history" data-id="' + esc(t.id) + '">History</button>' +
                '<button class="btn small secondary" data-act="edit" data-id="' + esc(t.id) + '">Edit</button>' +
                '<button class="btn small danger" data-act="delete" data-id="' + esc(t.id) + '">Delete</button>';
            return '<div class="item"><div class="head">' +
                '<span class="title">' + esc(t.name) + "</span>" +
                '<span class="hint">' + esc(t.status || "") + "</span></div>" +
                '<div class="sub">' + esc(t.task_type || "interval") + " · " + esc(t.handler || "") +
                (t.interval_seconds ? " · every " + t.interval_seconds + "s" : "") +
                (t.max_runs ? " · max " + t.max_runs + " runs" : "") +
                " · next " + nextRun + retention + "</div>" +
                (t.handler === "reactor"
                    ? '<div class="sub" style="color:var(--accent-dim)">listens to ' +
                      esc(t.payload && t.payload.listen_task_id || "?") + " · on " + esc(t.payload && t.payload.on || "all") + "</div>"
                    : "") +
                '<div class="actions">' + actions + "</div></div>";
        }).join("")
        : '<div class="empty">No scheduled tasks.</div>';

    const tags = state.monitor
        ? '<div class="item"><div class="head"><span class="title">Monitor</span>' +
          (state.monitor.running ? '<span class="ok-text">running</span>' : '<span class="warn-text">stopped</span>') + "</div>" +
          '<div class="sub">' + (state.monitor.sources || []).length + " sources · " + state.monitor.triggers + " triggers</div>" +
          '<div class="actions">' +
          (state.monitor.running
              ? '<button class="btn small secondary" id="monitor-stop">Stop loop</button>'
              : '<button class="btn small secondary" id="monitor-start">Start loop</button>') +
          '<button class="btn small secondary" id="monitor-tick">Tick now</button>' +
          '<button class="btn small secondary" id="monitor-refresh">Refresh</button>' +
          "</div></div>"
        : "";

    const triggerRows = state.triggers.length
        ? state.triggers.map((tg) => {
            const cond = (tg.condition && tg.condition.source)
                ? esc(tg.condition.source) + " " + esc(tg.condition.operator || "") +
                  (tg.condition.value !== undefined && tg.condition.value !== null ? " " + esc(String(tg.condition.value)) : "")
                : esc(tg.condition && tg.condition.condition_type || "custom");
            const retention =
                (tg.retention_days != null ? " · retain " + tg.retention_days + "d" : "") +
                (tg.keep_last != null ? " · keep " + tg.keep_last : "");
            const act = '<button class="btn small secondary" data-tact="history" data-id="' + esc(tg.id) + '">History</button>' +
                (tg.status === "active" || tg.status === "fired"
                    ? '<button class="btn small secondary" data-tact="pause" data-id="' + esc(tg.id) + '">Pause</button>'
                    : '<button class="btn small secondary" data-tact="resume" data-id="' + esc(tg.id) + '">Resume</button>') +
                (tg.status !== "disabled"
                    ? '<button class="btn small secondary" data-tact="disable" data-id="' + esc(tg.id) + '">Disable</button>'
                    : '') +
                '<button class="btn small danger" data-tact="delete" data-id="' + esc(tg.id) + '">Delete</button>';
            return '<div class="item"><div class="head">' +
                '<span class="title">' + esc(tg.name) + "</span>" +
                '<span class="hint">' + esc(tg.status || "") + "</span></div>" +
                '<div class="sub">' + cond + " · fired " + (tg.fire_count || 0) + "x" +
                (tg.cooldown_seconds ? " · cooldown " + tg.cooldown_seconds + "s" : "") + retention + "</div>" +
                (tg.actions && tg.actions.length
                    ? '<div class="sub" style="color:var(--accent-dim)">actions: ' +
                      tg.actions.map(function (a) { return esc(a && a.type || "?"); }).join(", ") + "</div>"
                    : "") +
                '<div class="actions">' + act + "</div></div>";
        }).join("")
        : '<div class="empty">No triggers.</div>';

    const triggerCreate =
        '<div class="panel-title">New trigger</div>' +
        '<div class="item">' +
        '<div class="form-row"><label>Name</label><input id="trigger-name" placeholder="e.g. high cpu"></div>' +
        '<div class="form-row"><label>Source</label><input id="trigger-source" placeholder="system.cpu_percent"></div>' +
        '<div class="form-row" style="display:flex;gap:8px">' +
        '<div style="flex:1"><label>Condition</label><select id="trigger-type">' +
        '<option value="threshold">Threshold</option><option value="changed">Changed</option>' +
        '<option value="pattern">Pattern</option><option value="custom">Custom</option></select></div>' +
        '<div style="flex:1"><label>Operator</label><select id="trigger-op">' +
        '<option value="eq">=</option><option value="gt">&gt;</option><option value="gte">&gt;=</option>' +
        '<option value="lt">&lt;</option><option value="lte">&lt;=</option><option value="neq">!=</option></select></div>' +
        "</div>" +
        '<div class="form-row"><label>Value</label><input id="trigger-value" placeholder="e.g. 80"></div>' +
        '<div class="form-row"><label>Cooldown (seconds, optional)</label><input id="trigger-cooldown" type="number" value="0"></div>' +
        '<button class="btn small" id="trigger-create-btn">Create trigger</button>' +
        "</div>";

    host.innerHTML =
        '<div class="panel-title">Monitor</div>' + tags +
        '<div class="panel-title">Triggers</div>' + triggerRows +
        triggerCreate +
        '<div class="panel-title">Scheduled tasks</div>' + taskRows +
        '<div class="panel-title">New task</div>' +
        '<div class="item">' +
        '<div class="form-row"><label>Name</label><input id="task-name" placeholder="e.g. daily summary"></div>' +
        '<div class="form-row"><div style="display:flex;gap:8px">' +
        '<div style="flex:1"><label>Type</label><select id="task-type"><option value="interval">Interval</option><option value="one_shot">One-shot</option></select></div>' +
        '<div style="flex:1"><label>Handler</label><select id="task-handler"><option value="notify">Notify</option><option value="agent_turn">Agent turn</option><option value="reactor">Reactor</option></select></div>' +
        "</div></div>" +
        '<div class="form-row" id="task-interval-row"><label>Interval (seconds)</label><input id="task-interval" type="number" value="3600"></div>' +
        '<div class="form-row" id="task-runat-row" style="display:none"><label>Run at (ISO-8601)</label><input id="task-runat" placeholder="2026-09-01T09:00:00"></div>' +
        '<div class="form-row" id="task-message-row"><label>Message (notify)</label><input id="task-message" placeholder="notification text"></div>' +
        '<div class="form-row" id="task-react-row" style="display:none">' +
        '<label>Listen to task ID</label><input id="task-listen" placeholder="task id to react to">' +
        '<div style="margin-top:6px"><label>On</label><select id="task-on"><option value="all">All runs</option><option value="success">Success only</option><option value="failure">Failure only</option></select></div>' +
        '<div style="margin-top:6px"><label>Message (optional override)</label><input id="task-react-msg" placeholder="leave empty for default"></div>' +
        "</div>" +
        '<div class="form-row"><label>Max runs (optional)</label><input id="task-maxruns" type="number" placeholder="leave empty for unlimited"></div>' +
        '<button class="btn small" id="task-create-btn">Create</button></div>';

    host.querySelectorAll("button[data-act]").forEach((btn) => {
        btn.addEventListener("click", () => taskAction(btn.getAttribute("data-act"), btn.getAttribute("data-id")));
    });
    host.querySelectorAll("button[data-tact]").forEach((btn) => {
        btn.addEventListener("click", () => triggerAction(btn.getAttribute("data-tact"), btn.getAttribute("data-id")));
    });
    const trigBtn = $("trigger-create-btn");
    if (trigBtn) trigBtn.addEventListener("click", createTrigger);
    const createBtn = $("task-create-btn");
    if (createBtn) createBtn.addEventListener("click", createTask);
    const typeSel = $("task-type");
    if (typeSel) typeSel.addEventListener("change", () => {
        $("task-interval-row").style.display = typeSel.value === "interval" ? "block" : "none";
        $("task-runat-row").style.display = typeSel.value === "one_shot" ? "block" : "none";
        $("task-react-row").style.display = $("task-handler").value === "reactor" ? "block" : "none";
    });
    const handlerSel = $("task-handler");
    if (handlerSel) handlerSel.addEventListener("change", () => {
        const isReactor = handlerSel.value === "reactor";
        $("task-react-row").style.display = isReactor ? "block" : "none";
        $("task-message-row").style.display = isReactor ? "none" : "block";
        const typeSel2 = $("task-type");
        if (isReactor && typeSel2) typeSel2.value = "one_shot";
        if (isReactor) {
            $("task-interval-row").style.display = "none";
            $("task-runat-row").style.display = "none";
        }
    });
    const startBtn = $("monitor-start");
    if (startBtn) startBtn.addEventListener("click", () => monitorControl("start"));
    const stopBtn = $("monitor-stop");
    if (stopBtn) stopBtn.addEventListener("click", () => monitorControl("stop"));
    const tickBtn = $("monitor-tick");
    if (tickBtn) tickBtn.addEventListener("click", () => monitorControl("tick"));
    const refreshBtn = $("monitor-refresh");
    if (refreshBtn) refreshBtn.addEventListener("click", () => loadAutomation(true));
}

async function loadTasks() {
    const r = await apiFetch("/scheduler/tasks");
    if (r.status === 200) {
        state.tasks = r.data || [];
        renderQuickStat();
    }
}

async function loadMonitor() {
    const r = await apiFetch("/monitor/status");
    if (r.status === 200) state.monitor = r.data;
}

async function loadTriggers() {
    const r = await apiFetch("/monitor/triggers");
    if (r.status === 200) state.triggers = r.data || [];
}

async function loadAutomation(forceRender) {
    await Promise.all([loadTasks(), loadMonitor(), loadTriggers()]);
    if (forceRender || $("tab-automation").querySelector(".panel-title")) renderTasks();
}

async function createTask() {
    const name = $("task-name").value.trim();
    if (!name) return;
    const taskType = $("task-type").value;
    const handler = $("task-handler").value;
    const body = { name, handler, task_type: taskType };
    if (taskType === "interval") {
        const iv = parseFloat($("task-interval").value || "0");
        if (iv > 0) body.interval_seconds = iv;
    } else {
        const runAt = $("task-runat").value.trim();
        if (runAt) body.run_at = runAt;
    }
    const msg = $("task-message").value.trim();
    if (handler === "notify") body.payload = { title: name, message: msg || name };
    if (handler === "agent_turn") body.payload = { message: msg || name };
    if (handler === "reactor") {
        const listen = ($("task-listen").value || "").trim();
        const on = ($("task-on") && $("task-on").value) || "all";
        const reactMsg = ($("task-react-msg").value || "").trim();
        if (!listen) {
            appendMessage("system", "Task: a reactor needs a 'Listen to task ID'.");
            return;
        }
        body.payload = { listen_task_id: listen, on, message: reactMsg };
    }
    const maxRuns = parseInt($("task-maxruns").value, 10);
    if (maxRuns > 0) body.max_runs = maxRuns;

    const r = await apiFetch("/scheduler/tasks", { method: "POST", body });
    if (r.status === 201 || r.status === 200) {
        $("task-name").value = "";
        $("task-message").value = "";
        await loadTasks();
        renderTasks();
    } else {
        const msg2 = (r.data && r.data.detail) || (r.data && r.data.error && r.data.error.message) || "Create failed.";
        appendMessage("system", "Task: " + msg2);
    }
}

async function taskAction(act, id) {
    if (act === "delete") {
        if (!confirm("Delete task " + id + "?")) return;
    }
    let r;
    if (act === "delete") r = await apiFetch("/scheduler/tasks/" + id, { method: "DELETE" });
    else if (act === "history") { showTaskHistory(id); return; }
    else if (act === "edit") { showTaskEdit(id); return; }
    else r = await apiFetch("/scheduler/tasks/" + id + "/" + act, { method: "POST" });
    if (r.status === 204 || r.status === 200) {
        await loadTasks();
        renderTasks();
    } else {
        const msg = (r.data && (r.data.detail || (r.data.error && r.data.error.message))) || "Action failed.";
        appendMessage("system", "Task: " + msg);
    }
}

async function showTaskHistory(id) {
    const r = await apiFetch("/scheduler/tasks/" + id + "/runs?limit=50");
    if (r.status !== 200) {
        appendMessage("system", "History: request failed.");
        return;
    }
    const h = r.data;
    const task = state.tasks.find((t) => t.id === id) || {};
    const s = h.summary || {};
    const rows = (h.runs || []).map((run) =>
        "<tr><td>" + fmtTime(run.run_at) + "</td><td>" + esc(run.status || "") + "</td>" +
        "<td>" + (run.duration_ms != null ? run.duration_ms + "ms" : "—") + "</td>" +
        "<td>" + esc((run.error || "").slice(0, 60)) + "</td></tr>"
    ).join("");
    const groups = (s.error_groups || []).map((g) =>
        "<div class=\"item\"><div class=\"head\"><span class=\"title\">" + esc(g.error) + "</span><span class=\"hint\">×" + g.count + "</span></div></div>"
    ).join("");
    const html =
        '<div class="panel-title">' + esc(task.name || id) + " — history</div>" +
        '<div class="item"><div class="head"><span class="title">Summary</span></div>' +
        '<div class="sub">total ' + (s.total || 0) + " · failed " + (s.failed || 0) +
        (s.success_rate != null ? " · success " + Math.round(s.success_rate * 100) + "%" : "") +
        (s.failure_rate != null ? " · failure " + Math.round(s.failure_rate * 100) + "%" : "") +
        (s.avg_duration_ms != null ? " · avg " + s.avg_duration_ms + " ms" : "") + "</div>" +
        (s.last_error
            ? '<div class="sub" style="color:var(--bad)">last error: ' + esc(s.last_error) + "</div>"
            : '<div class="sub">no recorded errors</div>') + "</div>" +
        '<div class="panel-title">Prune history</div>' +
        '<div class="item">' +
        '<div class="form-row"><label>Keep runs newer than (seconds)</label><input id="prune-secs" type="number" placeholder="e.g. 86400"></div>' +
        '<div class="form-row"><label>Keep newest N (optional)</label><input id="prune-keep" type="number" placeholder="e.g. 50"></div>' +
        '<div class="actions"><button class="btn small secondary" id="prune-btn">Prune runs</button></div>' +
        "</div>" +
        '<div class="panel-title">Error groups</div>' + (groups || '<div class="empty">No errors.</div>') +
        '<div class="panel-title">Runs</div>' +
        '<table class="table"><thead><tr><th>Time</th><th>Status</th><th>Duration</th><th>Error</th></tr></thead><tbody>' + rows + "</tbody></table>" +
        '<div class="actions"><button class="btn small secondary" id="history-back">Back</button></div>';
    $("tab-automation").innerHTML = html;
    const pruneBtn = $("prune-btn");
    if (pruneBtn) pruneBtn.addEventListener("click", async () => {
        const secs = $("prune-secs").value.trim();
        const keep = $("prune-keep").value.trim();
        const body = {};
        if (secs) body.retention_seconds = parseFloat(secs);
        if (keep) body.keep_last = parseInt(keep, 10);
        const pr = await apiFetch("/scheduler/tasks/" + id + "/prune-runs", { method: "POST", body });
        appendMessage("system", "Prune: " + (
            (pr.data && pr.data.deleted != null)
                ? pr.data.deleted + " rows deleted"
                : ((pr.data && (pr.data.detail || (pr.data.error && pr.data.error.message))) || "failed")
        ));
        showTaskHistory(id);
    });
    const back = $("history-back");
    if (back) back.addEventListener("click", () => renderTasks());
}

async function showTaskEdit(id) {
    const task = state.tasks.find((t) => t.id === id) || {};
    const html =
        '<div class="panel-title">' + esc(task.name || id) + " — edit</div>" +
        '<div class="item">' +
        '<div class="form-row"><label>Name (blank = keep)</label><input id="edit-name" value=""></div>' +
        '<div class="form-row"><label>Interval (seconds, blank = keep)</label><input id="edit-interval" type="number" value=""></div>' +
        '<div class="form-row"><label>Max runs (blank = keep)</label><input id="edit-maxruns" type="number" value=""></div>' +
        '<div class="form-row"><label>Retention days (blank = keep; 0 clears)</label><input id="edit-retention" type="number" step="any" value=""></div>' +
        '<div class="form-row"><label>Keep last N (blank = keep; 0 clears)</label><input id="edit-keep" type="number" value=""></div>' +
        '<div class="actions">' +
        '<button class="btn small" id="edit-save">Save</button>' +
        '<button class="btn small secondary" id="edit-back">Back</button>' +
        "</div></div>";
    $("tab-automation").innerHTML = html;
    const save = $("edit-save");
    if (save) save.addEventListener("click", async () => {
        const body = {};
        const name = $("edit-name").value.trim();
        const interval = $("edit-interval").value.trim();
        const maxruns = $("edit-maxruns").value.trim();
        const retention = $("edit-retention").value.trim();
        const keep = $("edit-keep").value.trim();
        if (name) body.name = name;
        if (interval) body.interval_seconds = parseFloat(interval);
        if (maxruns) body.max_runs = parseInt(maxruns, 10);
        if (retention !== "") body.retention_days = retention === "0" ? null : parseFloat(retention);
        if (keep !== "") body.keep_last = keep === "0" ? null : parseInt(keep, 10);
        const r = await apiFetch("/scheduler/tasks/" + id, { method: "PATCH", body });
        if (r.status === 200) {
            appendMessage("system", "Task: updated.");
            await loadTasks();
            renderTasks();
        } else {
            appendMessage("system", "Task edit: " + ((r.data && (r.data.detail || (r.data.error && r.data.error.message))) || "failed"));
        }
    });
    const back = $("edit-back");
    if (back) back.addEventListener("click", () => renderTasks());
}

async function createTrigger() {
    const name = $("trigger-name").value.trim();
    const source = $("trigger-source").value.trim();
    if (!name || !source) {
        appendMessage("system", "Trigger: a name and source are required.");
        return;
    }
    const type = $("trigger-type").value;
    const op = $("trigger-op").value;
    const valueRaw = $("trigger-value").value.trim();
    const cooldown = parseFloat($("trigger-cooldown").value || "0");
    let value = valueRaw;
    if (valueRaw !== "" && !isNaN(parseFloat(valueRaw))) value = parseFloat(valueRaw);
    const body = { name, condition_type: type, source, operator: op, value, cooldown_seconds: cooldown || 0 };
    const r = await apiFetch("/monitor/triggers", { method: "POST", body });
    if (r.status === 201 || r.status === 200) {
        $("trigger-name").value = "";
        $("trigger-source").value = "";
        $("trigger-value").value = "";
        await loadTriggers();
        renderTasks();
    } else {
        appendMessage("system", "Trigger: " + ((r.data && (r.data.detail || (r.data.error && r.data.error.message))) || "failed"));
    }
}

async function triggerAction(act, id) {
    if (act === "delete") {
        if (!confirm("Delete trigger " + id + "?")) return;
    }
    let r;
    if (act === "delete") r = await apiFetch("/monitor/triggers/" + id, { method: "DELETE" });
    else if (act === "history") { showTriggerHistory(id); return; }
    else r = await apiFetch("/monitor/triggers/" + id + "/" + act, { method: "POST" });
    if (r.status === 204 || r.status === 200) {
        await loadTriggers();
        renderTasks();
    } else {
        appendMessage("system", "Trigger: " + ((r.data && (r.data.detail || (r.data.error && r.data.error.message))) || "failed"));
    }
}

async function showTriggerHistory(id) {
    const r = await apiFetch("/monitor/triggers/" + id + "/fires?limit=50");
    if (r.status !== 200) {
        appendMessage("system", "Trigger history: request failed.");
        return;
    }
    const h = r.data;
    const trigger = state.triggers.find((tg) => tg.id === id) || {};
    const s = h.summary || {};
    const rows = (h.fires || []).map((f) =>
        "<tr><td>" + fmtTime(f.fired_at) + "</td><td>" +
        esc((f.condition && f.condition.source) || (f.condition && f.condition.condition_type) || "") + "</td>" +
        "<td>" + esc(f.value != null ? String(f.value) : "") + "</td></tr>"
    ).join("");
    const html =
        '<div class="panel-title">' + esc(trigger.name || id) + " — fire history</div>" +
        '<div class="item"><div class="head"><span class="title">Summary</span></div>' +
        '<div class="sub">total ' + (s.total || 0) + " · last value " +
        (s.last_value != null ? esc(String(s.last_value)) : "—") + "</div></div>" +
        '<div class="panel-title">Fires</div>' +
        '<table class="table"><thead><tr><th>Time</th><th>Condition</th><th>Value</th></tr></thead><tbody>' +
        (rows || "<tr><td colspan='3' class='empty'>No fires.</td></tr>") + "</tbody></table>" +
        '<div class="actions"><button class="btn small secondary" id="t-history-back">Back</button></div>';
    $("tab-automation").innerHTML = html;
    const back = $("t-history-back");
    if (back) back.addEventListener("click", () => renderTasks());
}

async function monitorControl(act) {
    const r = await apiFetch("/monitor/" + act, { method: "POST" });
    if (r.status === 200) {
        await loadMonitor();
        renderTasks();
    } else {
        const msg = (r.data && r.data.error && r.data.error.message) || "Monitor action failed.";
        appendMessage("system", "Monitor: " + msg);
    }
}

function renderFacts() {
    return apiFetch("/facts").then((r) => {
        const host = $("tab-memory");
        if (r.status !== 200) {
            host.innerHTML = '<div class="empty">Memory unavailable.</div>';
            return;
        }
        const facts = (r.data && r.data.items) || [];
        const rows = facts.map((f) =>
            '<div class="item"><div class="head"><span class="title">' + esc(f.key) + "</span>" +
            '<button class="btn small danger" data-del="' + esc(f.key) + '">Del</button></div>' +
            '<div class="sub">' + esc(f.value) + "</div>" +
            '<div class="sub">' + (f.updated_at ? fmtTime(f.updated_at) : "") + "</div></div>"
        ).join("");
        host.innerHTML =
            '<div class="panel-title">Facts (' + facts.length + ")</div>" + (rows || '<div class="empty">No remembered facts.</div>') +
            '<div class="panel-title">Remember</div>' +
            '<div class="item"><div class="form-row"><label>Key</label><input id="fact-key" placeholder="e.g. prefers_coffee"></div>' +
            '<div class="form-row"><label>Value</label><input id="fact-value" placeholder="value"></div>' +
            '<button class="btn small" id="fact-add-btn">Remember</button></div>';
        const addBtn = $("fact-add-btn");
        if (addBtn) addBtn.addEventListener("click", addFact);
        host.querySelectorAll("button[data-del]").forEach((btn) => {
            btn.addEventListener("click", () => deleteFact(btn.getAttribute("data-del")));
        });
    });
}

async function addFact() {
    const key = $("fact-key").value.trim();
    const value = $("fact-value").value.trim();
    if (!key || !value) return;
    const r = await apiFetch("/facts", { method: "POST", body: { key, value } });
    if (r.status === 201 || r.status === 200) renderFacts();
}

async function deleteFact(key) {
    const r = await apiFetch("/facts/" + encodeURIComponent(key), { method: "DELETE" });
    if (r.status === 204 || r.status === 200) renderFacts();
}

function renderPlans() {
    return apiFetch("/plans").then((r) => {
        const host = $("tab-plans");
        if (r.status !== 200) {
            host.innerHTML = '<div class="empty">Plans unavailable.</div>';
            return;
        }
        const plans = r.data || [];
        const rows = plans.map((p) =>
            '<div class="item"><div class="head"><span class="title">' + esc(p.goal) + "</span>" +
            '<span class="hint">' + esc(p.status) + " · " + (p.progress_pct != null ? p.progress_pct + "%" : "") + "</span></div>" +
            "</div>"
        ).join("");
        host.innerHTML =
            '<div class="panel-title">Plans (' + plans.length + ")</div>" + (rows || '<div class="empty">No plans.</div>') +
            '<div class="panel-title">New plan</div>' +
            '<div class="item"><div class="form-row"><label>Goal</label><input id="plan-goal" placeholder="a goal to decompose"></div>' +
            '<button class="btn small" id="plan-create-btn">Create</button></div>';
        const btn = $("plan-create-btn");
        if (btn) btn.addEventListener("click", createPlan);
    });
}

async function createPlan() {
    const goal = $("plan-goal").value.trim();
    if (!goal) return;
    const r = await apiFetch("/plans", { method: "POST", body: { goal } });
    if (r.status === 201 || r.status === 200) {
        $("plan-goal").value = "";
        renderPlans();
    } else {
        const msg = (r.data && r.data.error && r.data.error.message) || "Plan creation failed.";
        appendMessage("system", "Plan: " + msg);
    }
}

// ---- Desktop control panel ----

let desktopStatus = {};

async function loadDesktopStatus() {
    const r = await apiFetch("/desktop/status");
    if (r.status === 200) desktopStatus = r.data || {};
}

function _capBadge(label, ok) {
    const cls = ok ? "ok-text" : "bad-text";
    return '<span class="' + cls + '">' + esc(label) + (ok ? " available" : " unavailable") + "</span>";
}

function renderDesktop() {
    const host = $("tab-desktop");
    const av = desktopStatus;
    const statusHtml =
        '<div class="panel-title">Capabilities</div>' +
        '<div class="item" style="display:flex;flex-wrap:wrap;gap:8px">' +
        _capBadge("Screenshot", av.screenshot) +
        _capBadge("Mouse", av.mouse) +
        _capBadge("Keyboard", av.keyboard) +
        _capBadge("Clipboard", av.clipboard) +
        _capBadge("Apps", av.apps) +
        _capBadge("Windows", av.windows) +
        _capBadge("Monitors", av.monitors) +
        _capBadge("Hotkeys", av.hotkeys) +
        "</div>";

    host.innerHTML =
        statusHtml +
        "<div class='panel-title'>Screen</div>" +
        '<div class="item" id="desktop-screen">' +
        '<div class="sub">Click "Capture screenshot" below.</div>' +
        '<div class="actions">' +
        '<button class="btn small" id="desktop-screenshot-btn">Capture screenshot</button>' +
        '<button class="btn small secondary" id="desktop-screensize-btn">Screen size</button>' +
        "</div></div>" +
        "<div class='panel-title'>Mouse</div>" +
        '<div class="item" id="desktop-mouse">' +
        '<div class="sub" id="desktop-mouse-pos">Position: not read yet.</div>' +
        '<div class="form-row" style="margin-top:6px"><label>X</label><input id="desktop-mouse-x" type="number" value="0" style="width:80px;display:inline-block"> ' +
        '<label>Y</label><input id="desktop-mouse-y" type="number" value="0" style="width:80px;display:inline-block"></div>' +
        '<div class="actions">' +
        '<button class="btn small" id="desktop-mouse-pos-btn">Read position</button>' +
        '<button class="btn small secondary" id="desktop-mouse-move-btn">Move</button>' +
        '<button class="btn small secondary" id="desktop-mouse-click-btn">Click</button>' +
        '<button class="btn small secondary" id="desktop-mouse-dblclick-btn">Double-click</button>' +
        "</div>" +
        '<div class="form-row"><label>Scroll amount (positive = up)</label><input id="desktop-mouse-scroll" type="number" value="3" style="width:80px"></div>' +
        '<div class="actions">' +
        '<button class="btn small secondary" id="desktop-mouse-scroll-btn">Scroll</button>' +
        "</div></div>" +
        "<div class='panel-title'>Keyboard</div>" +
        '<div class="item">' +
        '<div class="form-row"><label>Text to type</label><input id="desktop-kb-type" placeholder="text..."></div>' +
        '<div class="actions"><button class="btn small" id="desktop-kb-type-btn">Type</button></div>' +
        '<div class="form-row"><label>Key name (enter, tab, esc...)</label><input id="desktop-kb-key" placeholder="enter"></div>' +
        '<div class="actions"><button class="btn small secondary" id="desktop-kb-press-btn">Press key</button></div>' +
        '<div class="form-row"><label>Hotkey (comma-separated, e.g. ctrl,c)</label><input id="desktop-kb-hotkey" placeholder="ctrl,c"></div>' +
        '<div class="actions"><button class="btn small secondary" id="desktop-kb-hotkey-btn">Hotkey</button></div>' +
        "</div>" +
        "<div class='panel-title'>Clipboard</div>" +
        '<div class="item">' +
        '<div class="sub" id="desktop-clip-content">Not read yet.</div>' +
        '<div class="actions"><button class="btn small" id="desktop-clip-read-btn">Read clipboard</button></div>' +
        '<div class="form-row"><label>Text to write</label><input id="desktop-clip-write" placeholder="text..."></div>' +
        '<div class="actions"><button class="btn small secondary" id="desktop-clip-write-btn">Write clipboard</button></div>' +
        "</div>" +
        "<div class='panel-title'>Applications</div>" +
        '<div class="item">' +
        '<div class="sub" id="desktop-apps-list">Not loaded.</div>' +
        '<div class="form-row"><label>Find by name</label><input id="desktop-apps-find" placeholder="notepad"></div>' +
        '<div class="actions">' +
        '<button class="btn small secondary" id="desktop-apps-list-btn">List processes</button>' +
        '<button class="btn small secondary" id="desktop-apps-find-btn">Find</button>' +
        "</div></div>" +
        "<div class='panel-title'>Windows <span class='hint' data-win-count></span></div>" +
        '<div class="item">' +
        '<div class="sub" id="desktop-windows-list">Not loaded.</div>' +
        '<div class="form-row"><label>Find by title</label><input id="desktop-windows-find" placeholder="edge"></div>' +
        '<div class="actions">' +
        '<button class="btn small secondary" id="desktop-windows-list-btn">List windows</button>' +
        '<button class="btn small secondary" id="desktop-windows-find-btn">Find</button>' +
        '<button class="btn small secondary" id="desktop-windows-stream-btn">Live view</button>' +
        "</div></div>" +
        "<div class='panel-title'>Monitors</div>" +
        '<div class="item"><div class="sub" id="desktop-monitors-list">Not loaded.</div>' +
        '<div class="actions"><button class="btn small secondary" id="desktop-monitors-btn">List monitors</button></div></div>' +
        "<div class='panel-title'>Global hotkeys</div>" +
        '<div class="item">' +
        '<div class="sub" id="desktop-hotkeys-list">Not loaded.</div>' +
        '<div class="form-row"><label>Name</label><input id="desktop-hotkey-name" placeholder="launch_terminal"></div>' +
        '<div class="form-row"><label>Combo (comma-separated)</label><input id="desktop-hotkey-combo" placeholder="ctrl,alt,k"></div>' +
        '<div class="actions"><button class="btn small secondary" id="desktop-hotkey-add-btn">Add hotkey</button></div>' +
        "</div>";

    const $on = (id, fn) => { const el = $(id); if (el) el.addEventListener("click", fn); };

    $on("desktop-screensize-btn", async () => {
        const r = await apiFetch("/desktop/mouse/screen-size");
        const box = $("desktop-screen");
        if (r.data && r.data.ok !== false && r.data.detail) {
            const d = r.data.detail;
            box.querySelector(".sub").textContent = d.width + " x " + d.height;
        } else if (r.status === 200 && r.data && r.data.detail) {
            box.querySelector(".sub").textContent = r.data.detail.width + " x " + r.data.detail.height;
        } else {
            const msg = (r.data && r.data.error) || "Not available.";
            box.querySelector(".sub").textContent = "Screen size: " + msg;
        }
    });

    $on("desktop-screenshot-btn", async () => {
        const box = $("desktop-screen");
        box.querySelector(".sub").textContent = "Capturing...";
        const r = await apiFetch("/desktop/screenshot", { method: "POST" });
        if (r.status === 200 && r.data && r.data.ok !== false && r.data.path) {
            const d = r.data;
            box.querySelector(".sub").innerHTML =
                "Saved: " + esc(d.path) + "<br>" +
                d.width + "x" + d.height +
                " · color " + (d.dominant_color || "—") +
                " · brightness " + (d.average_brightness != null ? d.average_brightness : "—");
        } else {
            const msg = (r.data && (r.data.error || r.data.message)) || "Failed.";
            box.querySelector(".sub").textContent = "Screenshot failed: " + msg;
        }
    });

    $on("desktop-mouse-pos-btn", async () => {
        const r = await apiFetch("/desktop/mouse/position");
        const el = $("desktop-mouse-pos");
        if (r.status === 200 && r.data && r.data.detail) {
            el.textContent = "Position: " + r.data.detail.x + ", " + r.data.detail.y;
        } else {
            el.textContent = "Mouse: " + ((r.data && r.data.error) || "not available");
        }
    });

    $on("desktop-mouse-move-btn", async () => {
        const x = parseInt($("desktop-mouse-x").value || "0", 10);
        const y = parseInt($("desktop-mouse-y").value || "0", 10);
        if (!confirm("Move mouse to " + x + "," + y + "?")) return;
        await apiFetch("/desktop/mouse/move", { method: "POST", body: { x, y } });
        $on("desktop-mouse-pos-btn", function() {})(); // noop — but read position after
        const r = await apiFetch("/desktop/mouse/position");
        const el = $("desktop-mouse-pos");
        if (r.status === 200 && r.data && r.data.detail) {
            el.textContent = "Position (after move): " + r.data.detail.x + ", " + r.data.detail.y;
        }
    });

    $on("desktop-mouse-click-btn", async () => {
        const x = parseInt($("desktop-mouse-x").value || "0", 10);
        const y = parseInt($("desktop-mouse-y").value || "0", 10);
        if (!confirm("Click at " + x + "," + y + "?")) return;
        await apiFetch("/desktop/mouse/click", { method: "POST", body: { x, y, button: "left" } });
    });

    $on("desktop-mouse-dblclick-btn", async () => {
        const x = parseInt($("desktop-mouse-x").value || "0", 10);
        const y = parseInt($("desktop-mouse-y").value || "0", 10);
        if (!confirm("Double-click at " + x + "," + y + "?")) return;
        await apiFetch("/desktop/mouse/double-click", { method: "POST", body: { x, y, button: "left" } });
    });

    $on("desktop-mouse-scroll-btn", async () => {
        const amount = parseInt($("desktop-mouse-scroll").value || "0", 10);
        if (!confirm("Scroll wheel " + amount + " clicks?")) return;
        await apiFetch("/desktop/mouse/scroll", { method: "POST", body: { amount } });
    });

    $on("desktop-kb-type-btn", async () => {
        const text = $("desktop-kb-type").value;
        if (!text) return;
        if (!confirm("Type this text? The focused window will receive keystrokes.")) return;
        await apiFetch("/desktop/keyboard/type", { method: "POST", body: { text } });
    });

    $on("desktop-kb-press-btn", async () => {
        const key = $("desktop-kb-key").value.trim();
        if (!key) return;
        if (!confirm("Press key '" + key + "'?")) return;
        await apiFetch("/desktop/keyboard/press", { method: "POST", body: { key } });
    });

    $on("desktop-kb-hotkey-btn", async () => {
        const raw = $("desktop-kb-hotkey").value.trim();
        if (!raw) return;
        const keys = raw.split(",").map(function (s) { return s.trim(); }).filter(Boolean);
        if (!keys.length) return;
        if (!confirm("Press hotkey " + keys.join("+") + "?")) return;
        await apiFetch("/desktop/keyboard/hotkey", { method: "POST", body: { keys } });
    });

    $on("desktop-clip-read-btn", async () => {
        const r = await apiFetch("/desktop/clipboard");
        const el = $("desktop-clip-content");
        if (r.status === 200 && r.data && r.data.ok !== false) {
            el.textContent = r.data.content || "(empty)";
        } else {
            el.textContent = "Clipboard: " + ((r.data && r.data.error) || "not available");
        }
    });

    $on("desktop-clip-write-btn", async () => {
        const text = $("desktop-clip-write").value;
        if (!confirm("Replace clipboard contents?")) return;
        await apiFetch("/desktop/clipboard", { method: "POST", body: { text } });
    });

    $on("desktop-apps-list-btn", async () => {
        const r = await apiFetch("/desktop/processes?limit=30");
        const el = $("desktop-apps-list");
        if (r.status === 200 && r.data && r.data.ok !== false) {
            const procs = r.data.processes || [];
            el.innerHTML = procs.length
                ? procs.map(function (p) {
                    return esc(p.name) + " <span class='hint'>(pid " + p.pid + " · " + esc(p.status) + ")</span>";
                }).join("<br>")
                : "No processes returned.";
        } else {
            el.textContent = "Process list: " + ((r.data && r.data.error) || "not available");
        }
    });

    $on("desktop-apps-find-btn", async () => {
        const q = $("desktop-apps-find").value.trim();
        if (!q) return;
        const r = await apiFetch("/desktop/processes/find", { method: "POST", body: { name: q } });
        const el = $("desktop-apps-list");
        if (r.status === 200 && r.data && r.data.ok !== false) {
            const procs = r.data.processes || [];
            el.innerHTML = procs.length
                ? procs.map(function (p) {
                    return esc(p.name) + " <span class='hint'>(pid " + p.pid + " · " + esc(p.status) + ")</span>";
                }).join("<br>")
                : "No matching processes found.";
        } else {
            el.textContent = "Find failed: " + ((r.data && r.data.error) || "not available");
        }
    });

    // ---- Windows ----

    $on("desktop-windows-list-btn", () => loadWindowsInto($("desktop-windows-list")));

    $on("desktop-windows-find-btn", () => loadWindowsInto($("desktop-windows-list"), $("desktop-windows-find").value.trim()));

    $on("desktop-windows-stream-btn", () => {
        if (desktopStreamActive) {
            stopDesktopStream();
            return;
        }
        startDesktopStream($("desktop-windows-list"));
    });

    $on("desktop-monitors-btn", () => loadMonitorsInto($("desktop-monitors-list")));

    $on("desktop-hotkey-add-btn", async () => {
        const name = $("desktop-hotkey-name").value.trim();
        const combo = $("desktop-hotkey-combo").value.trim();
        if (!name || !combo) return;
        const keys = combo.split(",").map(function (s) { return s.trim(); }).filter(Boolean);
        if (!confirm("Register global hotkey " + name + " = " + keys.join("+") + "?")) return;
        const r = await apiFetch("/desktop/hotkeys", { method: "POST", body: { name, combo: keys, enabled: true } });
        if (r.data && r.data.ok === false) {
            alert("Register failed: " + (r.data.error || "unknown"));
        } else {
            $("desktop-hotkey-name").value = "";
            $("desktop-hotkey-combo").value = "";
        }
        loadHotkeysInto($("desktop-hotkeys-list"));
    });

    // Auto-populate the new sections on render.
    loadWindowsInto($("desktop-windows-list"));
    loadMonitorsInto($("desktop-monitors-list"));
    loadHotkeysInto($("desktop-hotkeys-list"));

    bindDesktopButtonClicks();
}


// ---- Desktop windows / monitors / hotkeys helpers ----

let desktopStreamActive = false;
let desktopStreamAbort = null;

// Event delegation: one document-level handler covers every Focus/Close/Remove
// button rendered at any time — including rows streamed in fresh from SSE —
// so no re-binding is ever needed after a render.
function bindDesktopButtonClicks() {
    if (bindDesktopButtonClicks._done) return;
    bindDesktopButtonClicks._done = true;

    document.addEventListener("click", async (ev) => {
        const t = ev.target;
        const btn = t && t.closest ? t.closest("button[data-window-focus], button[data-window-close], button[data-hotkey-remove]") : null;
        if (!btn) return;

        if (btn.hasAttribute("data-window-focus")) {
            const hwnd = parseInt(btn.getAttribute("data-window-focus"), 10);
            if (!confirm("Bring window " + hwnd + " to the foreground?")) return;
            const r = await apiFetch("/desktop/windows/focus", { method: "POST", body: { hwnd } });
            alert(r.data && r.data.ok === false ? "Focus failed: " + (r.data.error || "unknown") : "Focused window " + hwnd);
        } else if (btn.hasAttribute("data-window-close")) {
            const hwnd = parseInt(btn.getAttribute("data-window-close"), 10);
            if (!confirm("Request a graceful close of window " + hwnd + "?")) return;
            const r = await apiFetch("/desktop/windows/close", { method: "POST", body: { hwnd } });
            alert(r.data && r.data.ok === false ? "Close failed: " + (r.data.error || "unknown") : "Close requested for " + hwnd);
        } else if (btn.hasAttribute("data-hotkey-remove")) {
            const name = btn.getAttribute("data-hotkey-remove");
            if (!confirm("Remove global hotkey '" + name + "'?")) return;
            await apiFetch("/desktop/hotkeys/" + encodeURIComponent(name), { method: "DELETE" });
            loadHotkeysInto($("desktop-hotkeys-list"));
        }
    });
}

function renderWindowRow(win) {
    return '<div class="sub" style="margin-bottom:4px">' + esc(win.title) +
        " <span class='hint'>(" + win.pid + " · " + win.width + "x" + win.height +
        (win.visible ? " · visible" : " · hidden") + ")</span>" +
        '<div class="actions">' +
        '<button class="btn small secondary" data-window-focus="' + win.hwnd + '">Focus</button>' +
        '<button class="btn small secondary" data-window-close="' + win.hwnd + '">Close</button>' +
        "</div></div>";
}

function renderMonitorRow(m) {
    return esc(m.width) + "x" + esc(m.height) +
        " at (" + esc(m.x) + "," + esc(m.y) + ")" +
        (m.is_primary ? " <span class='ok-text'>primary</span>" : "") +
        " <span class='hint'>#" + m.index + "</span>";
}

function renderHotkeyRow(h) {
    return esc(h.name) + " = <b>" + esc((h.combo || []).join("+")) + "</b>" +
        (h.enabled ? "" : " <span class='hint'>(disabled)</span>") +
        " <span class='hint'>fired " + h.fire_count + (h.last_fired_at ? " · last " + new Date(h.last_fired_at).toLocaleTimeString() : "") + "</span>" +
        '<div class="actions"><button class="btn small secondary" data-hotkey-remove="' + esc(h.name) + '">Remove</button></div>';
}

async function loadWindowsInto(el, filter) {
    if (!el) return;
    const r = await apiFetch("/desktop/windows?limit=40");
    if (r.status === 200 && r.data && r.data.ok !== false) {
        let wins = r.data.windows || [];
        if (filter) {
            const needle = filter.toLowerCase();
            wins = wins.filter(function (w) { return w.title.toLowerCase().indexOf(needle) !== -1; });
        }
        el.innerHTML = wins.length
            ? wins.map(renderWindowRow).join("")
            : (filter ? "No matching windows." : "No windows returned.");
        const badge = winCountEl();
        if (badge) badge.textContent = wins.length + " window" + (wins.length === 1 ? "" : "s");
    } else {
        el.textContent = "Windows: " + ((r.data && r.data.error) || "not available");
    }
}

function winCountEl() {
    const row = document.querySelector('[data-win-count]');
    return row ? row.querySelector('[data-count]') : null;
}

async function loadMonitorsInto(el) {
    if (!el) return;
    const r = await apiFetch("/desktop/monitors");
    if (r.status === 200 && r.data && r.data.ok !== false) {
        const monitors = r.data.monitors || [];
        el.innerHTML = monitors.length
            ? monitors.map(renderMonitorRow).join("<br>")
            : "No monitors returned.";
    } else {
        el.textContent = "Monitors: " + ((r.data && r.data.error) || "not available");
    }
}

async function loadHotkeysInto(el) {
    if (!el) return;
    const r = await apiFetch("/desktop/hotkeys");
    if (r.status === 200 && r.data && r.data.ok !== false) {
        const hotkeys = r.data.hotkeys || [];
        el.innerHTML = hotkeys.length
            ? hotkeys.map(renderHotkeyRow).join("")
            : "No global hotkeys registered.";
    } else {
        el.textContent = "Hotkeys: " + ((r.data && r.data.error) || "not available");
    }
}

function startDesktopStream(el) {
    const headers = { "Content-Type": "application/json" };
    const key = sessionStorage.getItem("beru_api_key");
    if (key) headers["X-API-Key"] = key;
    desktopStreamActive = true;
    desktopStreamAbort = new AbortController();
    const btn = $("desktop-windows-stream-btn");
    if (btn) btn.textContent = "Stop live view";
    el.innerHTML = "Streaming window snapshots...";
    if (btn) btn.classList.add("danger");
    fetch(API_BASE + "/desktop/windows/stream", {
        headers,
        credentials: "same-origin",
        signal: desktopStreamAbort.signal,
    })
        .then(async (resp) => {
            if (!resp.ok || !resp.body) throw new Error("Stream unavailable: " + resp.status);
            const reader = resp.body.getReader();
            const decoder = new TextDecoder();
            let buffer = "";
            for (;;) {
                const { done, value } = await reader.read();
                if (done) break;
                buffer += decoder.decode(value, { stream: true });
                let idx;
                while ((idx = buffer.indexOf("\n\n")) !== -1) {
                    const block = buffer.slice(0, idx);
                    buffer = buffer.slice(idx + 2);
                    const dataLine = block.split("\n").find(function (l) { return l.indexOf("data:") === 0; });
                    if (!dataLine) continue;
                    let payload = null;
                    try { payload = JSON.parse(dataLine.slice(5)); } catch (e) { continue; }
                    if (payload && payload.windows) {
                        el.innerHTML = payload.windows.map(renderWindowRow).join("");
                        const badge = winCountEl();
                        if (badge) badge.textContent = payload.windows.length + " window" + (payload.windows.length === 1 ? "" : "s") + " (live)";
                    }
                }
            }
        })
        .catch((err) => {
            if (el && !desktopStreamActive) return;
            el.innerHTML = "Stream ended: " + err.message;
        })
        .finally(() => {
            desktopStreamActive = false;
            if (btn) { btn.textContent = "Live view"; btn.classList.remove("danger"); }
        });
}

function stopDesktopStream() {
    if (desktopStreamAbort) desktopStreamAbort.abort();
    desktopStreamActive = false;
    const btn = $("desktop-windows-stream-btn");
    if (btn) { btn.textContent = "Live view"; btn.classList.remove("danger"); }
    const el = $("desktop-windows-list");
    if (el) loadWindowsInto(el);
}


// ---- Browser control panel ----

let browserStatus = { available: false };
let browserPages = [];
let browserActivePage = null;

async function loadBrowserStatus() {
    const r = await apiFetch("/browser/status");
    if (r.status === 200) browserStatus = r.data || { available: false };
}

async function loadBrowserPages() {
    const r = await apiFetch("/browser/pages");
    if (r.status === 200 && r.data && r.data.ok !== false) {
        browserPages = r.data.pages || [];
        browserActivePage = r.data.active_page || null;
    }
}

function _badge(label, ok) {
    return '<span class="' + (ok ? "ok-text" : "bad-text") + '">' + esc(label) + (ok ? " available" : " unavailable") + "</span>";
}

function renderBrowser() {
    const host = $("tab-browser");
    const av = browserStatus;
    const statusHtml =
        '<div class="panel-title">Capabilities</div>' +
        '<div class="item" style="display:flex;flex-wrap:wrap;gap:8px">' +
        _badge("Browser", !!av.available) +
        (av.available && av.pages !== undefined
            ? '<span class="sub" style="align-self:center">' + esc(av.mode || "headless") + " · " +
              av.pages + " page(s) · " +
              (av.blocked_globs || 0) + " blocked glob(s) · " +
              (av.redirects || 0) + " redirect(s) · " +
              (av.fulfills || 0) + " fulfil(s)</span>"
            : "") +
        "</div>";

    const active = browserPages.find((p) => p.id === browserActivePage) || browserPages[0] || null;
    const activeHtml = active
        ? '<div class="item"><div class="head"><span class="title">' + esc(active.title || "Untitled") + '</span></div>' +
          '<div class="sub">' + esc(active.url) + " · " + esc(active.status) + '</div></div>'
        : '<div class="item"><div class="sub">No pages yet. Create one below.</div></div>';

    const pagesHtml =
        '<div class="panel-title">Pages</div>' +
        '<div class="item" id="browser-pages">' +
        (browserPages.length
            ? browserPages.map(function (p, i) {
                const mark = p.id === browserActivePage
                    ? ' <span class="ok-text">[active]</span>'
                    : "";
                return '<div class="sub" style="margin-bottom:4px">' + esc(p.title || "Untitled") + " — " +
                    esc(p.url) + mark +
                    '<div class="actions">' +
                    '<button class="btn small secondary" data-browser-activate="' + esc(p.id) + '">Activate</button>' +
                    '<button class="btn small secondary" data-browser-close="' + esc(p.id) + '">Close</button>' +
                    "</div></div>";
            }).join("")
            : '<div class="sub">No pages.</div>') +
        '<div class="form-row" style="margin-top:6px"><label>New page URL</label><input id="browser-new-url" placeholder="https://example.com"></div>' +
        '<div class="actions">' +
        '<button class="btn small" id="browser-new-page-btn">New page</button>' +
        '<button class="btn small secondary" id="browser-close-all-btn">Close browser</button>' +
        "</div>" +
        "</div>";

    const navHtml =
        '<div class="panel-title">Navigate</div>' +
        '<div class="item">' +
        '<div class="form-row"><label>URL</label><input id="browser-nav-url" placeholder="https://example.com"></div>' +
        '<div class="actions">' +
        '<button class="btn small" id="browser-nav-btn">Go</button>' +
        '<button class="btn small secondary" id="browser-back-btn">Back</button>' +
        '<button class="btn small secondary" id="browser-forward-btn">Forward</button>' +
        '<button class="btn small secondary" id="browser-refresh-btn">Refresh</button>' +
        '<button class="btn small secondary" id="browser-info-btn">Info</button>' +
        "</div></div>";

    const interactHtml =
        '<div class="panel-title">Interact</div>' +
        '<div class="item">' +
        '<div class="form-row"><label>Frame</label><input id="browser-frame" placeholder="name / URL / index (optional)"></div>' +
        '<div class="form-row"><label>Click selector</label><input id="browser-click-sel" placeholder="#submit"></div>' +
        '<div class="actions"><button class="btn small" id="browser-click-btn">Click</button></div>' +
        '<div class="form-row"><label>Type selector</label><input id="browser-type-sel" placeholder="#email"></div>' +
        '<div class="form-row"><label>Text</label><input id="browser-type-text" placeholder="text to type"></div>' +
        '<div class="actions"><button class="btn small secondary" id="browser-type-btn">Type</button></div>' +
        '<div class="form-row"><label>Scroll</label>' +
        '<select id="browser-scroll-dir"><option value="down">Down</option><option value="up">Up</option></select>' +
        '<input id="browser-scroll-amount" type="number" value="400" style="width:80px;margin-left:6px"></div>' +
        '<div class="actions"><button class="btn small secondary" id="browser-scroll-btn">Scroll</button></div>' +
        '<div class="form-row"><label>Scroll to selector</label><input id="browser-scrollto-sel" placeholder="#section"></div>' +
        '<div class="actions"><button class="btn small secondary" id="browser-scrollto-btn">Scroll to</button></div>' +
        '<div class="form-row"><label>Press key</label>' +
        '<input id="browser-press-key" placeholder="Enter" style="width:80px">' +
        '<input id="browser-press-sel" placeholder="selector (optional)" style="flex:1;margin-left:6px"></div>' +
        '<div class="actions"><button class="btn small secondary" id="browser-press-btn">Press</button></div>' +
        '<div class="form-row"><label>Select selector</label><input id="browser-select-sel" placeholder="#country"></div>' +
        '<div class="form-row"><label>Value</label><input id="browser-select-value" placeholder="option value" style="flex:1"></div>' +
        '<div class="form-row"><label>Label</label><input id="browser-select-label" placeholder="option label" style="flex:1"></div>' +
        '<div class="form-row"><label>Index</label><input id="browser-select-index" type="number" placeholder="0" style="width:80px"></div>' +
        '<div class="actions"><button class="btn small secondary" id="browser-select-btn">Select</button></div>' +
        "</div>";

    const readHtml =
        '<div class="panel-title">Read</div>' +
        '<div class="item">' +
        '<div class="form-row"><label>Extract selector (default body)</label><input id="browser-extract-sel" placeholder="body"></div>' +
        '<div class="actions">' +
        '<button class="btn small" id="browser-extract-btn">Extract text</button>' +
        '<button class="btn small secondary" id="browser-shot-btn">Screenshot</button>' +
        "</div>" +
        '<div class="form-row"><label><input id="browser-shot-full" type="checkbox"> Full page</label></div>' +
        '<div class="sub" id="browser-read-out">Nothing read yet.</div>' +
        "</div>";

    const deepHtml =
        '<div class="panel-title">Deep pages</div>' +
        '<div class="item">' +
        '<div class="form-row"><label>Drag source</label><input id="browser-drag-src" placeholder="#item"></div>' +
        '<div class="form-row"><label>Drop target</label><input id="browser-drag-tgt" placeholder="#zone"></div>' +
        '<div class="actions"><button class="btn small secondary" id="browser-drag-btn">Drag</button></div>' +
        '<div class="form-row"><label>Download trigger</label><input id="browser-download-sel" placeholder="#dl-link"></div>' +
        '<div class="actions"><button class="btn small secondary" id="browser-download-btn">Download</button></div>' +
        '<div class="form-row"><label>Upload input selector</label><input id="browser-upload-sel" placeholder="#file-input"></div>' +
        '<div class="form-row"><label>File path(s)</label><input id="browser-upload-paths" placeholder="C:\\a.txt, C:\\b.txt"></div>' +
        '<div class="actions"><button class="btn small secondary" id="browser-upload-btn">Upload</button></div>' +
        '<div class="form-row"><label>Cookies</label>' +
        '<select id="browser-cookie-sub"><option value="get">Get</option><option value="set">Set</option><option value="clear">Clear</option></select>' +
        '<input id="browser-cookie-json" placeholder="cookies JSON (for set)" style="flex:1;margin-left:6px"></div>' +
        '<div class="actions"><button class="btn small secondary" id="browser-cookie-btn">Cookies</button></div>' +
        '<div class="form-row"><label>Storage</label>' +
        '<select id="browser-storage-sub"><option value="get">Get</option><option value="set">Set</option><option value="clear">Clear</option></select>' +
        '<input id="browser-storage-key" placeholder="key" style="flex:1;margin-left:6px"></div>' +
        '<div class="form-row"><label>Storage value (for set)</label><input id="browser-storage-value"></div>' +
        '<div class="actions"><button class="btn small secondary" id="browser-storage-btn">Storage</button></div>' +
        '<div class="form-row"><label>Block URL glob</label><input id="browser-block-glob" placeholder="*tracking*"></div>' +
        '<div class="form-row"><label>Redirect URL glob</label><input id="browser-redirect-glob" placeholder="*legacy*"></div>' +
        '<div class="form-row"><label>Redirect to</label><input id="browser-redirect-url" placeholder="https://new.example.com"></div>' +
        '<div class="form-row"><label>Fulfill glob</label><input id="browser-fulfill-glob" placeholder="*/api/data*"></div>' +
        '<div class="form-row"><label>Fulfill status</label><input id="browser-fulfill-status" type="number" value="200" style="width:80px">' +
        '<input id="browser-fulfill-type" placeholder="application/json" style="flex:1;margin-left:6px"></div>' +
        '<div class="form-row"><label>Fulfill body</label><input id="browser-fulfill-body" placeholder="{\"ok\": true}"></div>' +
        '<div class="form-row"><label>Fulfill headers</label><input id="browser-fulfill-headers" placeholder="X-Rate-Limit: 100"></div>' +
        '<div class="actions"><button class="btn small secondary" id="browser-block-btn">Block glob</button>' +
        '<button class="btn small secondary" id="browser-redirect-btn">Redirect</button>' +
        '<button class="btn small secondary" id="browser-fulfill-btn">Fulfill</button>' +
        '<button class="btn small secondary" id="browser-network-btn">Network log</button>' +
        '<button class="btn small secondary" id="browser-console-btn">Console</button>' +
        '<button class="btn small secondary" id="browser-downloads-btn">Downloads</button></div>' +
        '<div class="form-row"><label>Snapshot name</label><input id="browser-snapshot-name" placeholder="session"></div>' +
        '<div class="actions"><button class="btn small secondary" id="browser-snapshot-btn">Save snapshot</button>' +
        '<button class="btn small secondary" id="browser-restore-btn">Restore snapshot</button></div>' +
        '<div class="form-row"><label><input id="browser-launch-headless" type="checkbox" checked> Headless</label>' +
        '<input id="browser-launch-w" type="number" placeholder="width" value="1280" style="width:90px;margin-left:6px">' +
        '<input id="browser-launch-h" type="number" placeholder="height" value="800" style="width:90px;margin-left:6px"></div>' +
        '<div class="form-row"><label><input id="browser-launch-mobile" type="checkbox"> Mobile</label>' +
        '<label style="margin-left:8px"><input id="browser-launch-touch" type="checkbox"> Touch</label>' +
        '<input id="browser-launch-scale" type="number" step="0.5" min="0.5" placeholder="dpr e.g. 2" style="width:90px;margin-left:6px">' +
        '<input id="browser-launch-tz" placeholder="timezone e.g. Europe/Paris" style="flex:1;margin-left:6px"></div>' +
        '<div class="actions"><button class="btn small secondary" id="browser-launch-btn">Relaunch browser</button></div>' +
        '<div class="actions"><button class="btn small secondary" id="browser-clear-downloads-btn">Clear downloads</button></div>' +
        "</div>";

    host.innerHTML = statusHtml + activeHtml + pagesHtml + navHtml + interactHtml + readHtml + deepHtml +
        '<div class="item" id="browser-log" style="display:none"></div>';

    const $on = (sel, fn) => { const el = $(sel); if (el) el.addEventListener("click", fn); };

    function withFrame(body) {
        const f = $("browser-frame");
        if (f && f.value.trim()) body.frame = f.value.trim();
        return body;
    }

    function log(msg) {
        const el = $("browser-log");
        if (!el) return;
        el.style.display = "block";
        el.textContent = msg;
        el.scrollIntoView({ block: "nearest" });
    }

    $on("browser-new-page-btn", async () => {
        const url = $("browser-new-url").value.trim() || "about:blank";
        const r = await apiFetch("/browser/pages", { method: "POST", body: { url } });
        if (r.status === 201 || r.status === 200) {
            log("Page created.");
        } else {
            log("Create failed: " + ((r.data && (r.data.error && r.data.error.message)) || "unknown"));
        }
        await loadBrowserPages();
        renderBrowser();
    });

    document.querySelectorAll("[data-browser-activate]").forEach((btn) => {
        btn.addEventListener("click", async () => {
            const id = btn.getAttribute("data-browser-activate");
            await apiFetch("/browser/pages/" + id + "/activate", { method: "POST" });
            await loadBrowserPages();
            renderBrowser();
        });
    });

    document.querySelectorAll("[data-browser-close]").forEach((btn) => {
        btn.addEventListener("click", async () => {
            const id = btn.getAttribute("data-browser-close");
            if (!confirm("Close this browser tab?")) return;
            await apiFetch("/browser/pages/" + id, { method: "DELETE" });
            await loadBrowserPages();
            renderBrowser();
        });
    });

    $on("browser-close-all-btn", async () => {
        if (!confirm("Close the entire browser session? All tabs will be closed.")) return;
        await apiFetch("/browser/close", { method: "POST" });
        log("Browser session closed.");
        await loadBrowserPages();
        renderBrowser();
    });

    $on("browser-nav-btn", async () => {
        const url = $("browser-nav-url").value.trim();
        if (!url) return;
        const r = await apiFetch("/browser/action", { method: "POST", body: { action: "navigate", url } });
        log(r.data && r.data.error
            ? "Navigate: " + (r.data.error.message || r.data.error)
            : "Navigated to " + url);
        await loadBrowserPages();
        renderBrowser();
    });

    $on("browser-back-btn", async () => {
        const r = await apiFetch("/browser/action", { method: "POST", body: { action: "back" } });
        log(r.data && r.data.error ? "Back failed." : "Went back.");
        await loadBrowserPages();
        renderBrowser();
    });

    $on("browser-forward-btn", async () => {
        const r = await apiFetch("/browser/action", { method: "POST", body: { action: "forward" } });
        log(r.data && r.data.error ? "Forward failed." : "Went forward.");
        await loadBrowserPages();
        renderBrowser();
    });

    $on("browser-refresh-btn", async () => {
        const r = await apiFetch("/browser/action", { method: "POST", body: { action: "refresh" } });
        log(r.data && r.data.error ? "Refresh failed." : "Refreshed.");
        await loadBrowserPages();
        renderBrowser();
    });

    $on("browser-click-btn", async () => {
        const selector = $("browser-click-sel").value.trim();
        if (!selector) return;
        const r = await apiFetch("/browser/action", { method: "POST", body: withFrame({ action: "click", selector }) });
        log(r.data && r.data.error ? "Click failed: " + (r.data.error.message || r.data.error) : "Clicked " + selector);
    });

    $on("browser-type-btn", async () => {
        const selector = $("browser-type-sel").value.trim();
        const text = $("browser-type-text").value;
        if (!selector) return;
        const r = await apiFetch("/browser/action", { method: "POST", body: withFrame({ action: "type", selector, text }) });
        log(r.data && r.data.error ? "Type failed: " + (r.data.error.message || r.data.error) : "Typed into " + selector);
    });

    $on("browser-scroll-btn", async () => {
        const direction = $("browser-scroll-dir").value;
        const amount = parseInt($("browser-scroll-amount").value || "0", 10);
        await apiFetch("/browser/action", { method: "POST", body: { action: "scroll", direction, amount } });
        log("Scrolled " + direction + " " + amount);
    });

    $on("browser-scrollto-btn", async () => {
        const selector = $("browser-scrollto-sel").value.trim();
        if (!selector) return;
        const r = await apiFetch("/browser/action", { method: "POST", body: withFrame({ action: "scroll_to", selector }) });
        log(r.data && r.data.error ? "Scroll failed: " + (r.data.error.message || r.data.error) : "Scrolled to " + selector);
    });

    $on("browser-press-btn", async () => {
        const key = $("browser-press-key").value.trim();
        if (!key) return;
        const body = withFrame({ action: "press", key });
        const sel = $("browser-press-sel").value.trim();
        if (sel) body.selector = sel;
        const r = await apiFetch("/browser/action", { method: "POST", body });
        log(r.data && r.data.error ? "Press failed: " + (r.data.error.message || r.data.error) : "Pressed " + key);
    });

    $on("browser-info-btn", async () => {
        const r = await apiFetch("/browser/action", { method: "POST", body: { action: "page_info" } });
        if (r.status === 200 && r.data && r.data.ok !== false && r.data.data) {
            const d = r.data.data;
            let msg = "Title: " + (d.title || "(none)") + " | URL: " + (d.url || "(none)");
            if (d.interactive && d.interactive.length) {
                msg += " | elements: " + d.interactive.length;
            }
            if (d.links && d.links.length) {
                msg += " | links: " + d.links.length;
            }
            log(msg);
        } else {
            log("Info failed: " + ((r.data && (r.data.error && r.data.error.message)) || "unknown"));
        }
    });

    $on("browser-select-btn", async () => {
        const selector = $("browser-select-sel").value.trim();
        if (!selector) return;
        const body = { action: "select", selector };
        const value = $("browser-select-value").value.trim();
        const label = $("browser-select-label").value.trim();
        const index = $("browser-select-index").value.trim();
        let picked = false;
        if (value) { body.value = value; picked = true; }
        else if (label) { body.label = label; picked = true; }
        else if (index !== "") { body.index = parseInt(index, 10); picked = true; }
        if (!picked) { log("Select requires a value, label, or index."); return; }
        const r = await apiFetch("/browser/action", { method: "POST", body });
        log(r.data && r.data.error ? "Select failed: " + (r.data.error.message || r.data.error) : "Selected in " + selector);
    });

    $on("browser-extract-btn", async () => {
        const selector = $("browser-extract-sel").value.trim() || "body";
        const r = await apiFetch("/browser/action", { method: "POST", body: withFrame({ action: "extract_text", selector }) });
        const out = $("browser-read-out");
        if (r.status === 200 && r.data && r.data.ok !== false && r.data.data) {
            const text = r.data.data.text || "(empty)";
            out.textContent = text;
        } else {
            out.textContent = "Extract failed: " + ((r.data && (r.data.error && r.data.error.message)) || "unknown");
        }
    });

    $on("browser-shot-btn", async () => {
        const fullPage = !!(($("browser-shot-full") || {}).checked);
        const r = await apiFetch("/browser/action", { method: "POST", body: { action: "screenshot", full_page: fullPage } });
        const out = $("browser-read-out");
        if (r.status === 200 && r.data && r.data.ok !== false && r.data.data) {
            const d = r.data.data;
            out.innerHTML = "Screenshot saved: " + esc(d.path) +
                " (" + d.size_bytes + " bytes)";
        } else {
            out.textContent = "Screenshot failed: " + ((r.data && (r.data.error && r.data.error.message)) || "unknown");
        }
    });

    $on("browser-drag-btn", async () => {
        const source = $("browser-drag-src").value.trim();
        const target = $("browser-drag-tgt").value.trim();
        if (!source || !target) { log("Drag requires source and target."); return; }
        const r = await apiFetch("/browser/action", { method: "POST", body: { action: "drag", source, target } });
        log(r.data && r.data.error ? "Drag failed: " + (r.data.error.message || r.data.error) : "Dragged " + source + " onto " + target);
    });

    $on("browser-download-btn", async () => {
        const selector = $("browser-download-sel").value.trim();
        if (!selector) { log("Download requires a trigger selector."); return; }
        const r = await apiFetch("/browser/action", { method: "POST", body: { action: "download", selector } });
        log(r.data && r.data.error ? "Download failed: " + (r.data.error.message || r.data.error)
            : "Downloaded to " + (r.data.data && r.data.data.path));
    });

    $on("browser-upload-btn", async () => {
        const selector = $("browser-upload-sel").value.trim();
        const raw = $("browser-upload-paths").value;
        const paths = raw.split(",").map((s) => s.trim()).filter(Boolean);
        if (!selector || !paths.length) { log("Upload requires a selector and at least one path."); return; }
        const r = await apiFetch("/browser/action", { method: "POST", body: { action: "upload", selector, paths } });
        log(r.data && r.data.error ? "Upload failed: " + (r.data.error.message || r.data.error) : "Uploaded " + paths.length + " file(s)");
    });

    $on("browser-cookie-btn", async () => {
        const sub = $("browser-cookie-sub").value;
        const body = { action: "cookies", subcommand: sub };
        const json = $("browser-cookie-json").value.trim();
        if (json) {
            try { body.cookies = JSON.parse(json); }
            catch (e) { log("Cookie JSON is invalid: " + e.message); return; }
        }
        const r = await apiFetch("/browser/action", { method: "POST", body });
        if (r.status === 200 && r.data && r.data.ok !== false && r.data.data) {
            const d = r.data.data;
            log("Cookies " + sub + ": " + (d.count !== undefined ? d.count + " cookie(s)" : (d.cookie_count !== undefined ? d.cookie_count + " cookie(s) set" : (d.cleared ? "cleared" : "done"))));
        } else {
            log("Cookies failed: " + ((r.data && (r.data.error && r.data.error.message)) || "unknown"));
        }
    });

    $on("browser-storage-btn", async () => {
        const sub = $("browser-storage-sub").value;
        const body = { action: "storage", subcommand: sub };
        const key = $("browser-storage-key").value.trim();
        if (key) body.key = key;
        if (sub === "set") body.value = $("browser-storage-value").value;
        const r = await apiFetch("/browser/action", { method: "POST", body });
        if (r.status === 200 && r.data && r.data.ok !== false && r.data.data) {
            const d = r.data.data;
            log("Storage " + sub + ": " + (d.count !== undefined ? d.count + " entries" : (d.value !== undefined ? "\"" + d.value + "\"" : "done")));
        } else {
            log("Storage failed: " + ((r.data && (r.data.error && r.data.error.message)) || "unknown"));
        }
    });

    $on("browser-block-btn", async () => {
        const glob = $("browser-block-glob").value.trim();
        if (!glob) { log("Block glob required."); return; }
        const r = await apiFetch("/browser/action", { method: "POST", body: { action: "block_url", glob } });
        log(r.data && r.data.error ? "Block failed: " + (r.data.error.message || r.data.error) : "Blocking " + glob);
    });

    $on("browser-network-btn", async () => {
        const r = await apiFetch("/browser/action", { method: "POST", body: { action: "network" } });
        if (r.status === 200 && r.data && r.data.ok !== false && r.data.data) {
            const d = r.data.data;
            const lines = (d.entries || []).slice(0, 20).map((e) =>
            e.status + " " + e.method + " " + e.url +
            (e.size != null ? " (" + e.size + " B)" : "") +
            (e.timing_ms != null ? " [" + e.timing_ms + "ms]" : "")).join("\n");
            log("Network: " + d.count + " entry(ies)" + (lines ? "\n" + lines : "")); 
        } else {
            log("Network failed: " + ((r.data && (r.data.error && r.data.error.message)) || "unknown"));
        }
    });

    $on("browser-console-btn", async () => {
        const r = await apiFetch("/browser/action", { method: "POST", body: { action: "console" } });
        if (r.status === 200 && r.data && r.data.ok !== false && r.data.data) {
            const d = r.data.data;
            const lines = (d.entries || []).slice(0, 30).map((e) => "[" + e.type + "]" + (e.count > 1 ? " x" + e.count : "") + " " + e.text).join("\n");
            log("Console: " + d.count + " entry(ies)" + (lines ? "\n" + lines : ""));
        } else {
            log("Console failed: " + ((r.data && (r.data.error && r.data.error.message)) || "unknown"));
        }
    });

    $on("browser-downloads-btn", async () => {
        const r = await apiFetch("/browser/action", { method: "POST", body: { action: "downloads" } });
        if (r.status === 200 && r.data && r.data.ok !== false && r.data.data) {
            const d = r.data.data;
            const lines = (d.files || []).slice(0, 20).map((f) => f.name + " (" + f.size_bytes + " bytes)").join("\n");
            log("Downloads: " + d.count + " file(s)" + (lines ? "\n" + lines : ""));
        } else {
            log("Downloads failed: " + ((r.data && (r.data.error && r.data.error.message)) || "unknown"));
        }
    });

    $on("browser-redirect-btn", async () => {
        const glob = $("browser-redirect-glob").value.trim();
        const redirect = $("browser-redirect-url").value.trim();
        if (!glob || !redirect) { log("Redirect requires a glob and target URL."); return; }
        const r = await apiFetch("/browser/action", { method: "POST", body: { action: "redirect_url", glob, redirect } });
        log(r.data && r.data.error ? "Redirect failed: " + (r.data.error.message || r.data.error) : "Redirecting " + glob + " → " + redirect);
    });

    $on("browser-fulfill-btn", async () => {
        const glob = $("browser-fulfill-glob").value.trim();
        if (!glob) { log("Fulfill requires a glob."); return; }
        const body = {
            action: "fulfill",
            glob,
            status_code: parseInt($("browser-fulfill-status").value || "200", 10),
            body: $("browser-fulfill-body").value,
            content_type: $("browser-fulfill-type").value.trim()
        };
        const headersText = $("browser-fulfill-headers") ? $("browser-fulfill-headers").value.trim() : "";
        if (headersText) {
            const headers = {};
            headersText.split("\n").forEach((line) => {
                const idx = line.indexOf(":");
                if (idx > 0) headers[line.slice(0, idx).trim()] = line.slice(idx + 1).trim();
            });
            if (Object.keys(headers).length) body.headers = headers;
        }
        const r = await apiFetch("/browser/action", { method: "POST", body });
        log(r.data && r.data.error ? "Fulfill failed: " + (r.data.error.message || r.data.error) : "Fulfilling " + glob);
    });

    $on("browser-launch-btn", async () => {
        const body = {
            action: "launch",
            headless: !!$("browser-launch-headless").checked,
            width: parseInt($("browser-launch-w").value || "0", 10),
            height: parseInt($("browser-launch-h").value || "0", 10)
        };
        if ($("browser-launch-mobile") && $("browser-launch-mobile").checked) body.is_mobile = true;
        if ($("browser-launch-touch") && $("browser-launch-touch").checked) body.has_touch = true;
        const scale = parseFloat($("browser-launch-scale").value || "0");
        if (scale > 0) body.device_scale_factor = scale;
        const tz = $("browser-launch-tz").value.trim();
        if (tz) body.timezone_id = tz;
        if (!confirm("Relaunch the browser with these settings? Current tabs will close.")) return;
        const r = await apiFetch("/browser/action", { method: "POST", body });
        log(r.data && r.data.error ? "Launch failed: " + (r.data.error.message || r.data.error)
            : "Browser relaunched" + (body.headless ? " (headless)" : " (headed)"));
        await loadBrowserStatus();
        await loadBrowserPages();
        renderBrowser();
    });

    $on("browser-clear-downloads-btn", async () => {
        if (!confirm("Delete all files in the browser download directory?")) return;
        const r = await apiFetch("/browser/action", { method: "POST", body: { action: "clear_downloads" } });
        log(r.data && r.data.error ? "Clear failed: " + (r.data.error.message || r.data.error)
            : "Download dir cleared");
        if (r.status === 200 && r.data && r.data.ok !== false && r.data.data) {
            const d = r.data.data;
            log("Cleared " + d.removed + " download file(s)" + (d.failed ? " (" + d.failed + " failed)" : ""));
        }
    });

    $on("browser-snapshot-btn", async () => {
        const name = $("browser-snapshot-name").value.trim() || "session";
        const r = await apiFetch("/browser/action", { method: "POST", body: { action: "snapshot", name } });
        if (r.status === 200 && r.data && r.data.ok !== false && r.data.data) {
            const d = r.data.data;
            log("Snapshot saved: " + d.name + " (" + d.cookies + " cookie(s), " + d.origins + " origin(s))");
        } else {
            log("Snapshot failed: " + ((r.data && (r.data.error && r.data.error.message)) || "unknown"));
        }
    });

    $on("browser-restore-btn", async () => {
        const name = $("browser-snapshot-name").value.trim() || "session";
        if (!confirm("Restore snapshot '" + name + "'? This closes the current session's tabs.")) return;
        const r = await apiFetch("/browser/action", { method: "POST", body: { action: "restore", name } });
        log(r.data && r.data.error ? "Restore failed: " + (r.data.error.message || r.data.error) : "Restored snapshot " + name);
        await loadBrowserPages();
        renderBrowser();
    });
}


// ---- Notifications ----

function renderNotifications() {
    return apiFetch("/scheduler/notifications?limit=50").then((r) => {
        const host = $("tab-notifications");
        if (r.status !== 200) {
            host.innerHTML = '<div class="empty">Notifications unavailable.</div>';
            return;
        }
        const d = r.data || {};
        const items = d.notifications || [];
        const rows = items.map((n) => {
            const pl = n.payload || {};
            const reactor = pl.origin === "reactor";
            let detail = "";
            if (reactor) {
                const st = pl.status || "";
                const stCls = st === "failed" ? "bad-text" : "ok-text";
                detail =
                    '<div class="sub"><span class="' + stCls + '">' + esc(st) + "</span>" +
                    (pl.listened_task_id ? " after " + esc(pl.listened_task_id.slice(0, 8)) : "") +
                    (pl.duration_ms != null ? " · " + Math.round(pl.duration_ms * 100) / 100 + "ms" : "") +
                    (pl.run_count != null ? " · run #" + pl.run_count : "") +
                    (pl.error_groups != null ? " · " + pl.error_groups + " error group(s)" : "") +
                    (pl.error ? " · " + esc(pl.error) : "") + "</div>";
            }
            return '<div class="item"><div class="head"><span class="title">' +
                esc(n.title) + (reactor ? ' <span class="hint">reactor</span>' : "") + "</span>" +
                '<span class="hint">' + esc(n.level || "") + (n.read ? "" : " · unread") + "</span></div>" +
                '<div class="sub">' + esc(n.message) + "</div>" +
                detail +
                '<div class="sub">' + (n.created_at ? fmtTime(n.created_at) : "") + "</div></div>";
        }).join("");
        host.innerHTML =
            '<div class="panel-title">Notifications (' + (d.unread_count || 0) + " unread)</div>" +
            (rows || '<div class="empty">No notifications.</div>') +
            '<div class="actions"><button class="btn small secondary" id="notif-clear">Mark all read</button> ' +
            '<button class="btn small secondary" id="notif-refresh">Refresh</button></div>';
        const clearBtn = $("notif-clear");
        if (clearBtn) clearBtn.addEventListener("click", () => apiFetch("/scheduler/notifications/read-all", { method: "POST" }).then(renderNotifications));
        const refreshBtn = $("notif-refresh");
        if (refreshBtn) refreshBtn.addEventListener("click", renderNotifications);
    });
}

function renderReliability() {
    const host = $("tab-reliability");
    Promise.all([
        apiFetch("/reliability/summary"),
        apiFetch("/reliability/activity?limit=40"),
        apiFetch("/reliability/audit?limit=40"),
    ]).then(function (results) {
        const sum = results[0].status === 200 ? (results[0].data || {}).data || {} : {};
        const act = results[1].status === 200 ? (results[1].data || {}).entries || [] : [];
        const audit = results[2].status === 200 ? (results[2].data || {}).entries || [] : [];

        const uptime = sum.uptime_s != null ? Math.floor(sum.uptime_s / 60) + "m " + Math.floor(sum.uptime_s % 60) + "s" : "—";
        const topTools = (sum.top_tools || []).map(function (kv) {
            return esc(kv[0]) + " (" + kv[1] + ")";
        }).join(", ");

        const summaryHtml =
            '<div class="panel-title">Health summary</div>' +
            '<div class="item">' +
            '<div class="sub">Uptime: ' + esc(uptime) + "</div>" +
            '<div class="sub">Tool calls: <strong>' + (sum.total_tool_calls || 0) + "</strong>" +
            " · success: " + (sum.successes || 0) +
            ' · <span class="bad-text">failed: ' + (sum.failures || 0) + "</span>" +
            " · confirmations: " + (sum.confirmations_pending || 0) + "</div>" +
            '<div class="sub">Approvals: <strong>' + (sum.approvals || 0) + "</strong>" +
            " · Denials: <strong>" + (sum.denials || 0) + "</strong></div>" +
            '<div class="sub">Storage: ' +
            (sum.persisted
                ? '<span class="ok-text">durable (database)</span>'
                : '<span class="warn-text">in-memory only</span>') +
            "</div>" +
            (topTools ? '<div class="sub">Top tools: ' + topTools + "</div>" : "") +
            '<div class="actions"><button class="btn small secondary" id="reliability-refresh">Refresh</button> ' +
            '<button class="btn small secondary" id="reliability-clear">Clear ledger</button></div>' +
            "</div>";

        const actRows = act.map(function (e) {
            const ok = e.outcome === "success";
            const cls = ok ? "ok-text" : (e.outcome === "confirmation_required" ? "warn-text" : "bad-text");
            const ms = e.duration_ms != null ? " · " + Math.round(e.duration_ms) + "ms" : "";
            return '<div class="item"><div class="head"><span class="title">' + esc(e.tool_name) + "</span>" +
                '<span class="' + cls + '">' + esc(e.outcome) + "</span></div>" +
                '<div class="sub">agent=' + esc(e.agent || "") + " · " + esc(e.args_summary || "") + ms + "</div>" +
                (e.error ? '<div class="sub bad-text">' + esc(e.error) + "</div>" : "") +
                '<div class="sub">' + esc(e.timestamp ? new Date(e.timestamp * 1000).toLocaleTimeString() : "") + "</div></div>";
        }).join("");

        const auditRows = audit.map(function (e) {
            const ok = e.decision === "approved";
            const cls = ok ? "ok-text" : "bad-text";
            return '<div class="item"><div class="head"><span class="title">' + esc(e.tool_name) + "</span>" +
                '<span class="' + cls + '">' + esc(e.decision) + "</span></div>" +
                '<div class="sub">agent=' + esc(e.agent || "") + (e.outcome ? " · outcome=" + esc(e.outcome) : "") +
                (e.error ? " · " + esc(e.error) : "") + "</div>" +
                '<div class="sub">' + esc(e.confirmation_id || "") + " · " + esc(e.timestamp ? new Date(e.timestamp * 1000).toLocaleTimeString() : "") + "</div></div>";
        }).join("");

        host.innerHTML =
            summaryHtml +
            '<div class="panel-title">Recent approval decisions</div>' +
            (auditRows || '<div class="empty">No approval decisions recorded yet.</div>') +
            '<div class="panel-title">Recent tool activity</div>' +
            (actRows || '<div class="empty">No tool activity yet.</div>');

        const ref = $("reliability-refresh");
        if (ref) ref.addEventListener("click", renderReliability);
        const clr = $("reliability-clear");
        if (clr) clr.addEventListener("click", async () => {
            if (!confirm("Clear the reliability ledger (in-memory + persisted rows)?")) return;
            await apiFetch("/reliability/activity", { method: "DELETE" });
            renderReliability();
        });
    });
}

async function loadTools() {
    const r = await apiFetch("/tools");
    if (r.status === 200) {
        state.tools = r.data || [];
        renderTools();
        renderQuickStat();
    }
}

async function loadStatus() {
    const r = await apiFetch("/status");
    if (r.status === 200) {
        const d = r.data;
        const providerOk = d.llm_provider !== "mock";
        STATUS.server = { ok: true, label: "online" };
        STATUS.model = {
            ok: providerOk,
            label: providerOk ? (d.llm_model || d.llm_provider) : d.llm_provider + " (mock)",
        };
    } else {
        STATUS.server = { ok: false, label: "offline" };
        STATUS.model = { ok: false, label: "unknown" };
    }
    renderChips();
}

async function loadAgents() {
    const r = await apiFetch("/agents");
    if (r.status === 200 && !state.activeAgent) {
        const agents = r.data || [];
        state.activeAgent = agents.length ? agents[0].name : "beru_core";
        renderSidebarAgents(agents);
    }
}

async function refreshAll() {
    await loadStatus();
    await refreshAuth();
    await Promise.all([loadAgents(), loadTools(), loadLlm()]);
    loadAutomation();
    renderFacts();
    renderPlans();
    renderNotifications();
    await loadDesktopStatus();
    await loadBrowserStatus();
    await loadBrowserPages();
}

let socket = null;
let socketClosedByUser = false;

function wsBase() {
    return (location.protocol === "https:" ? "wss://" : "ws://") + location.host;
}

function connectWebSocket() {
    const clientId = localStorage.getItem("beru_client_id") || (Math.random().toString(36).slice(2) + Date.now().toString(36));
    localStorage.setItem("beru_client_id", clientId);
    STATUS.ws = { ok: false, label: "connecting" };
    renderChips();
    socket = new WebSocket(wsBase() + "/ws/" + clientId);
    socket.addEventListener("open", () => {
        STATUS.ws = { ok: true, label: "connected" };
        renderChips();
    });
    socket.addEventListener("close", (ev) => {
        if (socketClosedByUser || ev.target !== socket) return;
        state.sending = false;
        STATUS.ws = ev.code === 1008 ? { ok: false, label: "auth required" } : { ok: false, label: "disconnected" };
        renderChips();
        renderAuthBanner();
        if (ev.code !== 1008) setTimeout(() => { if (!state.sending && !socketClosedByUser) connectWebSocket(); }, 3000);
    });
    socket.addEventListener("error", () => {
        STATUS.ws = { ok: false, label: "error" };
        renderChips();
    });
    socket.addEventListener("message", (ev) => {
        let msg;
        try {
            msg = JSON.parse(ev.data);
        } catch (err) {
            return;
        }
        handleWsMessage(msg);
    });
}

function reconnect() {
    socketClosedByUser = false;
    if (socket) {
        socketClosedByUser = true;
        try { socket.close(); } catch (err) {}
        socket = null;
    }
    connectWebSocket();
}

function handleWsMessage(msg) {
    if (msg.type === "start") {
        activeStreamEl = appendStreamMessage("assistant", "", msg.agent ? "agent: " + msg.agent : "");
        setTyping(false);
    } else if (msg.type === "delta") {
        if (!activeStreamEl) activeStreamEl = appendStreamMessage("assistant", "");
        activeStreamEl.appendChild(document.createTextNode(msg.text || ""));
        $("chat-area").scrollTop = $("chat-area").scrollHeight;
    } else if (msg.type === "end") {
        setTyping(false);
        state.sending = false;
        if (msg.conversation_id) state.conversationId = msg.conversation_id;
        (msg.pending_confirmations || []).forEach(registerConfirmation);
        renderConfirmations();
        activeStreamEl = null;
    } else if (msg.type === "error") {
        setTyping(false);
        state.sending = false;
        appendMessage("error", msg.error || "Generation failed.");
        activeStreamEl = null;
    } else if (msg.type === "notification") {
        renderNotifications();
    }
}

async function sendMessage() {
    const input = $("message-input");
    const text = input.value.trim();
    if (!text || state.sending) return;
    input.value = "";
    state.sending = true;
    appendMessage("user", text);
    if (!socket || socket.readyState !== WebSocket.OPEN) {
        appendMessage("error", "Not connected to BERU. Check the WebSocket status and try again.");
        state.sending = false;
        return;
    }
    setTyping(true);
    socket.send(JSON.stringify({
        message: text,
        conversation_id: state.conversationId,
        agent: state.activeAgent,
    }));
}

function initTabs() {
    const tabs = $("tabs");
    tabs.addEventListener("click", (e) => {
        const tab = e.target.closest(".tab");
        if (!tab) return;
        document.querySelectorAll(".tab").forEach((t) => t.classList.remove("active"));
        document.querySelectorAll(".tab-content").forEach((t) => t.classList.remove("active"));
        tab.classList.add("active");
        const name = tab.getAttribute("data-tab");
        const content = $("tab-" + name);
        content.classList.add("active");
        if (name === "desktop") { loadDesktopStatus().then(renderDesktop); }
        if (name === "browser") { loadBrowserStatus().then(loadBrowserPages).then(renderBrowser); }
        if (name === "automation") renderTasks();
        if (name === "tools") renderTools();
        if (name === "memory") renderFacts();
        if (name === "plans") renderPlans();
        if (name === "notifications") renderNotifications();
        if (name === "reliability") renderReliability();
        if (name === "settings") loadLlm();
    });
}

function init() {
    attachAuthInputs();
    initTabs();
    const send = () => sendMessage();
    $("send-btn").addEventListener("click", send);
    $("message-input").addEventListener("keydown", (e) => {
        if (e.key === "Enter") send();
    });
    $("new-chat-btn").addEventListener("click", () => {
        state.conversationId = null;
        $("chat-area").innerHTML = "";
    });
    refreshAll();
    connectWebSocket();
    setInterval(() => {
        if (STATUS.ws.ok && !state.sending) loadAutomation();
    }, 30000);
}

document.addEventListener("DOMContentLoaded", init);