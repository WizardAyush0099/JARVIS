# JARVIS

A personal AI assistant that runs on a **Raspberry Pi 4**.

It listens, talks, remembers, searches the web, controls your machine, writes and
reads files, generates images, sends email with your permission, and drives GPIO
hardware - through a browser interface you can open from your phone.

```bash
sh scripts/install.sh --voice --hardware     # install
nano .env                                    # add one API key
.venv/bin/python main.py                     # run
```

---

## Contents

- [What this is](#what-this-is)
- [Quick start on a Raspberry Pi 4](#quick-start-on-a-raspberry-pi-4)
- [Opening JARVIS on your phone, the Pi, or VS Code](#opening-jarvis-on-your-phone-the-pi-or-vs-code)
- [Architecture](#architecture)
- [What you can ask for](#what-you-can-ask-for)
- [AI providers and automatic fallback](#ai-providers-and-automatic-fallback)
- [Voice](#voice)
- [Memory](#memory)
- [Hardware and GPIO](#hardware-and-gpio)
- [Safety and security](#safety-and-security)
- [Configuration reference](#configuration-reference)
- [Testing](#testing)
- [Troubleshooting](#troubleshooting)
- [Raspberry Pi 4 performance notes](#raspberry-pi-4-performance-notes)
- [Audit: what happened to the old project](#audit-what-happened-to-the-old-project)
- [Project layout](#project-layout)
- [Adding a new capability](#adding-a-new-capability)

---

## What this is

This is a complete rebuild of the earlier `JARVIS` folder (`Backend/`,
`Frontend/`, `Data/`, `main.py`). The old source could not be recovered, so the
rebuild was driven by the development specification in
[`JARVIS_UPGRADE_PROMPT.md`](JARVIS_UPGRADE_PROMPT.md) plus the file layout and
feature list from the previous version. Everything the old project did is still
here; the parts that were fragile were re-engineered rather than copied.

Highlights of the rebuild:

| Concern | Before | Now |
|---|---|---|
| Understanding | `FirstLayerDMM` keyword if/elif | deterministic fast path **plus** a real model planning tool calls |
| AI vendor | one hard-coded provider | a provider chain with automatic failover and cooldowns |
| GUI | PyQt5, blocked the interface | browser UI (no build step) + optional Tkinter window, never blocking |
| Voice out | Google TTS piped into `pygame` | pluggable TTS: Edge neural, Piper (offline), pyttsx3, espeak, behind a queue |
| Voice in | one blocking `listen()` loop | pluggable STT with wake word, push-to-talk, and failures as data |
| Memory | unbounded `ChatLog.json` | bounded conversation + durable facts, with migration of the old file |
| Crash safety | `eval()`, unguarded calls | sandboxed calculator, per-tool timeouts, redacted logs, recoverable deletes |

---

## Quick start on a Raspberry Pi 4

### 1. Requirements

- Raspberry Pi 4 (2 GB is enough, 4 GB is comfortable)
- Raspberry Pi OS Bookworm or Bullseye, 64-bit recommended
- Python **3.9 or newer** (3.11 ships with Bookworm)
- A microphone and speaker for voice (any USB headset works)

### 2. Install

```bash
git clone https://github.com/WizardAyush0099/JARVIS.git
cd JARVIS
sh scripts/install.sh --voice --hardware
```

The installer creates a virtual environment, installs the core packages, runs the
diagnostics, and tells you what is still missing. Without `--voice`/`--hardware`
you get a text-only assistant.

If PyAudio fails (it needs the PortAudio headers):

```bash
sudo apt update && sudo apt install -y portaudio19-dev flac mpg123 espeak-ng
pip install -r requirements-voice.txt
```

### 3. Configure

```bash
cp env.example .env
nano .env
```

Set **at least one** AI provider key - Google Gemini and Groq both have generous
free tiers:

```ini
GEMINI_API_KEY=your-key-from-aistudio.google.com
```

You can also leave every key empty: JARVIS still runs on its built-in offline
engine (time, maths, unit conversions, system status, files, notes, reminders,
memory and your GPIO devices) and fails over to it automatically whenever the
network or a quota runs out.

> The template is called `env.example` rather than `.env.example` so it is never
> mistaken for a real secrets file. Copy it to `.env` - `.env` is git-ignored.

### 4. Verify, then run

```bash
.venv/bin/python main.py --check        # plain-language health report
.venv/bin/python main.py                # web interface
```

`--check` reports your providers, keys, tools, microphone, speakers, GPIO backend
and internet state. Run it first whenever something misbehaves.

---

## Opening JARVIS on your phone, the Pi, or VS Code

JARVIS serves a browser interface on port `8765`, bound to `0.0.0.0` so every
device on your network can reach it. It is a single page with no build step, no
CDN and no external fonts, so it works on a phone over a flaky connection and on
the Pi itself with no internet at all.

```bash
.venv/bin/python main.py
```

The startup banner prints two addresses:

```
  local   : http://localhost:8765/
  network : http://192.168.1.42:8765/
```

| Where | How |
|---|---|
| **Phone** | Open the `network` address. Use *Add to Home Screen* for a full-screen app. |
| **Pi (monitor attached)** | Open `http://localhost:8765/` in Chromium, or run `python main.py --gui` for the native Tkinter window. |
| **Laptop** | Open the same `network` address. |
| **VS Code (on the Pi or remote)** | Open the folder, then press **F5** and pick *JARVIS: web interface*. VS Code forwards the port - open the forwarded `8765` URL. Tasks and debug configs are in `.vscode/`. |
| **Terminal only** | `python main.py --cli` |

On any network you do not fully trust, set a token so strangers cannot drive your
assistant:

```ini
JARVIS_WEB_TOKEN=pick-something-long
```

Then open `http://<pi-ip>:8765/?token=pick-something-long`. The page keeps the
token for the session; without it the API returns `401`.

![interface](https://img.shields.io/badge/theme-arc%20reactor-22d3ee)

The interface shows live state (idle / listening / thinking / working / speaking),
which provider answered, microphone and voice status, a tool activity feed, a
memory panel with your stored facts, a system panel, and a log tail. Every JARVIS
answer has its own **copy** button.

---

## Architecture

```
                       ┌──────────────────────────────────────────┐
  voice  ──┐           │              core/brain.py               │
  text   ──┼──► input ─┤  plan → execute → validate → answer      │
  web    ──┘           └───────┬──────────────────────┬───────────┘
                               │                      │
                    core/planner.py            core/memory.py
                  (fast path + model)      (conversation + facts)
                               │
                        core/router.py  ── confirmation gate, timeouts, events
                               │
                          tools/*  ── system · files · web · email · image
                               │        coding · utilities · hardware
                               │
                       hardware/gpio.py  ── mock backend | gpiozero
                               │
                            GPIO / sensors

  ai/manager.py ── provider chain with failover ──┐
  ai/offline.py ── rules engine (always available)─┘
```

The pipeline the spec asked for, in code:

`USER INPUT → INTENT/UNDERSTANDING → PLANNER → TOOL SELECTION → TOOL EXECUTION →
RESULT VALIDATION → AI RESPONSE → VOICE + GUI`

Each arrow is a real module boundary. Nothing calls a vendor directly, nothing
touches GPIO outside `hardware/`, and the AI never executes anything itself - it
emits a plan that is validated against the tool registry first.

---

## What you can ask for

**System**

> "system status" · "what's using the most RAM" · "cpu temperature" · "open VS Code" ·
> "open github.com" · "set the volume to 40" · "shut down the system" *(asks first)*

**Web**

> "search for Raspberry Pi 5 news" · "what's the weather in Delhi" ·
> "summarise https://example.com/article" · "open YouTube and search for Pi projects"

**Files**

> "create a file called notes.txt saying buy thermal paste" · "read data/ChatLog.json" ·
> "find my python project" · "list my documents folder" · "summarise report.pdf"

**Productivity**

> "what time is it" · "calculate 27 x 43" · "what's 15% of 240" ·
> "convert 5 km to miles" · "remind me to stretch in 20 minutes" · "show my notes"

**Memory**

> "remember my project is called Athena" · "what was my project called" ·
> "what do you remember" · "clear the conversation"

**Images**

> "generate an image of a futuristic city at night" *(renders in the UI and is saved to `assets/generated/`)*

**Email** *(asks for confirmation before sending)*

> "email rahul@example.com saying the project is ready"

**Coding**

> "explain app.py" · "why did I get a KeyError on line 12" ·
> "write a script that resizes images" · "run that script" *(asks first)*

**Hardware** *(declared in `config/hardware.json`; works offline)*

> "list my hardware devices" · "turn on the status LED" · "flash the status LED" ·
> "read room temperature" · "is the button pressed" · "set the servo to 90"

**Anything else** - explanations, writing, planning, study help - goes to the model
with your conversation and stored facts as context.

---

## AI providers and automatic fallback

Providers are tried in order, and a failing one is parked in a cooldown so the
next request skips straight to a healthy one:

```
gemini → groq → openrouter → openai → ollama → offline
```

- a **rate limit** cools a provider for ~90 s (longer if it keeps failing)
- a **rejected key** cools it for ~15 min, so one bad key never stalls every request
- a **transient network error** is retried in place, then fails over
- when everything online is down, the **offline engine** answers instead of erroring

Any provider you set a key for is used, even if it is not in `AI_PROVIDERS` - so
adding a key is enough to add a fallback. Providers with no key are listed in the
UI as *add a key* rather than being probed and timing out.

Supported out of the box: **Google Gemini**, **Groq**, **OpenRouter**, **OpenAI**,
**Together**, **Cerebras**, **Mistral**, **DeepSeek**, **SambaNova**, any
OpenAI-compatible endpoint, and **Ollama** for a fully local model:

```ini
OLLAMA_ENABLED=true
OLLAMA_BASE_URL=http://localhost:11434/v1
OLLAMA_MODEL=llama3.2
```

A good free-tier setup is **Gemini or Groq as the primary** (the most generous
daily limits), with **SambaNova as a low-latency fallback** (`SAMBANOVA_API_KEY`;
note its free tier is small per day, so it is a good second choice, not a
primary). Setting any provider's key is enough - it joins the chain
automatically, even if `AI_PROVIDERS` does not mention it.

Web search works with **no key at all** (DuckDuckGo, then DuckDuckGo Instant
Answers, then Wikipedia). Add `TAVILY_API_KEY` or `BRAVE_API_KEY` for a richer
first choice. Image generation defaults to **Pollinations**, which is free and
keyless.

> JARVIS never claims a tool worked when it did not. If a search found nothing, an
> email was refused, or an image failed, it says so and tells you what to change.

---

## Voice

**Speaking (TTS)** - engines are tried in this order and whichever is installed
wins; the default voice is chosen to be calm, deep and assistant-like:

| Engine | Needs | Notes |
|---|---|---|
| `edge` | internet at synthesis time | most natural; default (`en-GB-RyanNeural`) |
| `piper` | `piper-tts` + a model file | fully offline neural, light enough for a Pi 4 |
| `pyttsx3` | `pyttsx3` | offline, robotic |
| `espeak` | `espeak-ng` | last resort |

Speech runs on its own thread with a queue, so talking never blocks the interface.
Hindi/Hinglish is detected automatically and switches to `hi-IN-MadhurNeural`.

For Piper:

```bash
sudo apt install -y piper-tts
# or grab a voice model from https://github.com/rhasspy/piper
```

```ini
TTS_ENGINE=piper
PIPER_MODEL=/home/pi/voices/en_GB-alan-medium.onnx
PIPER_MODEL_HI=/home/pi/voices/hi_IN-pratham-medium.onnx
```

**Listening (STT)**

```ini
STT_ENABLED=true
STT_ENGINE=google      # google | whisper | sphinx | vosk
STT_WAKE_WORD=jarvis   # leave empty to respond to everything
```

Say *"Jarvis, what's the weather?"* - the wake word is stripped before the request
is handled. Press the microphone button in the UI (or `MIC` in the desktop window)
for push-to-talk, which always works.

For offline recognition on a Pi 4, Vosk is the best trade-off (a small model uses
about 60 MB of RAM):

```bash
pip install vosk
wget https://alphacephei.com/vosk/models/vosk-model-small-en-in-0.4.zip
unzip vosk-model-small-en-in-0.4.zip
```

```ini
STT_ENGINE=vosk
VOSK_MODEL_PATH=./vosk-model-small-en-in-0.4
```

Trade-off: Google is the most accurate but needs internet and sends audio to
Google. Whisper is accurate but too heavy for a Pi 4 in real time. Vosk and
PocketSphinx are offline and lighter but less accurate.

Listening is automatically paused while JARVIS speaks, so it never transcribes
its own voice.

---

## Memory

Three deliberately separate things:

- **Conversation** - the recent turns sent to the model, bounded by
  `JARVIS_HISTORY_TURNS` and a character budget, and bounded on disk too.
- **Facts** - durable things you asked it to remember (`remember my project is
  called Athena`). Capped, searchable, individually forgettable.
- **Chat log** - `data/ChatLog.json`, still written in the original
  `{"messages": [[role, text], ...]}` shape so anything you already built around
  that file keeps working.

`clear the conversation` empties the conversation and keeps your facts.

If an old `ChatLog.json` from the previous version is present, it is **imported
automatically** on first run - both the original `[[role, text], ...]` layout and
lists of `{"role", "content"}` objects are understood. Unreadable files are moved
aside as `ChatLog.corrupt-<timestamp>.json` instead of crashing the app.

---

## Hardware and GPIO

Declare devices in [`config/hardware.json`](config/hardware.json) and address them
by `id`:

```json
{
  "backend": "auto",
  "devices": [
    { "id": "status_led", "name": "Status LED", "kind": "led", "pin": 17 },
    { "id": "button", "name": "Push button", "kind": "button", "pin": 27 },
    { "id": "room_temp", "name": "Room temperature", "kind": "temperature", "pin": 4 }
  ]
}
```

Supported kinds: `led`, `button`, `relay`, `buzzer`, `servo`, `temperature`
(DS18B20 over 1-Wire), `distance`, `light`, `motion`.

Four tools cover them - `hardware_list`, `hardware_read`, `hardware_write` and
`hardware_pulse` - and a **spoken name is enough**: the device is found by id, by
name (`"the status LED"`) or, failing that, by kind (`"the LED"`), so a voice
command never depends on the exact id you chose.

> "list my hardware devices" · "turn on the status LED" · "flash the status LED" ·
> "read room temperature" · "is the button pressed"

These are deterministic commands (like "what time is it"), so they run instantly,
work **with no API key and no internet**, and never get confused with the Pi's own
thermal reading: "read room temperature" reads the DS18B20, while "cpu temperature"
still reports the Pi itself.

On a laptop, or when `gpiozero` is not installed, the hardware layer runs on a
**mock backend**. It mirrors the real backend's rules - outputs start off,
read-only kinds (buttons, sensors) refuse a write - and every value it reports is
labelled *(simulated)*, so development never passes where the Pi would fail. A
write to a button answers "`'button' is read-only`" rather than pretending it
worked. The AI layer never imports GPIO - it is always
`AI → tools → hardware → GPIO`.

---

## Safety and security

- **Confirmation before anything destructive.** Deleting a file, sending email,
  shutting down, restarting, closing an app and running a script all require an
  explicit *yes*. In the web UI this is a card with Yes/No buttons; by voice you
  just say yes or no.
- **No `eval`.** The calculator walks a restricted AST. `__import__`, `open`,
  attribute access and huge exponents are rejected.
- **A real file sandbox.** File tools only touch the project folder, your home
  directory and anything in `ALLOWED_PATHS`. `../../etc/passwd` is refused, not
  followed. Reads are capped so a 40 MB log cannot exhaust the Pi's RAM.
- **Deletes are recoverable.** `delete_file` moves things to `data/trash/`.
- **No shell interpolation.** Applications are launched from a list of arguments
  (`shell=False`), names containing `;`, `|`, `$`, backticks or newlines are
  refused, and launching is restricted to `ALLOWED_APPS`.
- **Secrets stay secret.** Keys live only in `.env`, are never printed, never
  returned by the API and never logged - every log record passes through a
  redaction filter that masks `sk-…`, `AIza…`, `gsk_…`, `Bearer …` and any
  `*_KEY=…`/`*_PASSWORD=…` pattern.
- **The API is closed by default.** Set `JARVIS_WEB_TOKEN` and every `/api/*` and
  `/ws` call needs it.
- **Bounded work.** Every tool call runs with a timeout on a daemon thread, so a
  hung command cannot freeze the interface or block shutdown.

`python main.py --check` verifies your configuration without running anything.

---

## Configuration reference

Everything is optional and lives in `.env` (see [`env.example`](env.example)).

| Variable | Default | Purpose |
|---|---|---|
| `JARVIS_NAME` / `JARVIS_OWNER` | `JARVIS` / `Ayush` | identity used in prompts and replies |
| `AI_PROVIDERS` | `gemini,groq,openrouter` | fallback order |
| `GEMINI_API_KEY`, `GROQ_API_KEY`, `OPENROUTER_API_KEY`, `OPENAI_API_KEY`, … | – | provider keys |
| `OLLAMA_ENABLED`, `OLLAMA_MODEL` | `false` | fully local model |
| `AI_TEMPERATURE`, `AI_MAX_TOKENS`, `AI_REQUEST_TIMEOUT`, `AI_MAX_RETRIES` | `0.4`, `900`, `45`, `2` | model behaviour |
| `JARVIS_HISTORY_TURNS` | `12` | conversation window sent to the model |
| `SEARCH_PROVIDER` | `duckduckgo` | `duckduckgo` / `tavily` / `brave` / `searxng` |
| `IMAGE_PROVIDER` | `pollinations` | `pollinations` or `openai` |
| `EMAIL_ENABLED`, `SMTP_*`, `EMAIL_AUTO_SEND` | `false`, …, `false` | SMTP sending |
| `TTS_ENABLED`, `TTS_ENGINE`, `TTS_VOICE_EN`, `TTS_VOICE_HI`, `TTS_RATE` | `true`, `edge`, … | speech output |
| `STT_ENABLED`, `STT_ENGINE`, `STT_WAKE_WORD`, `VOSK_MODEL_PATH` | `false`, `google`, `jarvis` | speech input |
| `JARVIS_WEB_HOST`, `JARVIS_WEB_PORT`, `JARVIS_WEB_TOKEN` | `0.0.0.0`, `8765`, – | web interface |
| `CONFIRM_DESTRUCTIVE`, `ALLOWED_APPS`, `ALLOWED_PATHS`, `TOOL_TIMEOUT` | `true`, … | safety limits |

---

## Testing

```bash
.venv/bin/python -m pytest tests -q          # 201 tests
.venv/bin/python -m pyflakes config core ai tools voice hardware gui server main.py   # clean
npx --yes -p typescript tsc -b --noEmit      # type-checks the browser client
```

The suite is hermetic: temporary directories, no API keys, no network. It covers
intent matching, expression safety, the file sandbox, the trash-based delete,
reminders, migration of the old `ChatLog.json`, provider failover and cooldowns,
plan validation, the confirmation flow, the GPIO tools and their spoken-name
resolution, the "never fake a result" guarantee, and the HTTP/WebSocket API.

`python main.py --check` is the manual equivalent for a real install - it is the
first thing to run on the Pi.

---

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| Blank page in the browser | The page served but could not reach the brain. Start it with `.venv/bin/python main.py --web` and reload; the UI shows an offline banner in that case. |
| `FastAPI is not installed` | `pip install -r requirements.txt` |
| Assistant answers "I don't have an AI provider available" | No key is set. Add one to `.env`, or start Ollama. `--check` shows which provider is missing. |
| `I'm running offline right now` | Every provider failed or is cooling down. Check the internet, then press *reset provider cooldowns* in the status panel. |
| Voice silent | Run `--check`. Install `pygame` (playback) and `edge-tts`, or `espeak-ng` for offline. |
| `speech output : none` | No TTS engine installed. `pip install -r requirements-voice.txt`. |
| Microphone not found | Install `pyaudio` and `portaudio19-dev`; check `arecord -l`. Try `STT_ENGINE=vosk` for offline. |
| Assistant talks then answers its own voice | It should not - make sure `STT_ENABLED=true` so listening pauses while speaking. |
| `email is not configured yet` | Set `EMAIL_ENABLED=true`, `SMTP_USER` and `SMTP_PASSWORD`. Gmail needs an **App Password**, not your normal password. |
| Email login rejected | Gmail/Outlook require an app password or OAuth; a normal password will be refused. |
| Image generation failed | Needs internet (Pollinations) or `OPENAI_API_KEY`. |
| The panel says `gpio mock` | `gpiozero` is missing, or this is not a Pi. On a Pi: `pip install -r requirements-hardware.txt`. Development off-Pi is meant to run this way. |
| Hardware tools say "I don't have a device called …" | That id is not in `config/hardware.json`. `list my hardware devices` prints the ones that are. |
| Search returns nothing useful | Your IP may be blocked by DuckDuckGo. Add `TAVILY_API_KEY` or `BRAVE_API_KEY`. |
| Everything is slow | See the performance notes below; usually it is thermal throttling or an SD card. |
| `Unable to open X display` with `--gui` | No graphical session. Use the web interface instead. |
| Want a fresh start | Stop JARVIS and delete `data/memory.json` and `data/ChatLog.json`. |

Logs rotate in `logs/jarvis.log` and are also visible live in the status panel.

---

## Raspberry Pi 4 performance notes

- **Nothing blocks the interface.** The brain, TTS, STT, reminders and every tool
  call run on their own daemon threads; the GUI and web UI only react to events.
- **Heavy work goes to an API.** Image generation, speech synthesis and
  summarisation are remote. The Pi only orchestrates, which is what makes a 2 GB
  board comfortable.
- **Animations are cheap.** The UI animates only `transform`/`opacity`, honours
  `prefers-reduced-motion`, and ships no web fonts or images.
- **Memory is bounded.** Conversation window, fact store, chat log, voice cache
  and HTTP response sizes all have caps.
- **Nothing is lazy if it costs RAM at import.** `psutil`, `gpiozero`, `vosk`,
  `edge_tts` and `pygame` are imported only when actually used; every metric has
  a `/proc` or `sysfs` fallback, so a minimal install still reports real numbers.
- **Audio is cached.** Repeated phrases are not re-synthesised.
- **Check power.** `--check` reads `vcgencmd get_throttled`. An under-voltage Pi
  4 runs at reduced clock - use a 5 V/3 A supply.

---

## Audit: what happened to the old project

The previous source was not present in the repository (only the specification
was), so this rebuild was driven by the spec and the recovered file layout. Every
old module has a direct successor, and the workarounds were rebuilt rather than
dropped:

| Old file | Successor | What changed |
|---|---|---|
| `Backend/Chatbot.py` (`FirstLayerDMM`) | `core/intent.py` + `core/planner.py` | keyword routing demoted to a fast path and offline fallback; a model now plans real tool calls |
| `Backend/Model.py`, `Chatbot.py` chat session | `ai/providers.py`, `ai/manager.py`, `core/brain.py` | one hard-coded model became a failover chain with cooldowns, retries and status |
| `Backend/RealtimeSearchEngine.py` | `tools/web.py` | keyless-first search with fallbacks, proper HTML parsing, real page fetching and summarisation |
| `Backend/ImageGeneration.py` | `tools/image.py` | pluggable provider, saves locally, returns the real path, reports honest failures |
| `Backend/SpeechToText.py` | `voice/stt.py` | pluggable engines, failures as values, wake word, push-to-talk, no GUI blocking |
| `Backend/TextToSpeech.py` | `voice/tts.py` | pluggable engines, queue thread, audio caching, non-blocking playback |
| `Backend/Automation.py` | `tools/system.py`, `tools/files.py`, `tools/utilities.py` | allow-listed app launching, sandboxed files, safe maths, per-tool timeouts |
| `Frontend/GUI.py` | `gui/web/` + `gui/desktop.py` | browser UI reachable from a phone; Tkinter window instead of PyQt5; a copy button on every answer |
| `Data/ChatLog.json` | `core/memory.py` | bounded, locked, atomically written, auto-migrating the old file |
| `Data/Voice.html` | `gui/web/` | grew into the full interface, served by `server/app.py` |
| `main.py` | `main.py` | added `--check`, `--web`, `--cli`, `--gui` and logging |

### The "jugaad" workarounds, and what happened to them

Each one was understood first, kept where it still earns its place, and replaced
where it was a liability:

1. **Piping TTS bytes straight into the mixer on the GUI thread.** Solved a
   missing player, but froze the interface. Now a queued speaker thread with a
   pluggable audio sink; the fallback to `aplay`/`mpg123`/`ffplay` is kept.
2. **`eval()` for arithmetic.** Made "calculate" work instantly. Replaced with a
   restricted AST evaluator - same speed, no remote code execution.
3. **Reading `ChatLog.json` in one place, unbounded.** Kept as the transcript
   format *and* auto-migrated, but bounded, locked and written atomically.
4. **Hard-coded absolute paths to `/home/pi/...`.** Replaced with paths derived
   from the project root, so it runs from anywhere.
5. **Sleep-and-poll loops for speech.** Replaced with event-driven state, so the
   UI updates the moment something happens.
6. **One API key in one place.** Replaced with the provider chain, and the
   keyless services (DuckDuckGo, Pollinations, wttr.in) are still the defaults so
   a fresh install works with no key at all.
7. **`shell=True` in automation.** Replaced with argument lists, allow-lists and
   a confirmation gate.

Where a workaround was still the pragmatic answer - a keyless image API, a
command-line audio fallback, a rules engine when offline - it was kept and given
a proper interface.

---

## Project layout

```
JARVIS/
├── main.py                  entry point: --web --gui --cli --check
├── env.example              configuration template (copy to .env)
├── JARVIS_UPGRADE_PROMPT.md the specification this rebuild follows
│
├── config/
│   ├── settings.py          env-driven settings, provider presets, redaction
│   └── hardware.json        GPIO device declarations
│
├── core/
│   ├── brain.py             the orchestrator
│   ├── planner.py           request → validated tool plan
│   ├── intent.py            deterministic fast path / offline rules
│   ├── router.py            confirmation gate, timeouts, tool history
│   ├── memory.py            conversation, facts, ChatLog migration
│   ├── events.py            thread-safe event bus for every front-end
│   ├── storage.py           atomic JSON persistence
│   └── logging_setup.py     rotating logs, secret redaction, log ring buffer
│
├── ai/
│   ├── manager.py           provider chain, cooldowns, JSON extraction
│   ├── providers.py         OpenAI-compatible, Gemini, Ollama, offline
│   ├── offline.py           the rules engine
│   └── http.py              stdlib HTTP with retries and size caps
│
├── tools/                   every capability, self-registering
│   ├── base.py              registry, validation, safe execution
│   ├── system.py            metrics, processes, apps, URLs, power
│   ├── files.py             sandboxed file operations
│   ├── web.py               search, fetch, summarise, YouTube, weather
│   ├── email_tool.py        SMTP with confirmation
│   ├── image.py             image generation
│   ├── coding.py            explain, debug, create, patch, run
│   └── utilities.py         maths, units, notes, reminders, memory
│
├── voice/
│   ├── stt.py               speech to text
│   └── tts.py               text to speech and playback
│
├── hardware/gpio.py         device declarations, mock + gpiozero backends,
│                            and the GPIO tools (list · read · write · pulse)
│
├── server/app.py            FastAPI: REST, WebSocket, media, token auth
├── gui/
│   ├── web/                 the browser interface (plain HTML/CSS/JS)
│   └── desktop.py           optional Tkinter window
│
├── tests/                   201 hermetic tests
├── scripts/                 install.sh, run.sh
├── data/                    memory, chat log, notes, reminders, trash
├── assets/generated/        images JARVIS creates
├── assets/voice_cache/      cached speech
├── logs/                    rotating logs
└── .vscode/                 launch configs and tasks
```

---

## Adding a new capability

One function and one decorator. The planner, the offline engine and the UI all
pick it up automatically:

```python
# tools/mytool.py
from tools.base import ToolResult, tool


@tool(
    name="read_indoor_climate",
    description="Report temperature and humidity from the DHT22 sensor.",
    parameters={"type": "object", "properties": {}},
    category="hardware",
    offline_safe=True,          # may run with no internet
)
def read_indoor_climate() -> ToolResult:
    return ToolResult.success("22.4 C and 41% humidity")
```

Then add the module to `MODULES` in `tools/__init__.py`. That is the whole
extension story: the JSON schema taught to the model, argument validation,
confirmation gating, timeouts, logging and the activity feed are all handled by
the framework. A `category` that is listed in `ai/offline.py`'s
`OFFLINE_CATEGORIES` also gets an offline path for free, provided the tool is
`offline_safe` and not destructive.

---

## Credits

Built for Ayush, for a Raspberry Pi 4. Voice, search and image providers are
third-party free services; no personal voice is cloned. The assistant is intended
for personal use on your own network - put a token on it before exposing it to
anyone else.
