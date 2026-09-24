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
    voiceAvailable: false,
    voiceConfigured: false,
    micAvailable: false,
    listening: false,
    speaking: false,
    needsUnlock: false,
    lastState: "idle",
    liveTimer: null,
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
  function setState(name, note) {
    if (!name) return;
    state.lastState = name;
    document.body.setAttribute("data-state", name);
    var tone = "idle";
    if (name === "listening") tone = "ok";
    else if (name === "thinking" || name === "working") tone = "busy";
    else if (name === "error") tone = "warn";
    setPill($("pill-state"), note || name, tone);
    var label = $("state-label");
    if (label) label.textContent = (state.live ? "live talk · " : "") + (note || name);
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

  function seedIntro() {
    clearThread();
    var article = document.createElement("article");
    article.className = "msg intro";
    article.innerHTML =
      '<div class="msg-body">' +
      '<div class="hero" aria-hidden="true"><span class="hero-ring"></span>' +
      '<span class="hero-ring hero-ring-2"></span><span class="hero-core">J</span></div>' +
      "<p><strong>" + escapeHtml(identity.assistant) + "</strong> is online.</p>" +
      '<p class="muted">Type a message, press <strong>Mic</strong> to talk, or start ' +
      "<strong>Live Talk</strong> for a hands-free conversation.</p>" +
      '<div class="suggestions" id="suggestions">' +
      ['system status', "what time is it", "search for Raspberry Pi 5 news",
        "generate an image of a futuristic city at night", "list my hardware devices",
        "remember my project is called Athena"].map(function (text) {
        return '<button class="chip" data-fill="' + escapeHtml(text) + '">' + escapeHtml(text) + "</button>";
      }).join("") +
      "</div></div>";
    thread().appendChild(article);
  }

  var identity = { assistant: "JARVIS", owner: "Ayush" };

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

  function renderSystem(status) {
    var hardware = status.hardware || {};
    var speech = status.speech || {};
    var mic = status.mic || {};
    statRow($("system-stats"), [
      ["voice", speech.engine || "off"],
      ["mic", mic.engine || "off"],
      ["gpio", hardware.backend || "none"],
      ["tools", status.tools || 0],
    ]);
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

  function renderVoice(status) {
    var speech = status.speech || {};
    var mic = status.mic || {};
    state.voiceAvailable = !!speech.available;
    state.micAvailable = !!mic.enabled;
    statRow($("voice-stats"), [
      ["voice", speech.engine || "none"],
      ["out", state.voiceMuted ? "off" : (status.voice_output || state.voiceOut)],
      ["mic", mic.engine || "none"],
      ["live", state.live ? "on" : "off"],
    ]);
    var pillNote = $("state-note");
    if (pillNote) {
      pillNote.textContent = "voice " + (speech.engine || "none") + " · mic " + (mic.engine || "none");
    }
    var note = $("voice-note");
    if (!speech.available) {
      note.textContent = "No speech engine is installed on the machine running JARVIS, " +
        "so replies are text only. Install the voice requirements, then reload.";
    } else if (!mic.available) {
      note.textContent = "The microphone engine is not installed on the Pi, so the " +
        "Microphone and Live Talk buttons use this device's microphone through /api/transcribe.";
    } else if (!mic.enabled) {
      note.textContent = "Microphone input is disabled in .env (STT_ENABLED=false). " +
        "Live Talk still works from this device's microphone.";
    } else {
      note.textContent = "Voice is synthesized by the backend. Choose where it is played.";
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
    var micLabel = button("btn-mic-mute").querySelector("span");
    if (micLabel) micLabel.textContent = state.micMuted ? "Mic off" : "Mic on";
    var voiceLabel = button("btn-voice-mute").querySelector("span");
    if (voiceLabel) voiceLabel.textContent = state.voiceMuted ? "Voice off" : "Voice on";
    document.body.setAttribute("data-mic", state.micMuted ? "off" : "on");
    document.body.setAttribute("data-voice", state.voiceMuted ? "off" : "on");
    document.body.setAttribute("data-live", state.live ? "on" : "off");
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

    return api("/api/chat", { method: "POST", body: { text: message } })
      .then(function (payload) {
        removeTyping();
        return handleReply(payload.reply);
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
    if (!state.live) button("btn-mic").disabled = busy;
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
        if (error && error.message === "cancelled") { setState("idle", "idle"); return null; }
        addMessage("system", (error && error.message) || "the microphone failed");
        setState("error", "mic");
        if (/speech engine|not installed/i.test(error.message) && state.live) {
          // nothing can transcribe on this machine: stop rather than loop on failure
          setLive(false);
          addMessage("system",
            "Live Talk stopped. Install the speech engine on the machine running JARVIS " +
            "(sh scripts/install.sh --voice), or set STT_ENABLED=true, then reload.");
        }
        return null;
      });
  }

  function toggleMic() {
    if (state.busy) return;
    if (recorder.active()) {
      recorder.finish();  // stop now and send what was said
      return;
    }
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

    if (recorder.supported()) {
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
    if (!pending) { box.classList.add("hidden"); return; }
    $("confirm-text").textContent = pending.question || "Confirm this action?";
    $("confirm-meta").textContent = "tool: " + (pending.tool || "?");
    box.classList.remove("hidden");
    $("btn-confirm-yes").focus();
  }

  /* ---------------------------------------------------------------- snapshot */
  function applySnapshot(payload, keepThread) {
    if (!payload) return;
    var who = payload.identity || {};
    if (who.assistant) {
      identity.assistant = who.assistant;
      identity.owner = who.owner || identity.owner;
      document.title = who.assistant;
      $("intro-name").textContent = who.assistant;
      $("brand-sub").textContent = "personal ai · " + identity.owner;
    }
    var status = payload.status || {};
    setState(status.state || "idle", status.state_note || status.state);
    renderProviders(status.providers);
    renderMemory(status.memory, status.facts);
    renderSystem(status);
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
    setPill($("pill-voice"), !speech.available ? "no voice" : (state.voiceMuted ? "muted" : speech.engine || "voice"),
      !speech.available ? "warn" : (state.voiceMuted ? "warn" : "ok"));
    var mic = status.mic || {};
    setPill($("pill-mic"), state.micMuted ? "muted" : (mic.engine || "off"),
      state.micMuted ? "warn" : (mic.enabled ? "ok" : "muted"));
    var providers = status.providers || [];
    var live = providers.filter(function (item) { return item.status === "ready"; });
    setPill($("pill-brain"), live.length ? live[0].slug : "offline", live.length ? "ok" : "warn");
    setControls();

    if (!keepThread) renderHistory(payload.messages);
  }

  function setOnline(online, why) {
    state.online = online;
    var banner = $("offline-banner");
    setPill($("pill-link"), online ? "connected" : "offline", online ? "ok" : "warn");
    if (online) {
      banner.classList.add("hidden");
      button("btn-send").disabled = state.busy;
    } else {
      banner.classList.remove("hidden");
      var detail = banner.querySelector("span");
      if (why && detail) detail.textContent = why;
      setPill($("pill-brain"), "offline", "warn");
      button("btn-send").disabled = true;
    }
  }

  function refresh() {
    return api("/api/state").then(function (payload) {
      setOnline(true);
      applySnapshot(payload, true);
      return payload;
    }).catch(function (error) {
      setOnline(false, "This page is served but it cannot reach the JARVIS brain (" + error.message +
        "). Start it with `.venv/bin/python main.py`, then reload.");
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

  /** A web client plays the voice itself, so claim it from the Pi's speaker. */
  function claimBrowserAudio() {
    if (state.voiceMuted || state.voiceOut === "device") return;
    if (!state.voiceAvailable || !state.voiceConfigured) return;
    api("/api/speech", { method: "POST", body: { mode: "browser" } })
      .then(function (payload) {
        state.voiceOut = payload.mode === "off" ? state.voiceOut : payload.mode;
        setControls();
      })
      .catch(function () {});
  }

  /* ---------------------------------------------------------------- wiring */
  function init() {
    setState("idle", "connecting");
    setControls();

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

    // Delegated, so the suggestion chips keep working after the thread is rebuilt.
    thread().addEventListener("click", function (event) {
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
    button("btn-panel").addEventListener("click", function () { openPanel(true); });
    button("btn-panel-close").addEventListener("click", function () { openPanel(false); });
    scrim.addEventListener("click", function () { openPanel(false); });

    // The browser needs one gesture before it will play audio.
    document.addEventListener("click", unlockAudio);
    document.addEventListener("touchstart", unlockAudio);

    window.addEventListener("beforeunload", function () {
      if (recorder.active()) recorder.cancel();
    });

    seedIntro();
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
