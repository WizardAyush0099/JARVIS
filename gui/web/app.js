/* ==========================================================================
   JARVIS web client
   Vanilla JS, no build step, no external requests - it has to work on a Pi and
   on a phone with only the local network.
   ========================================================================== */
(function () {
  "use strict";

  var TOKEN = (function () {
    var fromUrl = new URLSearchParams(window.location.search).get("token");
    if (fromUrl) { try { sessionStorage.setItem("jarvis_token", fromUrl); } catch (e) {} }
    try { return fromUrl || sessionStorage.getItem("jarvis_token") || ""; } catch (e) { return fromUrl || ""; }
  })();

  var $ = function (id) { return document.getElementById(id); };
  /** @param {string} id @returns {HTMLButtonElement} */
  function button(id) { return /** @type {HTMLButtonElement} */ ($(id)); }
  /** @param {string} id @returns {HTMLTextAreaElement} */
  function textarea(id) { return /** @type {HTMLTextAreaElement} */ ($(id)); }
  var state = {
    busy: false,
    pending: null,
    online: false,
    muted: false,
    lastState: "idle",
  };

  // A reply arrives twice when the socket is healthy: once in the HTTP response
  // and once as an event. Ids let us render it exactly once either way.
  var renderedIds = {};

  /** @param {string} id @returns {boolean} true when this id was already shown */
  function alreadyRendered(id) {
    if (!id) return false;
    if (renderedIds[id]) return true;
    renderedIds[id] = true;
    return false;
  }

  /* ---------------------------------------------------------------- utils */
  function escapeHtml(text) {
    return String(text == null ? "" : text)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
  }

  // Minimal, safe formatting: escape first, then add structure.
  function format(text) {
    var out = escapeHtml(text);
    var blocks = [];
    out = out.replace(/```([\s\S]*?)```/g, function (_, code) {
      blocks.push("<pre>" + code.replace(/^\n+|\n+$/g, "") + "</pre>");
      return "\u0000" + (blocks.length - 1) + "\u0000";
    });
    out = out.replace(/`([^`\n]+)`/g, "<code>$1</code>");
    out = out.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
    out = out.replace(/(https?:\/\/[^\s<)]+)/g, '<a href="$1" target="_blank" rel="noopener noreferrer">$1</a>');
    out = out.split(/\n{2,}/).map(function (para) {
      return "<p>" + para.replace(/\n/g, "<br>") + "</p>";
    }).join("");
    out = out.replace(/\u0000(\d+)\u0000/g, function (_, index) { return blocks[Number(index)]; });
    return out;
  }

  function clock() {
    var now = new Date();
    return now.toTimeString().slice(0, 8);
  }

  function toast(message) {
    if (!message) return;
    var el = document.createElement("p");
    el.className = "system-note";
    el.textContent = message;
    return el;
  }

  /* ---------------------------------------------------------------- api */
  function api(path, options) {
    var opts = options || {};
    var headers = { "Content-Type": "application/json" };
    if (TOKEN) headers["X-Jarvis-Token"] = TOKEN;
    return fetch(path, {
      method: opts.method || "GET",
      headers: headers,
      body: opts.body ? JSON.stringify(opts.body) : undefined,
    }).then(function (response) {
      if (response.status === 401) throw new Error("token rejected - reopen the page with the correct ?token=");
      if (!response.ok) {
        return response.json().catch(function () { return {}; }).then(function (data) {
          throw new Error(data.detail || ("request failed (" + response.status + ")"));
        });
      }
      return response.json();
    });
  }

  /* ---------------------------------------------------------------- render */
  function setState(name, note) {
    if (!name) return;
    state.lastState = name;
    document.body.setAttribute("data-state", name);
    var pill = $("pill-state");
    var tone = "idle";
    if (name === "listening") tone = "ok";
    else if (name === "thinking" || name === "working") tone = "busy";
    else if (name === "speaking") tone = "idle";
    else if (name === "error") tone = "warn";
    setPill(pill, note || name, tone);
  }

  function setPill(pill, text, tone) {
    if (!pill) return;
    pill.setAttribute("data-tone", tone || "muted");
    var label = pill.querySelector("span");
    if (label) label.textContent = text;
  }

  function thread() { return $("thread"); }

  function clearThread() {
    thread().innerHTML = "";
  }

  function addMessage(role, text, meta) {
    meta = meta || {};
    if (alreadyRendered(meta.messageId)) return null;
    var wrap = document.createElement("article");
    wrap.className = "msg " + (role === "user" ? "user" : role === "system" ? "system" : "assistant") +
      (meta.error ? " error" : "");

    var body = document.createElement("div");
    body.className = "msg-body";

    if (role !== "system") {
      var head = document.createElement("div");
      head.className = "msg-head";
      var who = document.createElement("span");
      who.className = "who";
      who.textContent = role === "user" ? "you" : (meta.provider ? "jarvis · " + meta.provider : "jarvis");
      head.appendChild(who);
      head.appendChild(document.createTextNode(meta.time || clock()));
      if (role === "assistant") {
        var grow = document.createElement("span");
        grow.className = "grow";
        head.appendChild(grow);
        var copy = document.createElement("button");
        copy.type = "button";
        copy.className = "copy-btn";
        copy.textContent = "copy";
        copy.addEventListener("click", function () {
          copyText(text, copy);
        });
        head.appendChild(copy);
      }
      body.appendChild(head);
    }

    var content = document.createElement("div");
    content.innerHTML = format(text);
    body.appendChild(content);

    (meta.images || []).forEach(function (src) {
      var img = document.createElement("img");
      img.className = "msg-image";
      img.src = src;
      img.alt = "image generated by JARVIS";
      img.loading = "lazy";
      body.appendChild(img);
    });

    if (meta.steps && meta.steps.length) {
      var steps = document.createElement("div");
      steps.className = "steps";
      meta.steps.forEach(function (step) {
        var chip = document.createElement("span");
        chip.className = "step";
        chip.setAttribute("data-ok", step.ok ? "true" : "false");
        chip.textContent = (step.ok ? "✓ " : "✕ ") + step.tool;
        steps.appendChild(chip);
      });
      body.appendChild(steps);
    }

    wrap.appendChild(body);
    thread().appendChild(wrap);
    scrollToEnd();
    return wrap;
  }

  function scrollToEnd() {
    var node = thread();
    node.scrollTop = node.scrollHeight;
  }

  function copyText(text, button) {
    var done = function () {
      if (!button) return;
      var original = button.textContent;
      button.textContent = "copied";
      button.classList.add("done");
      setTimeout(function () {
        button.textContent = original;
        button.classList.remove("done");
      }, 1400);
    };
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text).then(done, function () { fallbackCopy(text, done); });
    } else {
      fallbackCopy(text, done);
    }
  }

  function fallbackCopy(text, done) {
    var area = document.createElement("textarea");
    area.value = text;
    area.setAttribute("readonly", "readonly");
    area.style.position = "fixed";
    area.style.opacity = "0";
    document.body.appendChild(area);
    area.select();
    try { document.execCommand("copy"); done(); } catch (e) { /* clipboard blocked */ }
    document.body.removeChild(area);
  }

  function showTyping(label) {
    removeTyping();
    var wrap = document.createElement("article");
    wrap.className = "msg assistant";
    wrap.id = "typing-indicator";
    var body = document.createElement("div");
    body.className = "msg-body";
    body.innerHTML = '<div class="typing"><i></i><i></i><i></i></div>';
    var caption = document.createElement("span");
    caption.className = "muted small";
    caption.style.marginLeft = "8px";
    caption.textContent = label || "thinking";
    body.appendChild(caption);
    wrap.appendChild(body);
    thread().appendChild(wrap);
    scrollToEnd();
  }

  function removeTyping() {
    var node = $("typing-indicator");
    if (node && node.parentNode) node.parentNode.removeChild(node);
  }

  function setBusy(busy) {
    state.busy = busy;
    button("btn-send").disabled = busy;
    button("btn-mic").disabled = busy;
  }

  /* ---------------------------------------------------------------- confirmation */
  function showConfirmation(pending) {
    state.pending = pending;
    var box = $("confirmation");
    if (!pending) { box.classList.add("hidden"); return; }
    $("confirm-text").textContent = pending.question || "Confirm this action?";
    $("confirm-meta").textContent = "tool: " + (pending.tool || "?");
    box.classList.remove("hidden");
    $("btn-confirm-yes").focus();
  }

  /* ---------------------------------------------------------------- panel */
  function renderProviders(providers) {
    var list = $("provider-list");
    list.innerHTML = "";
    (providers || []).forEach(function (item) {
      var li = document.createElement("li");
      var dot = document.createElement("span");
      var tone = "off";
      if (item.status === "ready") tone = "ok";
      else if (item.status === "cooling" || item.status === "degraded") tone = "busy";
      else if (item.status === "not configured") tone = "off";
      else if (item.status === "error") tone = "warn";
      dot.className = "dot " + tone;
      var name = document.createElement("span");
      name.className = "name";
      name.textContent = item.label || item.slug;
      var meta = document.createElement("span");
      meta.className = "meta";
      var detail = item.model || "";
      if (item.status === "cooling") detail += " · " + Math.round(item.cooldown_s) + "s";
      else if (item.status === "not configured") detail = "add a key";
      else if (item.successes) detail += " · " + item.successes + " ok";
      meta.textContent = detail;
      li.appendChild(dot);
      li.appendChild(name);
      li.appendChild(meta);
      list.appendChild(li);
    });
    if (!list.children.length) {
      var empty = document.createElement("li");
      empty.textContent = "no providers configured";
      list.appendChild(empty);
    }
  }

  function renderMemory(memory, facts) {
    var stats = $("memory-stats");
    stats.innerHTML = "";
    var pairs = [["turns", (memory || {}).turns || 0], ["messages", (memory || {}).messages || 0], ["facts", (memory || {}).facts || 0]];
    pairs.forEach(function (pair) {
      var box = document.createElement("span");
      box.className = "stat";
      box.innerHTML = pair[0] + " <b>" + pair[1] + "</b>";
      stats.appendChild(box);
    });

    var list = $("fact-list");
    list.innerHTML = "";
    var keys = Object.keys(facts || {});
    if (!keys.length) {
      var li = document.createElement("li");
      li.textContent = "nothing remembered yet";
      list.appendChild(li);
      return;
    }
    keys.slice(0, 12).forEach(function (key) {
      var li = document.createElement("li");
      li.textContent = key + ": " + facts[key];
      list.appendChild(li);
    });
  }

  function renderSystem(status) {
    var stats = $("system-stats");
    stats.innerHTML = "";
    var hardware = status.hardware || {};
    var speech = status.speech || {};
    var mic = status.mic || {};
    var items = [
      ["voice", speech.engine || "off"],
      ["mic", mic.engine || "off"],
      ["gpio", hardware.backend || "none"],
      ["tools", status.tools || 0],
    ];
    items.forEach(function (pair) {
      var box = document.createElement("span");
      box.className = "stat";
      box.innerHTML = pair[0] + " <b>" + escapeHtml(pair[1]) + "</b>";
      stats.appendChild(box);
    });
  }

  function renderHardware(hardware) {
    var stats = $("hardware-stats");
    var list = $("device-list");
    hardware = hardware || {};
    var devices = hardware.devices || [];
    stats.innerHTML = "";
    [
      ["backend", hardware.backend || "none"],
      ["devices", String(devices.length)],
      ["mode", hardware.real_hardware ? "live" : "simulated"],
    ].forEach(function (pair) {
      var box = document.createElement("span");
      box.className = "stat";
      box.innerHTML = pair[0] + " <b>" + escapeHtml(pair[1]) + "</b>";
      stats.appendChild(box);
    });

    list.innerHTML = "";
    if (!devices.length) {
      var empty = document.createElement("li");
      empty.textContent = "no devices declared - add them to config/hardware.json";
      list.appendChild(empty);
      return;
    }
    devices.forEach(function (device) {
      var li = document.createElement("li");
      var name = document.createElement("span");
      name.className = "name";
      name.textContent = device.name || device.id;
      var meta = document.createElement("span");
      meta.className = "meta";
      meta.textContent = device.kind + (device.pin === null || device.pin === undefined
        ? "" : " · pin " + device.pin);
      li.appendChild(name);
      li.appendChild(meta);
      list.appendChild(li);
    });
  }

  function renderToolLog(entries) {
    var list = $("tool-log");
    list.innerHTML = "";
    if (!entries || !entries.length) {
      var li = document.createElement("li");
      li.textContent = "no tool activity yet";
      list.appendChild(li);
      return;
    }
    entries.slice().reverse().forEach(function (entry) {
      var li = document.createElement("li");
      li.textContent = (entry.ok ? "✓ " : "✕ ") + entry.tool +
        (entry.elapsed ? " · " + entry.elapsed.toFixed(2) + "s" : "") +
        (entry.ok ? "" : " · " + (entry.error || "").slice(0, 60));
      list.appendChild(li);
    });
  }

  function renderLogs(logs) {
    var list = $("log-list");
    list.innerHTML = "";
    (logs || []).slice(-25).forEach(function (entry) {
      var li = document.createElement("li");
      li.textContent = "[" + entry.time + "] " + entry.level + " " + entry.message;
      if (entry.level === "WARNING") li.style.color = "#ffd9a8";
      if (entry.level === "ERROR") li.style.color = "#ffd0d0";
      list.appendChild(li);
    });
    if (!list.children.length) {
      var empty = document.createElement("li");
      empty.textContent = "quiet so far";
      list.appendChild(empty);
    }
  }

  function renderHistory(messages) {
    clearThread();
    renderedIds = {};
    (messages || []).slice(-40).forEach(function (message) {
      var meta = message.meta || {};
      addMessage(message.role === "assistant" ? "assistant" : message.role === "user" ? "user" : "system",
        message.content, { time: message.time, provider: meta.provider, messageId: meta.message_id });
    });
    if (!messages || !messages.length) seedIntro();
  }

  function seedIntro() {
    addMessage("system", "JARVIS is online and listening for input.");
  }

  function applySnapshot(payload, keepThread) {
    if (!payload) return;
    var identity = payload.identity || {};
    if (identity.assistant) {
      document.title = identity.assistant;
      $("intro-name").textContent = identity.assistant;
      $("brand-sub").textContent = "personal ai · " + (identity.owner || "user");
    }
    var status = payload.status || {};
    setState(status.state || "idle", status.state_note || status.state);
    renderProviders(status.providers);
    renderMemory(status.memory, status.facts);
    renderSystem(status);
    renderHardware(status.hardware);
    renderToolLog(status.recent_tools);
    showConfirmation(status.pending);

    var speech = status.speech || {};
    state.muted = !speech.enabled;
    $("btn-mute").classList.toggle("muted", state.muted);
    setPill($("pill-voice"), speech.available ? (speech.enabled ? "voice" : "muted") : "no voice",
      speech.available ? (speech.enabled ? "ok" : "warn") : "muted");
    var mic = status.mic || {};
    setPill($("pill-mic"), mic.engine || "off", mic.enabled ? "ok" : "muted");
    var providers = status.providers || [];
    var live = providers.filter(function (p) { return p.status === "ready"; });
    setPill($("pill-brain"), live.length ? live[0].slug : "offline", live.length ? "ok" : "warn");

    if (!keepThread) renderHistory(payload.messages);
  }

  function setOnline(online, why) {
    state.online = online;
    var banner = $("offline-banner");
    if (online) {
      banner.classList.add("hidden");
      setBusy(state.busy);
      button("btn-send").disabled = false;
    } else {
      banner.classList.remove("hidden");
      var detail = banner.querySelector("span");
      if (why && detail) detail.textContent = why;
      setPill($("pill-brain"), "offline", "warn");
      button("btn-send").disabled = true;
      button("btn-mic").disabled = true;
    }
  }

  /* ---------------------------------------------------------------- actions */
  function refresh() {
    return api("/api/state").then(function (payload) {
      setOnline(true);
      applySnapshot(payload, true);
      return payload;
    }).catch(function (error) {
      setOnline(false, "This page is served but it cannot reach the JARVIS brain (" + error.message + "). Start it with `python main.py --web`, then reload.");
      throw error;
    });
  }

  function send(text) {
    var message = (text || "").trim();
    if (!message || state.busy) return;
    textarea("input").value = "";
    autoGrow();
    addMessage("user", message, { time: clock() });
    setBusy(true);
    showTyping("thinking");
    setState("thinking", "thinking");

    api("/api/chat", { method: "POST", body: { text: message } })
      .then(function (payload) {
        removeTyping();
        handleReply(payload.reply);
      })
      .catch(function (error) {
        removeTyping();
        addMessage("assistant", "I couldn't reach the brain: " + error.message, { error: true });
        setState("error", "offline");
      })
      .then(function () {
        setBusy(false);
        refresh().catch(function () {});
      });
  }

  function handleReply(reply) {
    if (!reply) return;
    addMessage("assistant", reply.text, {
      time: clock(),
      steps: reply.steps,
      images: reply.images,
      error: reply.error,
      provider: reply.provider,
      messageId: reply.id,
    });
    if (reply.pending) showConfirmation(reply.pending);
  }

  function confirm(approved) {
    if (state.busy) return;
    setBusy(true);
    showTyping(approved ? "running it" : "cancelling");
    api("/api/confirm", { method: "POST", body: { approved: approved } })
      .then(function (payload) {
        removeTyping();
        showConfirmation(null);
        handleReply(payload.reply);
      })
      .catch(function (error) {
        removeTyping();
        addMessage("assistant", "Confirmation failed: " + error.message, { error: true });
      })
      .then(function () {
        setBusy(false);
        refresh().catch(function () {});
      });
  }

  function autoGrow() {
    var input = textarea("input");
    input.style.height = "auto";
    input.style.height = Math.min(input.scrollHeight, 148) + "px";
  }

  /* ---------------------------------------------------------------- microphone */
  var recognition = null;
  var listening = false;

  // The Web Speech API is still vendor-prefixed in several mobile browsers.
  var speechWindow = /** @type {any} */ (window);

  function browserMicAvailable() {
    return !!(speechWindow.SpeechRecognition || speechWindow.webkitSpeechRecognition);
  }

  function toggleMic() {
    if (state.busy) return;
    if (browserMicAvailable()) {
      if (listening) {
        listening = false;
        if (recognition) { try { recognition.stop(); } catch (e) {} }
        $("btn-mic").classList.remove("active");
        setState("idle", "idle");
        return;
      }
      startBrowserMic();
      return;
    }
    // No browser speech API: ask JARVIS to use the Pi's own microphone.
    setBusy(true);
    $("btn-mic").classList.add("active");
    setState("listening", "listening");
    api("/api/listen", { method: "POST" })
      .then(function (payload) {
        var reply = payload.reply || {};
        if (reply.text && reply.error) {
          addMessage("system", reply.text);
        } else if (reply.text) {
          addMessage("user", "(voice)", {});
          handleReply(reply);
        } else {
          addMessage("system", "I didn't catch anything.");
        }
      })
      .catch(function (error) {
        addMessage("assistant", "Microphone error: " + error.message, { error: true });
      })
      .then(function () {
        $("btn-mic").classList.remove("active");
        setBusy(false);
        setState("idle", "idle");
      });
  }

  function startBrowserMic() {
    var Speech = speechWindow.SpeechRecognition || speechWindow.webkitSpeechRecognition;
    recognition = new Speech();
    recognition.lang = navigator.language || "en-IN";
    recognition.interimResults = true;
    recognition.continuous = false;
    listening = true;
    $("btn-mic").classList.add("active");
    setState("listening", "listening");

    var finalText = "";
    recognition.onresult = function (event) {
      var interim = "";
      for (var i = event.resultIndex; i < event.results.length; i++) {
        var piece = event.results[i][0].transcript;
        if (event.results[i].isFinal) finalText += piece;
        else interim += piece;
      }
      textarea("input").value = (finalText + " " + interim).trim();
      autoGrow();
    };
    recognition.onerror = function (event) {
      listening = false;
      $("btn-mic").classList.remove("active");
      setState("error", "mic error");
      addMessage("system", "Browser microphone error: " + (event.error || "unknown"));
    };
    recognition.onend = function () {
      listening = false;
      $("btn-mic").classList.remove("active");
      setState("idle", "idle");
      if (finalText.trim()) send(finalText);
    };
    try { recognition.start(); } catch (e) {
      listening = false;
      $("btn-mic").classList.remove("active");
      addMessage("system", "Could not start the browser microphone: " + e.message);
    }
  }

  /* ---------------------------------------------------------------- live events */
  var reconnectTimer = null;

  function connect() {
    var protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
    var url = protocol + "//" + window.location.host + "/ws" + (TOKEN ? "?token=" + encodeURIComponent(TOKEN) : "");
    var socket;
    try { socket = new WebSocket(url); } catch (e) { return scheduleReconnect(); }

    socket.onopen = function () {
      setState(state.lastState === "boot" ? "idle" : state.lastState);
    };
    socket.onmessage = function (message) {
      var payload;
      try { payload = JSON.parse(message.data); } catch (e) { return; }
      if (payload.type === "hello") {
        setOnline(true);
        applySnapshot(payload.state, false);
        return;
      }
      if (payload.type === "ping") { return; }
      if (payload.type === "event") onEvent(payload.event);
    };
    socket.onclose = function () {
      if (state.online) setPill($("pill-brain"), "reconnecting", "warn");
      scheduleReconnect();
    };
    socket.onerror = function () { /* onclose handles it */ };
  }

  function scheduleReconnect() {
    if (reconnectTimer) return;
    reconnectTimer = setTimeout(function () {
      reconnectTimer = null;
      refresh().catch(function () {});
      connect();
    }, 3000);
  }

  function onEvent(event) {
    if (!event || !event.kind) return;
    switch (event.kind) {
      case "state":
        setState(event.state, event.note || event.state);
        break;
      case "message":
        if (event.role === "assistant" && event.text) {
          removeTyping();
          addMessage("assistant", event.text, {
            provider: event.provider,
            images: event.images || [],
            messageId: event.message_id,
          });
        }
        break;
      case "tool_start":
        showTyping(event.tool);
        setState("working", event.tool);
        break;
      case "tool_result":
        removeTyping();
        if (!event.ok) addMessage("system", event.tool + " failed: " + (event.error || "unknown error"));
        break;
      case "confirm":
        setState("working", "waiting for your go-ahead");
        break;
      case "provider":
        addMessage("system", event.message || "switching provider");
        refresh().catch(function () {});
        break;
      case "stt_error":
        addMessage("system", "microphone: " + (event.message || "error"));
        break;
      case "reminder":
        if (event.message) addMessage("assistant", event.message, {});
        break;
      case "cleared":
        seedIntro();
        break;
      default:
        break;
    }
  }

  /* ---------------------------------------------------------------- wiring */
  function init() {
    setState("idle", "connecting");
    seedIntro();

    $("composer").addEventListener("submit", function (event) {
      event.preventDefault();
      send(textarea("input").value);
    });
    $("input").addEventListener("input", autoGrow);
    $("input").addEventListener("keydown", function (event) {
      if (event.key === "Enter" && !event.shiftKey) {
        event.preventDefault();
        send(textarea("input").value);
      }
    });
    $("btn-mic").addEventListener("click", toggleMic);
    $("btn-confirm-yes").addEventListener("click", function () { confirm(true); });
    $("btn-confirm-no").addEventListener("click", function () { confirm(false); });

    $("btn-mute").addEventListener("click", function () {
      var next = !state.muted;
      api("/api/speech", { method: "POST", body: { muted: next } })
        .then(function (payload) {
          state.muted = !!payload.muted;
          $("btn-mute").classList.toggle("muted", state.muted);
          setPill($("pill-voice"), state.muted ? "muted" : "voice", state.muted ? "warn" : "ok");
        })
        .catch(function () {});
    });

    $("btn-clear").addEventListener("click", function () {
      api("/api/clear", { method: "POST" })
        .then(function () { clearThread(); seedIntro(); refresh().catch(function () {}); })
        .catch(function (error) { addMessage("system", "could not clear: " + error.message); });
    });

    $("btn-reset-providers").addEventListener("click", function () {
      api("/api/providers/reset", { method: "POST" })
        .then(function () { refresh().catch(function () {}); })
        .catch(function () {});
    });

    $("suggestions").addEventListener("click", function (event) {
      var target = /** @type {HTMLElement} */ (event.target);
      var chip = target.closest(".chip");
      if (!chip) return;
      send(chip.getAttribute("data-fill") || chip.textContent || "");
    });

    var panel = $("panel");
    var scrim = $("scrim");
    var openPanel = function (open) {
      panel.classList.toggle("open", open);
      scrim.classList.toggle("show", open);
    };
    $("btn-panel").addEventListener("click", function () { openPanel(true); });
    $("btn-panel-close").addEventListener("click", function () { openPanel(false); });
    scrim.addEventListener("click", function () { openPanel(false); });

    refresh().catch(function () {});
    connect();
    setInterval(function () { refresh().catch(function () {}); }, 20000);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
