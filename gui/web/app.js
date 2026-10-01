/* ==========================================================================
   JARVIS web console
   Vanilla JS, no build step, no external requests - it has to work on a
   Raspberry Pi and on a phone with only the local network.

   Everything here is the real thing:
     * text  -> POST /api/chat      (the backend brain, tools and memory)
     * voice -> the backend STT     (/api/listen for the Pi's microphone,
                                     /api/transcribe for this device's mic)
     * speech -> the backend TTS    (/api/speak returns audio for the engine
                                     configured on the Pi; the browser only
                                     plays the file, it never synthesizes)
   ========================================================================== */
(function () {
  "use strict";

  var TOKEN = (function () {
    var fromUrl = new URLSearchParams(window.location.search).get("token");
    if (fromUrl) { try { sessionStorage.setItem("jarvis_token", fromUrl); } catch (e) {} }
    try { return fromUrl || sessionStorage.getItem("jarvis_token") || ""; } catch (e) { return fromUrl || ""; }
  })();

  var $ = function (id) { return document.getElementById(id); };

  /** Writes text only when the node really is on the page.
   *
   *  A missing element must never take the console down with it.  An old cached
   *  app.js meeting a newer index.html used to abort the whole render, and the
   *  banner then wrongly claimed the backend was unreachable.
   */
  function setText(id, value) {
    var node = $(id);
    if (node) node.textContent = value;
    return node;
  }
  /** @param {string} id @returns {HTMLButtonElement} */
  function button(id) { return /** @type {HTMLButtonElement} */ ($(id)); }
  /** @param {string} id @returns {HTMLTextAreaElement} */
  function textarea(id) { return /** @type {HTMLTextAreaElement} */ ($(id)); }
  /** @returns {HTMLMediaElement} */
  function player() { return /** @type {HTMLMediaElement} */ ($("player")); }

  var storage = {
    get: function (key, fallback) {
      try { return localStorage.getItem(key) || fallback; } catch (e) { return fallback; }
    },
    set: function (key, value) {
      try { localStorage.setItem(key, value); } catch (e) { /* private mode */ }
    },
  };

  var state = {
    busy: false,
    pending: null,
    online: false,
    live: false,
    micMuted: false,
    voiceMuted: false,
    voiceOut: storage.get("jarvis_voice_out", "browser"),
    audioClaimed: false,
    voiceAvailable: false,
    voiceConfigured: false,
    micAvailable: false,
    listening: false,
    speaking: false,
    needsUnlock: false,
    lastState: "idle",
    liveTimer: null,
    micStatus: null,
    // voice input is a bonus: `textOnly` is set when no microphone path exists,
    // and the console then says so instead of offering a dead button
    textOnly: false,
    browserMic: true,
    micProbed: false,
    warnedNoMic: false,
    warnedNoProvider: false,
    warnedKeyless: false,
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

  /** The header's live clock and date, straight from this device's own time. */
  function tickClock() {
    var now = new Date();
    var time = $("clock");
    if (time) time.textContent = now.toTimeString().slice(0, 8);
    var day = $("day");
    if (day) {
      day.textContent = now.toLocaleDateString(undefined, {
        weekday: "short", day: "2-digit", month: "short", year: "numeric",
      });
    }
  }

  /* ---------------------------------------------------------------- core */
  // The neural core - JARVIS's face.  A particle field on a canvas that is
  // driven *only* by real state: the microphone level while listening, a firing
  // synapse web while thinking, sonar rings while speaking.  Deliberately
  // cheap: no shadows, no blur, no filters, ~40fps, and nothing at all when the
  // tab is hidden or the visitor asked for reduced motion.
  var core = (function () {
    var canvas = /** @type {HTMLCanvasElement|null} */ ($("core-canvas"));
    var ctx = canvas ? canvas.getContext("2d") : null;
    var nodes = [];
    var mode = "idle";
    var level = 0;
    var wave = 0;
    var raf = null;
    var last = 0;
    var dpr = 1;
    var reduced = !!(window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches);
    // each state gets its own colour family, so the core reads at a glance
    var TINTS = {
      idle: [188, 205],
      listening: [148, 168],
      thinking: [34, 48],
      working: [34, 48],
      speaking: [252, 272],
      error: [352, 368],  // wraps past 360, staying in the reds

    };

    function grow() {
      var width = window.innerWidth;
      var count = width < 620 ? 22 : width < 1100 ? 32 : 46;
      nodes = [];
      for (var i = 0; i < count; i++) {
        nodes.push({
          angle: Math.random() * Math.PI * 2,
          orbit: 0.26 + Math.random() * 0.44,
          speed: 0.0011 + Math.random() * 0.0026,
          size: 0.9 + Math.random() * 1.7,
          drift: Math.random() * Math.PI * 2,
          fire: 0,
        });
      }
    }

    function resize() {
      if (!canvas || !ctx) return;
      var box = canvas.getBoundingClientRect();
      var side = Math.max(90, Math.round(box.width || canvas.clientWidth || 180));
      dpr = Math.min(2, window.devicePixelRatio || 1);
      canvas.width = Math.round(side * dpr);
      canvas.height = Math.round(side * dpr);
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      last = 0;
    }

    function tint() {
      var pair = TINTS[mode] || TINTS.idle;
      return pair[0] + Math.random() * (pair[1] - pair[0]);
    }

    function render(dt) {
      if (!ctx || !canvas) return;
      var side = canvas.clientWidth || 180;
      var middle = side / 2;
      var radius = middle * 0.94;
      ctx.clearRect(0, 0, side, side);

      var busy = mode === "thinking" || mode === "working";
      var lively = mode === "listening";
      var speed = busy ? 3.1 : mode === "speaking" ? 1.9 : lively ? 1.35 : 0.7;
      var push = lively ? Math.min(1, level * 2.4) : 0;
      var wobble = busy ? radius * 0.035 : 0;

      // sonar rings: one per ~450ms of speech, honest to the audio that plays
      if (mode === "speaking") {
        wave = (wave + dt * 0.021) % 1;
        for (var w = 0; w < 3; w++) {
          var reach = ((wave + w / 3) % 1);
          ctx.beginPath();
          ctx.strokeStyle = "hsla(" + tint() + ", 92%, 74%, " + (0.34 * (1 - reach)).toFixed(3) + ")";
          ctx.lineWidth = 1.2;
          ctx.arc(middle, middle, radius * (0.3 + reach * 0.68), 0, Math.PI * 2);
          ctx.stroke();
        }
      }

      var points = [];
      for (var i = 0; i < nodes.length; i++) {
        var node = nodes[i];
        node.angle += node.speed * speed * dt;
        node.fire = Math.max(0, node.fire - dt * 0.045);
        // a fresh spark every so often keeps it alive without being noisy
        if (Math.random() < (busy ? 0.05 : 0.012)) node.fire = 1;
        var orbit = node.orbit + push * 0.09 + Math.sin(node.drift + node.angle * 3) * 0.012;
        var x = middle + Math.cos(node.angle) * radius * orbit;
        var y = middle + Math.sin(node.angle) * radius * orbit * 0.98;
        if (wobble) {
          x += (Math.random() - 0.5) * wobble;
          y += (Math.random() - 0.5) * wobble;
        }
        points.push([x, y]);
        var alpha = 0.28 + node.fire * 0.62 + push * 0.25;
        ctx.beginPath();
        ctx.fillStyle = "hsla(" + (tint() - node.fire * 6) + ", 95%, " + (58 + node.fire * 30) + "%, " + Math.min(1, alpha).toFixed(3) + ")";
        ctx.arc(x, y, node.size + node.fire * 1.1, 0, Math.PI * 2);
        ctx.fill();
      }

      // while thinking, nearby sparks link up: the network literally forming
      if (busy) {
        ctx.lineWidth = 0.7;
        for (var a = 0; a < points.length; a++) {
          for (var b = a + 1; b < points.length; b++) {
            var dx = points[a][0] - points[b][0];
            var dy = points[a][1] - points[b][1];
            var gap = dx * dx + dy * dy;
            if (gap > 2100) continue;
            ctx.strokeStyle = "hsla(42, 96%, 72%, " + (0.26 * (1 - gap / 2100)).toFixed(3) + ")";
            ctx.beginPath();
            ctx.moveTo(points[a][0], points[a][1]);
            ctx.lineTo(points[b][0], points[b][1]);
            ctx.stroke();
          }
        }
      }
    }

    function tick(ts) {
      raf = null;
      if (document.hidden) return;
      var dt = last ? Math.min(3, Math.max(0.35, (ts - last) / 16.7)) : 1;
      last = ts;
      render(dt);
      schedule();
    }

    function schedule() {
      if (reduced || raf !== null || document.hidden) return;
      raf = requestAnimationFrame(tick);
    }

    function setMode(name) {
      mode = name || "idle";
      if (reduced) render(1);
      else schedule();
    }

    resize();
    grow();
    render(1);
    schedule();
    window.addEventListener("resize", function () { resize(); grow(); render(1); });
    document.addEventListener("visibilitychange", function () {
      last = 0;
      if (!document.hidden) schedule();
    });

    return {
      setMode: setMode,
      setLevel: function (value) { level = Math.max(0, Math.min(1, value || 0)); },
    };
  })();

  /* ---------------------------------------------------------------- trace */
  // The pipeline narrating itself, in order, with real timings.  Only ever fed
  // by what the backend actually reported - never by a guess.
  var TRACE_MAX = 4;

  function pushTrace(text, tone) {
    var list = $("trace-list");
    if (!list || !text) return;
    var empty = list.querySelector(".trace-empty");
    if (empty) list.removeChild(empty);
    var item = document.createElement("li");
    if (tone) item.setAttribute("data-tone", tone);
    item.innerHTML = "<b>" + clock() + "</b> " + escapeHtml(text);
    list.appendChild(item);
    while (list.children.length > TRACE_MAX) list.removeChild(list.firstChild);
  }

  /* ---------------------------------------------------------------- boot */
  // The boot sequence is decoration, so it can never trap the user: it fills
  // itself in, finishes as soon as the backend answers, and gives up on its own
  // if nothing ever answers.
  var boot = { done: false, step: 0, timer: null, auto: null };

  function startBoot() {
    var bar = $("boot-bar");
    if (!bar) { boot.done = true; return; }
    boot.timer = setInterval(function () {
      if (boot.step < bar.children.length) bar.children[boot.step].classList.add("on");
      boot.step += 1;
      if (boot.step >= bar.children.length) {
        var note = $("boot-note");
        if (note) note.textContent = "core online · console ready";
      }
    }, 130);
    boot.auto = setTimeout(function () { finishBoot("no answer from the brain yet"); }, 4200);
  }

  function finishBoot(note) {
    if (boot.done) return;
    boot.done = true;
    if (boot.timer) clearInterval(boot.timer);
    if (boot.auto) clearTimeout(boot.auto);
    var bar = $("boot-bar");
    if (bar) {
      Array.prototype.forEach.call(bar.children, function (segment) { segment.classList.add("on"); });
    }
    var message = $("boot-note");
    if (message && note) message.textContent = note;
    var overlay = $("boot");
    if (overlay) {
      overlay.classList.add("done");
      setTimeout(function () { overlay.style.display = "none"; }, 460);
    }
    document.body.removeAttribute("data-boot");
  }

  /* ---------------------------------------------------------------- gauges */
  var GAUGE_LENGTH = 301.6; // 2 * pi * r, r = 48 in the SVG

  /** Paint one radial gauge. Unknown values show a dash rather than a guess. */
  function paintGauge(fillId, valueId, ringPercent, label) {
    var ring = $(fillId);
    var text = $(valueId);
    var known = typeof ringPercent === "number" && isFinite(ringPercent);
    if (ring) {
      var clamped = known ? Math.max(0, Math.min(100, ringPercent)) : 0;
      ring.style.strokeDashoffset = String(GAUGE_LENGTH * (1 - clamped / 100));
    }
    if (text) text.textContent = known ? label : "\u2014";
  }

  function renderMachine(machine, toolCount) {
    machine = machine || {};
    var memory = machine.memory || {};
    var disk = machine.disk || {};
    var cpu = machine.cpu_percent;
    var temp = machine.temperature_c;
    paintGauge("gauge-cpu", "val-cpu", cpu, Math.round(cpu || 0) + "%");
    paintGauge("gauge-ram", "val-ram", memory.percent, Math.round(memory.percent || 0) + "%");
    paintGauge("gauge-disk", "val-disk", disk.percent, Math.round(disk.percent || 0) + "%");
    // the temperature ring fills against a 0-100 C scale, the label reads out C
    paintGauge("gauge-temp", "val-temp", temp, Math.round(temp || 0) + "\u00b0");

    var stats = $("machine-stats");
    if (!stats) return;
    stats.innerHTML = "";
    var rows = [
      ["host", machine.host || "unknown"],
      ["board", machine.board || (machine.is_pi ? "raspberry pi" : "generic")],
      ["load", machine.load === null || machine.load === undefined ? "\u2014" : machine.load],
      ["uptime", machine.uptime || "\u2014"],
      ["cores", machine.cpu_count || "\u2014"],
      ["tools", toolCount || 0],
    ];
    rows.forEach(function (pair) {
      var box = document.createElement("span");
      box.className = "stat";
      box.innerHTML = pair[0] + " <b>" + escapeHtml(String(pair[1])) + "</b>";
      stats.appendChild(box);
    });
    if (machine.error) addMachineNote(stats, "telemetry error: " + machine.error);
  }

  function addMachineNote(container, text) {
    var note = document.createElement("p");
    note.className = "muted small";
    note.textContent = text;
    container.appendChild(note);
  }

  /* ---------------------------------------------------------------- feed */
  var MAX_FEED = 24;

  function pushFeed(text, tone) {
    var list = $("feed");
    if (!list || !text) return;
    var item = document.createElement("li");
    item.textContent = clock() + "  " + text;
    if (tone === "warn") item.style.color = "#ffd9d9";
    else if (tone === "ok") item.style.color = "#b6ffe6";
    list.insertBefore(item, list.firstChild);
    while (list.children.length > MAX_FEED) list.removeChild(list.lastChild);
  }

  /* ---------------------------------------------------------------- visitor */
  function renderVisitor(visitor) {
    var box = $("visitor");
    if (!box) return;
    var label = $("visitor-label");
    if (!visitor || !(visitor.label || visitor.raw)) {
      box.classList.add("hidden");
      return;
    }
    if (label) label.textContent = visitor.label || visitor.raw;
    box.classList.remove("hidden");
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
      if (response.status === 401) {
        // Say where the token actually lives: "reopen with ?token=" on its own
        // is a dead end for anyone who has never set one.
        throw new Error(
          "token rejected - this JARVIS is protected. Its token is JARVIS_WEB_TOKEN in .env " +
            "(or leave that line empty for no token). Open http://" +
            window.location.host +
            "/?token=... once and this tab remembers it."
        );
      }
      if (!response.ok) {
        return response.json().catch(function () { return {}; }).then(function (data) {
          throw new Error(data.detail || ("request failed (" + response.status + ")"));
        });
      }
      return response.json();
    });
  }

  /** Raw-body POST, used to send recorded PCM to the recogniser. */
  function apiRaw(path, payload) {
    var headers = { "Content-Type": "application/octet-stream" };
    if (TOKEN) headers["X-Jarvis-Token"] = TOKEN;
    return fetch(path, { method: "POST", headers: headers, body: payload })
      .then(function (response) {
        return response.json().catch(function () { return {}; }).then(function (data) {
          if (!response.ok) throw new Error(data.detail || ("request failed (" + response.status + ")"));
          return data;
        });
      });
  }

  /* ---------------------------------------------------------------- render */
  // Reads like the HUD it is modelled on: the state name shown on screen is the
  // console's own word for what the brain is doing, never an invented metric.
  var MISSION = {
    boot: "initialising",
    idle: "standby · nominal",
    listening: "capturing audio",
    thinking: "processing request",
    working: "executing tool",
    speaking: "speaking",
    error: "attention required",
  };

  function setState(name, note) {
    if (!name) return;
    var changed = name !== state.lastState;
    state.lastState = name;
    document.body.setAttribute("data-state", name);
    core.setMode(name);
    var tone = "idle";
    if (name === "listening") tone = "ok";
    else if (name === "thinking" || name === "working") tone = "busy";
    else if (name === "error") tone = "warn";
    setPill($("pill-state"), note || name, tone);
    var label = $("state-label");
    var shown = note || name;
    if (label) label.textContent = (state.live ? "live talk · " : "") + shown;
    var mission = $("mission");
    if (mission) mission.textContent = MISSION[name] || name;
    var coreMode = $("core-mode");
    if (coreMode) coreMode.textContent = shown;
    if (changed) {
      pushFeed("state · " + shown);
      pushTrace(shown, name === "error" ? "warn" : (name === "thinking" || name === "working" ? "busy" : ""));
    }
  }

  function setPill(pill, text, tone) {
    if (!pill) return;
    pill.setAttribute("data-tone", tone || "muted");
    var label = pill.querySelector("span");
    if (label) label.textContent = text;
  }

  function thread() { return $("thread"); }

  function scrollToEnd() {
    var node = thread();
    node.scrollTop = node.scrollHeight;
  }

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
      var speaker = meta.provider ? "jarvis · " + meta.provider : "jarvis";
      who.textContent = role === "user" ? (meta.voice ? "you · voice" : "you") : speaker;
      head.appendChild(who);
      head.appendChild(document.createTextNode(meta.time || clock()));
      if (role === "assistant") {
        var grow = document.createElement("span");
        grow.className = "grow";
        head.appendChild(grow);
        var say = document.createElement("button");
        say.type = "button";
        say.className = "copy-btn";
        say.textContent = "speak";
        say.addEventListener("click", function () { requestSpeech(text, true); });
        head.appendChild(say);
        var copy = document.createElement("button");
        copy.type = "button";
        copy.className = "copy-btn";
        copy.textContent = "copy";
        copy.addEventListener("click", function () { copyText(text, copy); });
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

  /** "Built by Ayush." - the "for Ayush" half only appears when it is someone else. */
  function creditText(creator, owner) {
    var who = creator || owner || "";
    if (!creator || creator === owner) return "Built by " + who + ".";
    return "Built by " + creator + " for " + owner + ".";
  }

  function seedIntro() {
    clearThread();
    var article = document.createElement("article");
    article.className = "msg intro";
    article.innerHTML =
      '<div class="msg-body">' +
      '<div class="hero" aria-hidden="true"><span class="hero-ring"></span>' +
      '<span class="hero-ring hero-ring-2"></span><span class="hero-core">J</span></div>' +
      "<p><strong>" + escapeHtml(identity.assistant) + "</strong> is online.</p>" +
      '<p class="muted" id="intro-credit">' +
      escapeHtml(creditText(identity.creator, identity.owner)) + "</p>" +
      '<p class="muted">Type a message, tap <strong>Talk</strong> to speak, or ' +
      "<strong>Live Talk</strong> for a hands-free conversation.</p>" +
      '<div class="suggestions" id="suggestions">' +
      ['system status', "what time is it", "search for Raspberry Pi 5 news",
        "generate an image of a futuristic city at night", "what is playing",
        "play some music", "list my hardware devices",
        "remember my project is called Athena"].map(function (text) {
        return '<button class="chip" data-fill="' + escapeHtml(text) + '">' + escapeHtml(text) + "</button>";
      }).join("") +
      "</div></div>";
    thread().appendChild(article);
  }

  var identity = { assistant: "JARVIS", owner: "Ayush", creator: "Ayush" };

  /* ---------------------------------------------------------------- panel */
  var CHAIN_TONE = {
    ready: "ok", cooling: "busy", degraded: "busy", error: "warn", "not configured": "off",
  };

  /** What one node is doing, in one line.  Never invents a number. */
  function describeProvider(item) {
    var bits = [];
    if (item.model) bits.push(item.model);
    if (item.status === "cooling") bits.push("cooling " + Math.round(item.cooldown_s) + "s");
    else if (item.status === "degraded") bits.push(String(item.last_error || "failing").slice(0, 42));
    else if (item.status === "error") bits.push("unavailable");
    else {
      if (item.last_latency_ms) bits.push(item.last_latency_ms + "ms");
      if (item.successes) bits.push(item.successes + " ok");
    }
    return bits.join(" · ");
  }

  /**
   * The fallback chain as a live circuit: wires top to bottom, the brain that
   * is answering now lit up, and every missing key one click from its fix.
   * This is what makes JARVIS's redundancy visible instead of a claim.
   */
  function renderProviders(providers, active) {
    var chain = $("chain");
    if (!chain) return;
    providers = providers || [];
    chain.innerHTML = "";
    var ready = 0;
    var missing = 0;

    providers.forEach(function (item) {
      var tone = CHAIN_TONE[item.status] || "off";
      if (item.status === "ready") ready += 1;
      if (item.status === "not configured") missing += 1;

      var node = document.createElement("li");
      node.className = "node";
      node.setAttribute("data-tone", tone);
      if (item.slug === active && tone === "ok") node.setAttribute("data-active", "true");

      var dot = document.createElement("span");
      dot.className = "node-dot";
      node.appendChild(dot);

      var name = document.createElement("span");
      name.className = "node-name";
      name.textContent = item.label || item.slug;
      node.appendChild(name);

      var meta = document.createElement("span");
      meta.className = "node-meta";
      meta.textContent = describeProvider(item);
      node.appendChild(meta);

      if (item.status === "not configured" && item.requires_key && item.docs) {
        var link = document.createElement("a");
        link.className = "node-key";
        link.href = item.docs;
        link.target = "_blank";
        link.rel = "noopener noreferrer";
        link.textContent = "get key";
        link.title = "Open this provider's key page, then add the key in Settings \u2192 Environment";
        node.appendChild(link);
      } else if (item.keyless) {
        node.appendChild(flagChip("no key needed"));
      } else if (item.local) {
        node.appendChild(flagChip("local"));
      }
      chain.appendChild(node);
    });

    if (!chain.children.length) {
      var empty = document.createElement("li");
      empty.className = "node";
      empty.setAttribute("data-tone", "off");
      empty.textContent = "no providers configured";
      chain.appendChild(empty);
    }

    var readyNode = $("chain-ready");
    if (readyNode) readyNode.textContent = String(ready);
    var totalNode = $("chain-total");
    if (totalNode) totalNode.textContent = String(providers.length);

    var note = $("chain-note");
    if (note) {
      note.textContent = missing
        ? missing + (missing === 1 ? " brain is" : " brains are") + " one key away. " +
          "Every extra key is another lifeline: JARVIS never has to say no because one quota ran out."
        : "Every brain in the chain is armed. Failover runs top to bottom, so the next node " +
          "answers before you ever notice a hiccup.";
    }
  }

  function flagChip(text) {
    var chip = document.createElement("span");
    chip.className = "node-flag";
    chip.textContent = text;
    return chip;
  }

  /**
   * The engine bank: one chip per brain that can answer right now.  It reads the
   * same provider list as the chain, so it can never claim a brain the chain
   * shows as unarmed - it just puts the redundancy where you actually look.
   */
  function renderEngines(providers, active) {
    var list = $("engines-list");
    if (!list) return;
    providers = providers || [];
    var ready = providers.filter(function (item) { return item.status === "ready"; });
    list.innerHTML = "";

    if (!ready.length) {
      var none = document.createElement("span");
      none.className = "engine";
      none.setAttribute("data-tone", "off");
      none.textContent = "offline rules only";
      list.appendChild(none);
      setEnginesNote("no key armed \u00b7 add one in Settings \u2192 Environment");
      return;
    }

    ready.forEach(function (item) {
      var chip = document.createElement("span");
      chip.className = "engine";
      chip.setAttribute("data-tone", item.keyless ? "keyless" : (item.local ? "off" : "ok"));
      if (item.slug === active) chip.classList.add("is-active");
      chip.title = (item.label || item.slug) +
        (item.model ? " \u00b7 " + item.model : "") +
        (item.keyless ? " \u00b7 no key needed (public endpoint)" : "");

      var name = document.createElement("b");
      name.textContent = item.slug;
      chip.appendChild(name);

      if (item.keyless) {
        var tag = document.createElement("span");
        tag.className = "engine-tag";
        tag.textContent = "keyless";
        chip.appendChild(tag);
      }
      list.appendChild(chip);
    });

    var extra = ready.length - 1;
    setEnginesNote(extra > 0
      ? extra + (extra === 1 ? " spare brain" : " spare brains") + " armed"
      : "single brain answering");
  }

  function setEnginesNote(text) {
    var note = $("engines-note");
    if (note) note.textContent = text;
  }

  /* ---------------------------------------------------------------- media */
  // Now playing, read from this machine's media player over MPRIS.  The console
  // shows the real track - or says plainly that nothing is on.  It never invents
  // a title, and the transport buttons do exactly what they claim.
  var ICON_PLAY = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M8 5l11 7-11 7z"/></svg>';
  var ICON_PAUSE = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M9 5v14M15 5v14"/></svg>';
  var PLAYERCTL_HINT = "Install playerctl on the machine running JARVIS to control music from here: " +
    "sudo apt install playerctl";

  function renderMedia(state) {
    state = state || {};
    var playing = !!state.playing;
    document.body.setAttribute("data-media", playing ? "playing" : "idle");

    var title = $("media-title");
    var meta = $("media-meta");
    var note = $("media-note");
    if (state.available) {
      if (title) title.textContent = state.title || "unknown track";
      var bits = [];
      if (state.artist) bits.push(state.artist);
      if (state.album) bits.push(state.album);
      if (state.status) bits.push(state.status);
      if (state.player) bits.push("on " + state.player);
      if (meta) meta.textContent = bits.join(" \u00b7 ");
    } else {
      var reason = String(state.reason || "start a song or a video");
      if (title) title.textContent = "nothing playing";
      if (meta) meta.textContent = reason.slice(0, 140);
      if (note) {
        note.textContent = /playerctl/i.test(reason)
          ? PLAYERCTL_HINT
          : "Start a song or a video on this machine (Spotify, a browser tab, VLC) and I can control it from here.";
      }
    }

    var toggle = $("btn-media-toggle");
    if (toggle) {
      toggle.innerHTML = playing ? ICON_PAUSE : ICON_PLAY;
      toggle.setAttribute("aria-label", playing ? "Pause" : "Play");
      toggle.setAttribute("title", playing ? "Pause" : "Play");
    }
  }

  function refreshMedia() {
    if (document.hidden) return Promise.resolve(null);
    return api("/api/media").then(renderMedia).catch(function () { return null; });
  }

  function mediaAction(action) {
    return api("/api/media", { method: "POST", body: { action: action } })
      .then(function (payload) {
        renderMedia(payload.now);
        pushFeed("media \u00b7 " + action + (payload.ok ? "" : " refused"), payload.ok ? "ok" : "warn");
        if (!payload.ok) addMessage("system", "Media: " + payload.message);
        return payload;
      })
      .catch(function (error) { addMessage("system", "Media: " + error.message); });
  }

  /* ---------------------------------------------------------- boot readout */
  // The boot screen finishes by printing what is actually true about this
  // machine, taken straight from the first real snapshot.
  var bootLinesShown = false;

  function renderBootLog(payload) {
    var list = $("boot-log");
    if (!list || bootLinesShown) return;
    bootLinesShown = true;

    var status = (payload && payload.status) || {};
    var settings = (payload && payload.settings) || {};
    var providers = status.providers || [];
    var ready = providers.filter(function (item) { return item.status === "ready"; });
    var missing = providers.filter(function (item) { return item.status === "not configured"; });
    var mic = status.mic || {};
    var speech = status.speech || {};
    var memory = status.memory || {};
    var machine = status.machine || {};
    var envFiles = settings.env_files || [];

    var lines = [
      ["brains", ready.length + " ready" + (missing.length ? " · " + missing.length + " one key away" : "")],
      ["brain", ready.length ? (ready[0].label || ready[0].slug) : "offline engine (no key set)"],
      ["tools", (status.tools || 0) + " registered"],
      ["memory", (memory.turns || 0) + " turns · " + (memory.facts || 0) + " facts"],
      ["voice", (speech.engine || "none") + " \u2192 " + (status.voice_output || "device")
        + (mic.engine && mic.engine !== "none" ? " · mic " + mic.engine : " · no microphone")],
      ["machine", (machine.host || "unknown") + " · " + (machine.board || (machine.is_pi ? "raspberry pi" : "generic"))],
      ["config", envFiles.length ? envFiles.join(", ") : "none (env.example is the template)"],
    ];

    list.innerHTML = "";
    lines.forEach(function (pair) {
      var item = document.createElement("li");
      var key = document.createElement("b");
      key.textContent = pair[0] + " ";
      item.appendChild(key);
      item.appendChild(document.createTextNode(String(pair[1])));
      list.appendChild(item);
    });
    setText("boot-note", "initialising " + (settings.assistant_name || identity.assistant));
  }

  function renderMemory(memory, facts) {
    var stats = $("memory-stats");
    stats.innerHTML = "";
    [["turns", (memory || {}).turns || 0], ["messages", (memory || {}).messages || 0],
      ["facts", (memory || {}).facts || 0]].forEach(function (pair) {
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
      var item = document.createElement("li");
      item.textContent = key + ": " + facts[key];
      list.appendChild(item);
    });
  }

  function statRow(container, pairs) {
    container.innerHTML = "";
    pairs.forEach(function (pair) {
      var box = document.createElement("span");
      box.className = "stat";
      box.innerHTML = pair[0] + " <b>" + escapeHtml(String(pair[1])) + "</b>";
      container.appendChild(box);
    });
  }

  function renderHardware(hardware) {
    var list = $("device-list");
    hardware = hardware || {};
    var devices = hardware.devices || [];
    statRow($("hardware-stats"), [
      ["backend", hardware.backend || "none"],
      ["devices", devices.length],
      ["mode", hardware.real_hardware ? "live" : "simulated"],
    ]);

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

  /**
   * Does this browser have a microphone at all? Only ever answers "no" when it
   * is sure, and the answer arrives asynchronously (labels need permission).
   */
  function probeBrowserMic() {
    if (state.micProbed) return;
    state.micProbed = true;
    if (!navigator.mediaDevices || !navigator.mediaDevices.enumerateDevices) return;
    navigator.mediaDevices.enumerateDevices().then(function (devices) {
      state.browserMic = devices.some(function (device) {
        return device.kind === "audioinput";
      });
      updateVoiceMode();
    }, function () { /* cannot tell: assume there is one */ });
  }

  /**
   * The Pi's own microphone: a real device *and* a speech engine to drive it.
   * Used to decide whether this page can borrow the Pi's ears when the device it
   * is open on has none of its own.
   */
  function deviceMicAvailable() {
    var mic = state.micStatus || {};
    return mic.available === true && mic.enabled !== false;
  }

  /**
   * Why voice input is off, said straight.  A missing microphone and a missing
   * speech engine are different problems with different fixes, so the console
   * never tells you to plug in a microphone when the real gap is the engine.
   */
  function voiceInputBlocked() {
    var mic = state.micStatus || {};
    if (String(mic.engine || "none") === "none") {
      return "There's no speech engine installed on the machine running JARVIS, so no " +
        "microphone can be transcribed here. Type your message instead - everything except " +
        "voice input works. Install it with: sh scripts/install.sh --voice, then reload.";
    }
    return "There's no microphone available to JARVIS - not on this device and not on the " +
      "Pi. Type your message instead: chat, tools, memory, reminders and JARVIS's spoken " +
      "replies all work without one.";
  }

  /**
   * Voice input needs a microphone *and* something to recognise it with: the Pi's
   * own microphone, or this device's microphone plus a backend STT engine.
   * When neither exists the console goes text-only - visibly, not silently.
   */
  function updateVoiceMode() {
    var mic = state.micStatus || {};
    var engine = String(mic.engine || "none");
    var canTranscribe = engine !== "none" && mic.can_transcribe !== false;
    state.textOnly = !(mic.available === true || (canTranscribe && state.browserMic));

    var textOnly = state.textOnly;
    var hint = $("hint");
    if (hint) {
      hint.textContent = textOnly
        ? "No microphone needed - type your message. Enter sends, Shift+Enter for a new line."
        : "Enter sends · Shift+Enter for a new line · actions that touch your system ask first";
    }
    document.body.setAttribute("data-voice-in", textOnly ? "off" : "on");
    button("btn-mic").disabled = textOnly;
    button("btn-live").disabled = textOnly;
    if (textOnly) {
      button("btn-mic").title = "No microphone on this machine - type your message instead";
      button("btn-live").title = "No microphone on this machine - type your message instead";
    } else {
      button("btn-mic").title = "One tap to talk, and the same tap to stop";
      button("btn-live").title = "One tap for a hands-free conversation, same tap to stop";
    }
  }

  function renderVoice(status) {
    var speech = status.speech || {};
    var mic = status.mic || {};
    // This browser plays the audio, so a missing *local* speaker is not a
    // blocker - all that matters is that the backend engine can synthesize.
    state.voiceAvailable = !!(speech.synthesis_available || speech.available);
    state.micAvailable = !!mic.enabled;
    state.micStatus = mic;
    updateVoiceMode();
    // only worth asking the browser when a backend engine could use its audio
    if (String(mic.engine || "none") !== "none") probeBrowserMic();
    statRow($("voice-stats"), [
      ["voice", speech.engine || "none"],
      ["out", state.voiceMuted ? "off" : (status.voice_output || state.voiceOut)],
      ["mic", mic.engine || "none"],
      ["live", state.live ? "on" : "off"],
    ]);
    var pillNote = $("state-note");
    if (pillNote) {
      pillNote.textContent = "voice " + (speech.engine || "none") + " · mic " + (mic.engine || "none");
    }    if (state.textOnly) {
      setText("voice-note", voiceInputBlocked());
    } else if (!speech.synthesis_available && !speech.available) {
      setText("voice-note", "No speech engine is installed on the machine running JARVIS, "
        + "so replies are text only. Install the voice requirements, then reload.");
    } else if (!speech.available) {
      setText("voice-note", "This machine has no speaker, so JARVIS's voice is played "
        + "here in the browser.");
    } else if (!mic.available) {
      setText("voice-note", "The Pi has no microphone of its own, so the Microphone and Live "
        + "Talk buttons use this device's microphone through /api/transcribe.");
    } else if (!mic.enabled) {
      setText("voice-note", "Microphone input is disabled in .env (STT_ENABLED=false). "
        + "Live Talk still works from this device's microphone.");
    } else {
      setText("voice-note", "Voice is synthesized by the backend. Choose where it is played.");
    }
    $("btn-out-browser").setAttribute("aria-pressed", state.voiceOut === "browser" ? "true" : "false");
    $("btn-out-device").setAttribute("aria-pressed", state.voiceOut === "device" ? "true" : "false");
  }

  function renderToolLog(entries) {
    var list = $("tool-log");
    list.innerHTML = "";
    if (!entries || !entries.length) {
      var empty = document.createElement("li");
      empty.textContent = "no tool activity yet";
      list.appendChild(empty);
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

  /* ---------------------------------------------------------------- audio */
  function showAudioHint() {
    if (state.audioHintShown) return;
    state.audioHintShown = true;
    addMessage("system", "Tap the screen once so this browser allows audio, then press speak again.");
  }

  function setControls() {
    button("btn-mic-mute").setAttribute("aria-pressed", state.micMuted ? "true" : "false");
    button("btn-voice-mute").setAttribute("aria-pressed", state.voiceMuted ? "true" : "false");
    button("btn-live").setAttribute("aria-pressed", state.live ? "true" : "false");
    button("btn-mic").setAttribute("aria-pressed", state.listening ? "true" : "false");
    // one tap to start, the same tap to stop - and the button says which it is
    var talkLabel = button("btn-mic").querySelector("span");
    if (talkLabel) talkLabel.textContent = state.listening ? "Stop" : "Talk";
    var liveLabel = button("btn-live").querySelector("span");
    if (liveLabel) liveLabel.textContent = state.live ? "Stop live" : "Live Talk";
    var micLabel = button("btn-mic-mute").querySelector("span");
    if (micLabel) micLabel.textContent = state.micMuted ? "Mic off" : "Mic on";
    var voiceLabel = button("btn-voice-mute").querySelector("span");
    if (voiceLabel) voiceLabel.textContent = state.voiceMuted ? "Voice off" : "Voice on";
    document.body.setAttribute("data-mic", state.micMuted ? "off" : "on");
    document.body.setAttribute("data-voice", state.voiceMuted ? "off" : "on");
    document.body.setAttribute("data-live", state.live ? "on" : "off");
    if (state.textOnly) {
      // no microphone anywhere: the two voice-input controls stay out of the way
      button("btn-mic").disabled = true;
      button("btn-live").disabled = true;
    }
  }

  /** Play audio the backend produced and drive the "speaking" state. */
  function playSpeechUrl(url) {
    return new Promise(function (resolve) {
      if (!url) { resolve(false); return; }
      var audio = player();
      audio.src = url;
      var finish = function () {
        state.speaking = false;
        if (!state.busy) setState(state.live ? "listening" : "idle", state.live ? "listening" : "idle");
        resolve(true);
      };
      audio.onended = finish;
      audio.onerror = finish;
      state.speaking = true;
      setState("speaking", "speaking");
      var attempt = audio.play();
      if (attempt && attempt.then) {
        attempt.then(function () { state.needsUnlock = false; }, function () {
          state.needsUnlock = true;
          state.pendingSpeech = url;
          showAudioHint();
          finish();
        });
      }
    });
  }

  /** Ask the backend TTS for this text and play it. Forced = the "speak" button. */
  function requestSpeech(text, forced) {
    if (!text) return Promise.resolve(false);
    if (!forced && (state.voiceMuted || !state.voiceAvailable)) return Promise.resolve(false);
    return api("/api/speak", { method: "POST", body: { text: text } })
      .then(function (payload) { return playSpeechUrl(payload.url); })
      .catch(function (error) {
        // never pretend audio played: say what the backend said
        if (forced) addMessage("system", "Voice: " + error.message);
        return false;
      });
  }

  function unlockAudio() {
    if (!state.needsUnlock) return;
    state.needsUnlock = false;
    var url = state.pendingSpeech;
    state.pendingSpeech = null;
    if (url) playSpeechUrl(url);
  }

  /* ---------------------------------------------------------------- recorder */
  /** Records this device's microphone as raw 16-bit PCM for the backend STT. */
  function createRecorder() {
    var w = /** @type {any} */ (window);
    var context = null;
    var stream = null;
    var node = null;
    var source = null;
    var chunks = [];
    var total = 0;
    var active = false;
    var resolveTurn = null;
    var rejectTurn = null;

    function supported() {
      return !!(navigator.mediaDevices && navigator.mediaDevices.getUserMedia &&
        (w.AudioContext || w.webkitAudioContext));
    }

    function floatToPcm(samples) {
      var out = new Int16Array(samples.length);
      for (var i = 0; i < samples.length; i++) {
        var value = Math.max(-1, Math.min(1, samples[i]));
        out[i] = value < 0 ? value * 0x8000 : value * 0x7fff;
      }
      return out;
    }

    function teardown() {
      active = false;
      try { if (node) node.disconnect(); } catch (e) {}
      try { if (source) source.disconnect(); } catch (e) {}
      try { if (stream) stream.getTracks().forEach(function (track) { track.stop(); }); } catch (e) {}
      try { if (context) context.close(); } catch (e) {}
      node = null; source = null; stream = null; context = null;
    }

    function finish() {
      if (!active) return;
      var collected = chunks;
      chunks = [];
      var count = total;
      total = 0;
      teardown();
      setLevel(0);
      var resolve = resolveTurn;
      resolveTurn = null;
      rejectTurn = null;
      if (resolve) resolve({ pcm: floatToPcm(concat(collected)), rate: lastRate, samples: count });
    }

    function concat(parts) {
      var length = 0;
      for (var i = 0; i < parts.length; i++) length += parts[i].length;
      var merged = new Float32Array(length);
      var offset = 0;
      for (var j = 0; j < parts.length; j++) {
        merged.set(parts[j], offset);
        offset += parts[j].length;
      }
      return merged;
    }

    var lastRate = 16000;

    function cancel() {
      if (!active) return;
      var reject = rejectTurn;
      resolveTurn = null;
      rejectTurn = null;
      chunks = [];
      total = 0;
      teardown();
      setLevel(0);
      if (reject) reject(new Error("cancelled"));
    }

    /**
     * @param {{silenceMs?: number, maxMs?: number, minMs?: number, onLevel?: Function}} options
     * @returns {Promise<{pcm: Int16Array, rate: number, samples: number}>}
     */
    function record(options) {
      var opts = options || {};
      var silenceMs = opts.silenceMs || 1100;
      var maxMs = opts.maxMs || 15000;
      if (!supported()) return Promise.reject(new Error("this browser cannot record audio"));
      return new Promise(function (resolve, reject) {
        resolveTurn = resolve;
        rejectTurn = reject;
        navigator.mediaDevices.getUserMedia({
          audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true },
        }).then(function (micStream) {
          stream = micStream;
          try {
            context = w.AudioContext ? new w.AudioContext({ sampleRate: 16000 }) : new w.webkitAudioContext();
          } catch (e) {
            context = new (w.AudioContext || w.webkitAudioContext)();
          }
          lastRate = context.sampleRate || 16000;
          source = context.createMediaStreamSource(stream);
          node = context.createScriptProcessor(4096, 1, 1);
          var heardSpeech = false;
          var lastLoud = Date.now();
          var startedAt = Date.now();
          var silences = 0;

          node.onaudioprocess = function (event) {
            if (!active) return;
            var data = event.inputBuffer.getChannelData(0);
            chunks.push(new Float32Array(data));
            total += data.length;
            var sum = 0;
            for (var i = 0; i < data.length; i++) sum += data[i] * data[i];
            var rms = Math.sqrt(sum / data.length);
            if (opts.onLevel) opts.onLevel(rms);
            if (rms > 0.02) {
              heardSpeech = true;
              lastLoud = Date.now();
              silences = 0;
            } else if (heardSpeech) {
              silences += 1;
              if (Date.now() - lastLoud > silenceMs) { finish(); return; }
            }
            if (Date.now() - startedAt > maxMs) finish();
          };

          source.connect(node);
          // the processor must be in the graph to fire, but must not echo back
          var sink = context.createGain();
          sink.gain.value = 0;
          node.connect(sink);
          sink.connect(context.destination);
          active = true;
        }).catch(function (error) {
          teardown();
          resolveTurn = null;
          rejectTurn = null;
          var name = (error && error.name) || "";
          var message = name === "NotAllowedError" || name === "SecurityError"
            ? "microphone permission was refused - allow it in your browser, or use the Pi's own microphone"
            : name === "NotFoundError" || name === "DevicesNotFoundError"
              ? "this device has no microphone - type your message instead"
              : name === "NotReadableError" || name === "TrackStartError"
                ? "the microphone is already in use by another app - type your message instead"
                : (error && error.message) || "the microphone could not be opened";
          reject(new Error(message));
        });
      });
    }

    return {
      supported: supported,
      active: function () { return active; },
      record: record,
      finish: finish,
      cancel: cancel,
    };
  }

  var recorder = createRecorder();
  var levelBars = [];

  function setLevel(value) {
    if (!levelBars.length) levelBars = Array.prototype.slice.call($("level").children);
    var boosted = Math.min(1, value * 6);
    core.setLevel(boosted);
    for (var i = 0; i < levelBars.length; i++) {
      var middle = Math.abs(i - (levelBars.length - 1) / 2);
      var height = Math.max(0.15, boosted * (1 - middle / levelBars.length) * 1.6);
      levelBars[i].style.transform = "scaleY(" + height.toFixed(3) + ")";
    }
  }

  /* ---------------------------------------------------------------- turns */
  function send(text, options) {
    var opts = options || {};
    var message = (text || "").trim();
    if (!message || state.busy) return Promise.resolve();
    if (!opts.keepInput) {
      textarea("input").value = "";
      autoGrow();
    }
    addMessage("user", message, { time: clock(), voice: !!opts.voice });
    setBusy(true);
    showTyping("thinking");
    setState("thinking", "thinking");
    var startedAt = Date.now();

    return api("/api/chat", { method: "POST", body: { text: message } })
      .then(function (payload) {
        removeTyping();
        var reply = payload.reply || {};
        pushTrace("answered by " + (reply.provider || "offline") + " \u00b7 "
          + ((Date.now() - startedAt) / 1000).toFixed(1) + "s", "ok");
        return handleReply(payload.reply);
      })
      .catch(function (error) {
        removeTyping();
        addMessage("assistant", "I couldn't reach the brain: " + error.message, { error: true });
        pushTrace("request failed \u00b7 " + error.message, "warn");
        setState("error", "offline");
      })
      .then(function () {
        setBusy(false);
        refresh().catch(function () {});
      });
  }

  function handleReply(reply) {
    if (!reply) return Promise.resolve();
    var shown = addMessage("assistant", reply.text, {
      time: clock(),
      steps: reply.steps,
      images: reply.images,
      error: reply.error,
      provider: reply.provider,
      messageId: reply.id,
    });
    if (reply.pending) showConfirmation(reply.pending);
    // a reply may just have started (or paused) something: reflect it right away
    refreshMedia();
    if (reply.error || !reply.text) return Promise.resolve();
    // the socket may have delivered this same reply first: speak it once
    if (!shown) return Promise.resolve();
    return requestSpeech(reply.text);
  }

  function confirm(approved) {
    if (state.busy) return;
    setBusy(true);
    showTyping(approved ? "running it" : "cancelling");
    api("/api/confirm", { method: "POST", body: { approved: approved } })
      .then(function (payload) {
        removeTyping();
        showConfirmation(null);
        return handleReply(payload.reply);
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

  function setBusy(busy) {
    state.busy = busy;
    button("btn-send").disabled = busy;
    if (!state.live) button("btn-mic").disabled = busy || state.textOnly;
  }

  /* ---------------------------------------------------------------- microphone */
  function transcribe(clip) {
    return apiRaw("/api/transcribe?rate=" + encodeURIComponent(clip.rate), clip.pcm)
      .then(function (payload) {
        if (payload.ok) {
          var text = String(payload.text || "").trim();
          return text || null;
        }
        var why = payload.error || "";
        // "nothing was said" is not an error worth interrupting the user with
        if (/no speech|didn't catch|couldn't make out|no audio/i.test(why)) return null;
        throw new Error(why || "the recogniser failed");
      });
  }

  /** One spoken turn: record, transcribe with the backend STT, then answer. */
  function captureTurn(label) {
    if (state.micMuted) {
      addMessage("system", "The microphone is muted. Unmute it with the Mic on/off control.");
      return Promise.resolve();
    }
    state.listening = true;
    setControls();
    setState("listening", label || "listening");
    return recorder.record({ silenceMs: 1100, maxMs: 15000, onLevel: setLevel })
      .then(function (clip) {
        state.listening = false;
        setControls();
        if (!clip.samples || clip.samples < clip.rate * 0.25) {
          setState("idle", "idle");
          return null;
        }
        setState("thinking", "transcribing");
        return transcribe(clip);
      })
      .then(function (text) {
        if (!text) return null;
        return send(text, { voice: true });
      })
      .catch(function (error) {
        state.listening = false;
        setControls();
        var message = (error && error.message) || "the microphone failed";
        if (message === "cancelled") { setState("idle", "idle"); return null; }
        // Definitively no microphone on this device? The Pi's own microphone can
        // still take the turn - fall over to it instead of stopping at an error.
        // (Live Talk is excluded: it hands the loop to /api/live in setLive.)
        if (!state.live && deviceMicAvailable() &&
            /no microphone|could not be opened|already in use/i.test(message)) {
          addMessage("system", "This device has no usable microphone, so I'll listen " +
            "through the Pi's instead.");
          serverListen();
          return null;
        }
        addMessage("system", message);
        pushFeed("microphone · " + message, "warn");
        // A missing microphone must never make the console look broken: it goes
        // back to idle with a note, and typed chat keeps working exactly as before.
        if (/no microphone|no speech engine|speech engine|not installed|refused|in use/i.test(message)) {
          setState("idle", "microphone unavailable · type instead");
        } else {
          setState("idle", "idle");
        }
        if (state.live) {
          // nothing can transcribe here: stop rather than loop on failure
          setLive(false);
          addMessage("system", "Live Talk stopped. You can still type every message - " +
            "or install the speech engine on the machine running JARVIS " +
            "(sh scripts/install.sh --voice) and reload.");
        }
        return null;
      });
  }

  function toggleMic() {
    if (state.busy) return;
    if (state.textOnly) {
      addMessage("system", voiceInputBlocked());
      return;
    }
    if (recorder.active()) {
      recorder.finish();  // stop now and send what was said
      return;
    }
    // No microphone on this device, but the Pi has one: use the Pi's ears rather
    // than telling a phone user they cannot talk to their own assistant.
    if (state.browserMic === false && deviceMicAvailable()) { serverListen(); return; }
    if (recorder.supported()) { captureTurn("listening"); return; }
    // No browser capture (old browser, no permission): use the Pi's microphone.
    serverListen();
  }

  function serverListen() {
    if (state.micMuted) {
      addMessage("system", "The microphone is muted. Unmute it with the Mic on/off control.");
      return;
    }
    setBusy(true);
    setState("listening", "listening on the device");
    api("/api/listen", { method: "POST" })
      .then(function (payload) {
        var reply = payload.reply || {};
        if (reply.error) addMessage("system", reply.text || "The microphone is unavailable.");
        else return handleReply(reply);
      })
      .catch(function (error) { addMessage("system", "Microphone: " + error.message); })
      .then(function () {
        setBusy(false);
        setState(state.live ? "listening" : "idle", state.live ? "listening" : "idle");
      });
  }

  /* ---------------------------------------------------------------- live talk */
  function setLive(enabled) {
    if (enabled && state.textOnly) {
      addMessage("system", "Live Talk needs voice input. " + voiceInputBlocked());
      return;
    }
    state.live = enabled;
    setControls();
    if (state.liveTimer) { clearTimeout(state.liveTimer); state.liveTimer = null; }
    if (!enabled) {
      if (recorder.active()) recorder.cancel();
      state.listening = false;
      setControls();
      setState("idle", "idle");
      addMessage("system", "Live Talk off.");
      // stop the device-side loop too, whichever one was running
      api("/api/live", { method: "POST", body: { enabled: false } }).catch(function () {});
      return;
    }

    // This device records when it can; when it has no microphone of its own, the
    // Pi's microphone takes over so Live Talk works either way.
    if (recorder.supported() && !(state.browserMic === false && deviceMicAvailable())) {
      addMessage("system", "Live Talk on. Speak normally - I'll answer as you go.");
      liveTick();
      return;
    }
    // Fall back to the Pi's own microphone loop, and say so rather than pretend.
    api("/api/live", { method: "POST", body: { enabled: true } })
      .then(function (payload) {
        if (payload.enabled || payload.running) {
          addMessage("system", "Live Talk on, listening through the device's microphone.");
          setState("listening", "listening on the device");
        } else {
          state.live = false;
          setControls();
          addMessage("system", "Live Talk needs a microphone: " + (payload.reason || "none available"));
        }
      })
      .catch(function (error) {
        state.live = false;
        setControls();
        addMessage("system", "Live Talk could not start: " + error.message);
      });
  }

  function liveTick() {
    if (!state.live) return;
    if (state.micMuted || state.busy || state.speaking || recorder.active()) {
      state.liveTimer = setTimeout(liveTick, 300);
      return;
    }
    captureTurn("listening").then(function () {
      if (!state.live) return;
      state.liveTimer = setTimeout(liveTick, 250);
    });
  }

  /* ---------------------------------------------------------------- controls */
  function toggleMicMute() {
    state.micMuted = !state.micMuted;
    if (state.micMuted && recorder.active()) recorder.cancel();
    setControls();
    api("/api/mic", { method: "POST", body: { muted: state.micMuted } })
      .then(function () { refresh().catch(function () {}); })
      .catch(function () {});
    addMessage("system", state.micMuted ? "Microphone muted." : "Microphone live.");
  }

  /** Mute / unmute JARVIS's voice. The backend owns the state, we just reflect it. */
  function toggleVoiceMute() {
    state.audioClaimed = false; // the next snapshot may claim the audio again
    api("/api/speech", { method: "POST", body: {} })
      .then(function (payload) {
        state.voiceMuted = payload.mode === "off";
        if (payload.mode !== "off") state.voiceOut = payload.mode;
        if (state.voiceMuted) {
          try { player().pause(); } catch (e) {}
          state.speaking = false;
        }
        setControls();
        addMessage("system", state.voiceMuted
          ? "JARVIS is muted."
          : "JARVIS will speak through " + (payload.mode === "browser" ? "this browser." : "the device."));
        refresh().catch(function () {});
      })
      .catch(function (error) { addMessage("system", "Voice control: " + error.message); });
  }

  function setVoiceOut(mode) {
    state.voiceOut = mode;
    state.audioClaimed = false;
    storage.set("jarvis_voice_out", mode);
    setControls();
    api("/api/speech", { method: "POST", body: { mode: mode } })
      .then(function (payload) {
        state.voiceOut = payload.mode;
        state.voiceMuted = payload.mode === "off";
        setControls();
        addMessage("system", payload.mode === "off"
          ? "Voice output is off."
          : "JARVIS will speak through " + (payload.mode === "browser" ? "this browser." : "the device."));
        refresh().catch(function () {});
      })
      .catch(function (error) { addMessage("system", "Voice output: " + error.message); });
  }

  function stopAll() {
    if (recorder.active()) recorder.cancel();
    state.speaking = false;
    try { player().pause(); } catch (e) {}
    api("/api/stop", { method: "POST" })
      .then(function () {
        if (!state.busy) setState(state.live ? "listening" : "idle", state.live ? "listening" : "idle");
      })
      .catch(function () {});
    addMessage("system", "Stopped.");
  }

  function clearChat() {
    api("/api/clear", { method: "POST" })
      .then(function () { renderedIds = {}; seedIntro(); refresh().catch(function () {}); })
      .catch(function (error) { addMessage("system", "could not clear: " + error.message); });
  }

  /* ---------------------------------------------------------------- confirmation */
  function showConfirmation(pending) {
    state.pending = pending;
    var box = $("confirmation");
    if (!box) return;
    if (!pending) { box.classList.add("hidden"); return; }
    setText("confirm-text", pending.question || "Confirm this action?");
    setText("confirm-meta", "tool: " + (pending.tool || "?"));
    box.classList.remove("hidden");
    var yes = $("btn-confirm-yes");
    if (yes) yes.focus();
  }

  /* ---------------------------------------------------------------- snapshot */
  function applySnapshot(payload, keepThread) {
    if (!payload) return;
    var who = payload.identity || {};
    if (who.assistant) {
      identity.assistant = who.assistant;
      identity.owner = who.owner || identity.owner;
      identity.creator = who.creator || identity.creator || identity.owner;
      document.title = who.assistant;
      setText("intro-name", who.assistant);
      var credit = $("intro-credit");
      if (credit) {
        credit.textContent = creditText(identity.creator, identity.owner);
      }
      setText("brand-sub", "personal ai · " + identity.owner);
    }
    var status = payload.status || {};
    setState(status.state || "idle", status.state_note || status.state);
    renderBootLog(payload);
    renderProviders(status.providers, status.provider);
    renderEngines(status.providers, status.provider);
    renderMemory(status.memory, status.facts);
    renderMachine(status.machine, status.tools);
    renderVisitor(status.visitor);
    renderHardware(status.hardware);
    renderVoice(status);
    renderToolLog(status.recent_tools);
    showConfirmation(status.pending);

    var voice = payload.voice || {};
    if (typeof voice.mic_muted === "boolean") state.micMuted = voice.mic_muted;
    if (typeof voice.configured === "boolean") state.voiceConfigured = voice.configured;
    if (voice.mode) {
      state.voiceMuted = voice.mode === "off";
      state.voiceOut = voice.mode === "off"
        ? (voice.routing || state.voiceOut)
        : voice.mode;
    }
    var speech = status.speech || {};
    var voiceOk = !!(speech.synthesis_available || speech.available);
    setPill($("pill-voice"), !voiceOk ? "no voice" : (state.voiceMuted ? "muted" : speech.engine || "voice"),
      !voiceOk ? "warn" : (state.voiceMuted ? "warn" : "ok"));
    var mic = status.mic || {};
    setPill($("pill-mic"), state.micMuted ? "muted" : (mic.engine || "off"),
      state.micMuted ? "warn" : (mic.enabled ? "ok" : "muted"));
    var providers = status.providers || [];
    var live = providers.filter(function (item) { return item.status === "ready"; });
    setPill($("pill-brain"), live.length ? live[0].slug : "offline", live.length ? "ok" : "warn");
    setPill($("pill-brains"), live.length + "/" + providers.length,
      live.length > 1 ? "ok" : (live.length ? "busy" : "warn"));
    setControls();

    if (!keepThread) {
      renderHistory(payload.messages);
      // Say plainly what a keyless install can and cannot do - silence here
      // would read as "it is broken" the first time it runs.
      var keylessOnly = live.length > 0 && live.every(function (item) { return item.keyless; });
      if (!live.length && !state.warnedNoProvider) {
        state.warnedNoProvider = true;
        addMessage("system", "No AI provider is reachable, so general questions go to the " +
          "offline engine. Time, maths, system status, memory, your GPIO devices and the " +
          "visitor protocol already work. Open the Ai core panel - every red node there is " +
          "one key away from becoming another brain. Setup help is at /docs.");
      } else if (keylessOnly && !state.warnedKeyless) {
        state.warnedKeyless = true;
        addMessage("system", "Answering through the keyless public endpoint: it needs no setup " +
          "and keeps JARVIS alive offline-of-quota, but anything you send it leaves this " +
          "machine. Add a provider key for a private brain, or set POLLINATIONS_ENABLED=false " +
          "to switch it off. Open the Ai core panel to see the whole chain.");
      }
      if (state.textOnly && !state.warnedNoMic) {
        state.warnedNoMic = true;
        addMessage("system", "No microphone is available here, so voice input is off - " +
          "nothing else changes. Type every message in the box below: chat, tools, memory, " +
          "reminders and JARVIS's spoken replies all work without a microphone.");
      }
    }
  }

  function setOnline(online, why) {
    state.online = online;
    var banner = $("offline-banner");
    setPill($("pill-link"), online ? "connected" : "offline", online ? "ok" : "warn");
    if (online) {
      banner.classList.add("hidden");
      button("btn-send").disabled = state.busy;
      finishBoot("core online · console ready");
    } else {
      banner.classList.remove("hidden");
      var detail = banner.querySelector("span");
      if (why && detail) detail.textContent = why;
      setPill($("pill-brain"), "offline", "warn");
      button("btn-send").disabled = true;
    }
  }

  /** Report a front-end render failure instead of blaming the backend.
   *
   *  `refresh()` used to funnel *every* failure into the "cannot reach the
   *  brain" banner, so a console-side bug read as a dead Pi and sent people
   *  hunting for a backend that was answering perfectly well.
   */
  function setRenderError(err) {
    var message = err && err.message ? err.message : String(err);
    pushFeed("console render error · " + message, "warn");
    setPill($("pill-state"), "console error", "warn");
    addMessage("system", "JARVIS answered, but this console page hit a front-end error "
      + "while drawing the answer (" + message + "). The Pi is fine. Reload with "
      + "Ctrl+Shift+R to force the newest console files.");
  }

  function refresh() {
    return api("/api/state").then(function (payload) {
      try {
        setOnline(true);
        applySnapshot(payload, true);
      } catch (err) {
        // The backend answered - so say that, instead of claiming it is offline.
        setOnline(true);
        setRenderError(err);
        throw err;
      }
      // The log block is the real backend ring buffer, not a client echo.
      api("/api/logs?limit=40")
        .then(function (logs) { renderLogs((logs || {}).logs); })
        .catch(function () {});
      return payload;
    }).catch(function (error) {
      // Only a failed request may claim the brain is unreachable.
      if (!state.online) {
        setOnline(false, "This page is served but it cannot reach the JARVIS brain (" +
          error.message + "). Start it with `.venv/bin/python main.py`, then reload.");
      }
      throw error;
    });
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
        claimBrowserAudio();
        return;
      }
      if (payload.type === "ping") return;
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
      case "visitor":
        renderVisitor(event.visitor);
        pushFeed(
          event.visitor && (event.visitor.label || event.visitor.raw)
            ? "visitor · " + (event.visitor.label || event.visitor.raw)
            : (event.message || "visitor protocol closed"),
          "warn"
        );
        refresh().catch(function () {});
        break;
      case "message":
        if (event.role === "user" && event.text) {
          // a turn that came from the device's microphone (Live Talk)
          addMessage("user", event.text, { voice: event.source === "voice", time: clock() });
        } else if (event.role === "assistant" && event.text) {
          removeTyping();
          var shown = addMessage("assistant", event.text, {
            provider: event.provider,
            images: event.images || [],
            messageId: event.message_id,
          });
          if (shown) requestSpeech(event.text);
          pushFeed("reply ready" + (event.provider ? " · " + event.provider : ""));
        }
        break;
      case "tool_start":
        showTyping(event.tool);
        setState("working", event.tool);
        pushFeed("tool → " + event.tool);
        pushTrace("running " + event.tool, "busy");
        break;
      case "tool_result":
        removeTyping();
        if (!event.ok) addMessage("system", event.tool + " failed: " + (event.error || "unknown error"));
        pushFeed(event.tool + (event.ok ? " · ok" : " · failed"), event.ok ? "ok" : "warn");
        pushTrace(event.tool + (event.ok ? " ok" : " failed"), event.ok ? "ok" : "warn");
        break;
      case "confirm":
        setState("working", "waiting for your go-ahead");
        break;
      case "provider":
        addMessage("system", event.message || "switching provider");
        pushTrace((event.provider ? event.provider + " unavailable - next brain up" : "switching provider"), "warn");
        refresh().catch(function () {});
        break;
      case "stt_error":
        if (event.fatal) {
          // The backend gave up on its microphone: say it once, and never again.
          // (A phone's browser can still dictate, so the client decides what that
          // means - it refreshes its own microphone status instead of assuming.)
          refresh().catch(function () {});
          if (!state.warnedNoMic) {
            state.warnedNoMic = true;
            addMessage("system", event.message || "No microphone is available - type your message instead.");
            pushFeed("microphone · unavailable", "warn");
          }
        } else {
          addMessage("system", "microphone: " + (event.message || "error"));
          pushFeed("microphone · " + (event.message || "error"), "warn");
        }
        break;
      case "mic":
        state.micMuted = !!event.muted;
        setControls();
        break;
      case "speech":
        break;
      case "reminder":
        if (event.message) {
          addMessage("assistant", event.message, {});
          requestSpeech(event.message);
        }
        break;
      case "cleared":
        renderedIds = {};
        seedIntro();
        break;
      default:
        break;
    }
  }

  /** The output the user picked, remembered per browser.
   *
   * Deliberately not `state.voiceOut`: `applySnapshot` copies whatever the
   * *server* is currently doing into that, so a fresh console on a Pi whose
   * default is its own speaker read "device" here, refused to claim the audio,
   * and every reply was silent - /api/speak answered 502 and a Pi with no sound
   * card had nothing to play either. Only an explicit choice may keep the voice
   * on the device.
   */
  function voicePreference() {
    return storage.get("jarvis_voice_out", "browser");
  }

  /** A web client plays the voice itself, so claim it from the Pi's speaker. */
  function claimBrowserAudio() {
    if (state.audioClaimed) return;
    if (state.voiceMuted || voicePreference() !== "browser") return;
    if (!state.voiceAvailable || !state.voiceConfigured) return;
    if (state.voiceOut === "browser") return; // already ours
    state.audioClaimed = true; // one attempt per user action, not per snapshot
    api("/api/speech", { method: "POST", body: { mode: "browser" } })
      .then(function (payload) {
        state.voiceOut = payload.mode === "off" ? state.voiceOut : payload.mode;
        setControls();
      })
      .catch(function () { state.audioClaimed = false; });
  }

  /* ---------------------------------------------------------------- wiring */
  function focusBlock(id) {
    var block = $(id);
    if (!block) return;
    if (!window.matchMedia("(min-width: 1180px)").matches) openPanel(true);
    block.scrollIntoView({ behavior: "smooth", block: "start" });
    block.classList.add("flash");
    setTimeout(function () { block.classList.remove("flash"); }, 900);
  }

  var panelNode = null;
  var scrimNode = null;

  function openPanel(open) {
    if (!panelNode) panelNode = $("panel");
    if (!scrimNode) scrimNode = $("scrim");
    panelNode.classList.toggle("open", open);
    scrimNode.classList.toggle("show", open);
  }

  function init() {
    setState("idle", "connecting");
    setControls();
    tickClock();
    setInterval(tickClock, 1000);
    startBoot();
    pushFeed("console starting");

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

    button("btn-mic").addEventListener("click", toggleMic);
    button("btn-live").addEventListener("click", function () { setLive(!state.live); });
    button("btn-mic-mute").addEventListener("click", toggleMicMute);
    button("btn-voice-mute").addEventListener("click", toggleVoiceMute);
    button("btn-stop").addEventListener("click", stopAll);
    button("btn-clear").addEventListener("click", clearChat);
    button("btn-confirm-yes").addEventListener("click", function () { confirm(true); });
    button("btn-confirm-no").addEventListener("click", function () { confirm(false); });
    $("btn-out-browser").addEventListener("click", function () { setVoiceOut("browser"); });
    $("btn-out-device").addEventListener("click", function () { setVoiceOut("device"); });

    button("btn-reset-providers").addEventListener("click", function () {
      api("/api/providers/reset", { method: "POST" })
        .then(function () { refresh().catch(function () {}); })
        .catch(function () {});
    });

    // The transport buttons are the same MPRIS control the voice commands use,
    // so a tap here and "pause the music" from across the room do one thing.
    // (Scoped to the transport: <body> carries data-media too, for the styling.)
    Array.prototype.forEach.call(document.querySelectorAll("#transport [data-media]"), function (node) {
      node.addEventListener("click", function () {
        mediaAction(node.getAttribute("data-media") || "toggle");
      });
    });

    // Delegated on the document: the suggestion chips are built at runtime in
    // the intro message, so there is nothing stable to bind them to.
    document.addEventListener("click", function (event) {
      var target = /** @type {HTMLElement} */ (event.target);
      if (!target || !target.closest) return;
      var chip = target.closest(".chip");
      if (!chip) return;
      send(chip.getAttribute("data-fill") || chip.textContent || "");
    });

    var visitorClear = $("btn-visitor-clear");
    if (visitorClear) {
      visitorClear.addEventListener("click", function () { send("the guest has left"); });
    }

    // The rail is a shortcut into the telemetry column, not decoration: each
    // button scrolls to a real block and highlights it.
    Array.prototype.forEach.call(document.querySelectorAll(".rail-btn[data-goto]"), function (node) {
      node.addEventListener("click", function () { focusBlock(node.getAttribute("data-goto")); });
    });

    button("btn-panel").addEventListener("click", function () { openPanel(true); });
    button("btn-panel-close").addEventListener("click", function () { openPanel(false); });
    $("scrim").addEventListener("click", function () { openPanel(false); });

    // The browser needs one gesture before it will play audio.
    document.addEventListener("click", unlockAudio);
    document.addEventListener("touchstart", unlockAudio);

    // ...and any gesture gets past the boot screen if it is still showing.
    document.addEventListener("click", function () { finishBoot("skipped"); });
    document.addEventListener("keydown", function () { finishBoot("skipped"); });

    window.addEventListener("beforeunload", function () {
      if (recorder.active()) recorder.cancel();
    });

    seedIntro();
    refresh().catch(function () {});
    refreshMedia();
    connect();
    setInterval(function () { refresh().catch(function () {}); }, 20000);
    // Media changes without JARVIS (a song ending, a phone pausing Spotify):
    // cheap enough to check every 10s, and never while the tab is hidden.
    setInterval(refreshMedia, 10000);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
