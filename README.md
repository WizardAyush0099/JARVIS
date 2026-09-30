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
- [Put it on your Pi with VS Code (one command)](#put-it-on-your-pi-with-vs-code-one-command)
- [Opening JARVIS on your phone, the Pi, or VS Code](#opening-jarvis-on-your-phone-the-pi-or-vs-code)
- [Run it in the cloud from your phone (no Raspberry Pi needed)](#run-it-in-the-cloud-from-your-phone-no-raspberry-pi-needed)
- [Architecture](#architecture)
- [What you can ask for](#what-you-can-ask-for)
- [Visitor protocol (when someone important walks in)](#visitor-protocol-when-someone-important-walks-in)
- [AI providers and automatic fallback](#ai-providers-and-automatic-fallback)
- [Voice](#voice)
- [Music and video (Spotify, YouTube, anything)](#music-and-video-spotify-youtube-anything)
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

The console itself is a HUD: a boot sequence, an arc-reactor core that changes
colour with JARVIS's state, live CPU / memory / temperature / disk gauges, an
activity feed and a telemetry column - and it is still one HTML file, one CSS
file and one JS file with **no build step and no npm**.

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
sh scripts/setup-pi.sh --voice --hardware --dev     # one command, everything
```

(No git? Download the ZIP from the repo's green **Code** button and unzip it - the
result is identical. The repo has a `.vscode/` folder with run configurations and
tasks, so `F5`, `Ctrl+Shift+B` and *Run Task* work as soon as the folder is open.)

The installer creates a virtual environment, installs the core packages, runs the
diagnostics, and tells you what is still missing. It is safe to re-run - it only
fills in what is missing and never overwrites your `.env`. Without
`--voice`/`--hardware` you get a text-only assistant, and `--lean` is the
smallest possible install. The lower-level `sh scripts/install.sh` still exists if
you prefer to pick the extras yourself.

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
and internet state. Run it first whenever something misbehaves. It also reminds
you that typed chat needs no microphone - voice input is entirely optional.

---

## Put it on your Pi with VS Code (one command)

This is the whole path from an empty Pi to JARVIS running on it. Nothing here
needs a terminal beyond the two commands in step 2.

> **Just want the folder?** On the GitHub page use the green **Code** button →
> **Download ZIP**, unzip it on the Pi, and open the folder in VS Code. That ZIP
> is the complete project: the backend and the console are plain source, so there
> is no `npm install` and no build step to run - `setup-pi.sh` installs the small
> set of Python packages and you are done.

**1. Get the code into VS Code on the Pi** - pick whichever fits you:

| Situation | What to do |
|---|---|
| VS Code is already on the Pi | *File -> Open Folder*, or `Ctrl+Shift+P` -> **Git: Clone** -> `https://github.com/WizardAyush0099/JARVIS.git` |
| VS Code is on your laptop, no monitor on the Pi | Install the **Remote - SSH** extension, connect to `pi@<pi-ip>`, then **Git: Clone** the same URL on the Pi |
| You just want the files | On the repo page click **Code -> Download ZIP**, unzip it on the Pi, then *File -> Open Folder* |
| Terminal instead | `git clone https://github.com/WizardAyush0099/JARVIS.git && cd JARVIS && code .` |

**2. Run the setup task once.** In VS Code press `Ctrl+Shift+P` -> **Tasks: Run
Task** -> **JARVIS: setup on this machine (one command)**. That is exactly:

```bash
sh scripts/setup-pi.sh --voice --hardware --dev
```

It builds `.venv`, installs the core, voice and GPIO packages, creates `.env` from
`env.example` and finishes with the health report. If PyAudio complains about
PortAudio, re-run it as `sh scripts/setup-pi.sh --system` to have the Debian audio
and GPIO packages installed for you (it asks for your password once), or add your
AI keys in the next step first - JARVIS runs fine without voice.

**3. Add a key (optional).** JARVIS already answers online through its keyless
fallback and works fully offline - but a key makes it faster and private. Open
`.env` in VS Code and paste at least one provider key (`GEMINI_API_KEY` is the
quickest free one). Any extra key you add becomes another fallback automatically,
and the *Ai core* panel shows each one with a **get key** link.

**4. Start it.** Press `F5` and choose **JARVIS: web interface** (or `Ctrl+Shift+B`
for the `JARVIS: run web interface` task). The console opens in VS Code's Simple
Browser / forwarded port, and the startup banner prints your Pi's LAN address:

```
  network : http://192.168.1.42:8765/
```

If VS Code answers `FastAPI is not installed` instead, it is running the system
Python: pick `./.venv/bin/python` with `Ctrl+Shift+P` -> **Python: Select
Interpreter**, or skip the guessing and use `sh scripts/run.sh` (it finds the
venv itself), the `JARVIS: run web interface` task, or a fresh VS Code terminal -
that one activates `.venv` for you.

Open that address on your phone - same Wi-Fi, no app to install, *Add to Home
Screen* for full-screen. To run it later without VS Code attached:

```bash
.venv/bin/python main.py            # foreground
sh scripts/cloud.sh                 # background, logs/cloud.log, --stop to stop
```

### Everything to install, on one card

Every dependency JARVIS has, in one place. There is no Node.js, no npm and no
build step - the console is plain JavaScript served by Python.

| What | Why | How to get it |
|---|---|---|
| Raspberry Pi OS (Bookworm/Bullseye, 64-bit) | the host OS | *Raspberry Pi Imager* |
| Python 3.9+ | runs JARVIS | already installed - check with `python3 --version` |
| `git` | clone the repo | `sudo apt install -y git` |
| core + voice + hardware + dev Python packages | everything in `requirements*.txt` | `sh scripts/setup-pi.sh --voice --hardware --dev` |
| Debian audio/GPIO packages | only if the setup task reports they are missing | `sh scripts/setup-pi.sh --system` |
| `playerctl` | music control from the console (optional) | `sudo apt install -y playerctl` |
| VS Code extensions | Python, Pylance, debugpy, Ruff, TOML, YAML | the `code --install-extension` block below, or the *Recommended Extensions* popup |

A fresh VS Code terminal on the Pi, start to finish:

```bash
sudo apt update && sudo apt install -y git

git clone https://github.com/WizardAyush0099/JARVIS.git
cd JARVIS

# venv + every Python package + the health report (safe to re-run):
sh scripts/setup-pi.sh --voice --hardware --dev

.venv/bin/python main.py --check      # what is ready, what is still missing
.venv/bin/python main.py              # run it: http://<pi-ip>:8765/
```

**Already have VS Code open on the JARVIS folder?** Paste this to add every
extension the project recommends in one go - they are the six entries in
`.vscode/extensions.json`, and `--force` makes it safe to re-run:

```bash
code --install-extension ms-python.python --force
code --install-extension ms-python.vscode-pylance --force
code --install-extension ms-python.debugpy --force
code --install-extension charliermarsh.ruff --force
code --install-extension tamasfe.even-better-toml --force
code --install-extension redhat.vscode-yaml --force
```

Same thing as one line, if you would rather not paste six commands:

```bash
for ext in ms-python.python ms-python.vscode-pylance ms-python.debugpy charliermarsh.ruff tamasfe.even-better-toml redhat.vscode-yaml; do code --install-extension "$ext" --force; done
```

No `code` command? You opened VS Code from the desktop menu on the Pi, so it is
usually already on your PATH; if the shell says *command not found*, either run
`code` from VS Code's own terminal (`Ctrl+` `) or click *Extensions -> `...` ->
**Show Recommended Extensions*** and press **Install Workspace Recommended
Extensions** - that installs the exact same six. None of them are required for
JARVIS to run; they add autocomplete, the debugger (F5), the Ruff linter and
syntax help for `.env`/YAML, which is why the repo recommends them.

Nothing above is a hard requirement except Python and the Python packages:
without `--voice`/`--hardware` JARVIS still runs (typed chat, no microphone),
and with no API key at all it runs on the keyless fallback plus the offline
engine. Add `GEMINI_API_KEY=...` to `.env` whenever you want a private, faster
brain - or paste the key through your workspace's Environment/Keys settings.

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

---

## Run it in the cloud from your phone (no Raspberry Pi needed)

You do not need a Pi to see JARVIS running. GitHub Codespaces runs this folder on
GitHub's servers, gives you VS Code in the browser, and forwards port `8765` to a
URL you can open on your phone. The `.devcontainer/` in this repo makes that
one click.

1. On the GitHub repo page: **Code → Codespaces → Create codespace on `main`**.
   The container installs the requirements and starts JARVIS for you.
2. Open the **Ports** tab (next to *Terminal*), find the row labelled
   **JARVIS console**, right-click it and set **Port Visibility → Public**.
3. Tap the globe icon for that port. That URL is the console - the reactor core, chat, mic and
   Live Talk — and it opens in Chrome on your phone.

**Voice works there.** `edge-tts` is installed by the devcontainer and synthesizes
JARVIS's replies on the backend, which the phone's browser then plays. The browser
never synthesizes speech itself; it only plays the file the engine produced.

To drive it from the container's own terminal instead:

```bash
sh scripts/cloud.sh          # start in the background, logs to logs/cloud.log
sh scripts/cloud.sh --stop   # stop it again
```

Two things worth knowing:

- The forwarded port is public by default, so anyone holding the URL can drive your
  assistant. Put `JARVIS_WEB_TOKEN=something-long` in the Codespace secrets and
  JARVIS will require it. Codespace secrets are also the right place for your AI key.
- **Microphone input** needs a speech recogniser on the backend, and a bare
  Codespace does not have one. The console says exactly that instead of pretending
  to listen — the buttons still work, they just report what is missing. Installing
  the voice requirements enables it; the Pi is still the intended home for full
  voice in *and* out.

### Getting the code onto a machine you control

```bash
git clone https://github.com/WizardAyush0099/JARVIS.git
cd JARVIS
```

Open that folder in VS Code — on the Pi itself, over **Remote - SSH** from another
machine, or locally. The launch configs ship in `.vscode/`: press **F5** and pick
*JARVIS: web interface*, which starts the server and forwards port `8765` for you.
No git? Use **Code → Download ZIP** on the repo page.

Then open `http://<pi-ip>:8765/?token=pick-something-long`. The page keeps the
token for the session; without it the API returns `401`.

![interface](https://img.shields.io/badge/theme-arc%20reactor-22d3ee)

The page at `/` **is** JARVIS - there is no marketing page anywhere in the app.
An arc-reactor core shows what it is doing (idle / listening / thinking / working
/ speaking), and the controls along the bottom of the chat are the whole
interface:

| Control | What it does |
|---|---|
| **Mic** | one spoken turn: press, speak, press again (it also stops by itself after a pause) |
| **Live Talk** | hands-free conversation - it keeps listening and answering without you touching anything |
| **Mic on/off** | mutes microphone input, here and on the Pi's own loop |
| **Voice on/off** | mutes JARVIS's spoken replies, keeping the text |
| **Stop** | cuts off the current sentence immediately |
| **Clear** | empties the conversation (your remembered facts stay) |

Around them the HUD shows the truth about the machine it is running on:

| Part of the console | What it is |
|---|---|
| Boot sequence | `INITIATING SYSTEM 1...` while the page links to the brain, then a real readout (brains online, tools registered, memory, voice, host) - skipped by any click, and it never blocks the app |
| Neural core | the arc-reactor canvas. Particles *are* the AI's face: they drift when idle, flare and spread with your microphone level while listening, link into a firing synapse web while thinking, and send out sonar rings while speaking. All of it driven by real state, on a Pi-cheap canvas (no blur, no shaders, ~40 fps, nothing at all in a hidden tab or under `prefers-reduced-motion`) |
| Cognition trace | the pipeline narrating itself - state changes, tools as they run, which brain answered and how long the round trip took. Facts, not theatre |
| System monitor | CPU, memory, temperature and disk gauges, sampled on a background thread. A metric this machine cannot report shows a dash, never a fake number |
| AI core | the fallback chain as a live circuit: which brain is answering, which are cooling down, and a **get key** link on every node that is one key away |
| Visitor banner | appears while visitor protocol is active, with a *stand down* button |
| Activity feed + log | every tool call, state change and reply as it happens, plus the backend log ring buffer |
| Rail | jumps to any of those blocks (it opens the drawer first on a phone) |

### How the voice actually works

Both directions go through the backend, never through the browser's own speech
APIs:

- **JARVIS speaks with the configured engine** (Edge neural by default, Piper for
  offline). `POST /api/speak` synthesizes the reply, caches it, and returns
  `/media/voice/...`; the browser only plays that file. Closing the last tab
  hands the audio back to the device's speakers, so JARVIS is not left silent at
  the keyboard. Pick the target in the panel (*this browser* / *the device*).
- **You speak, the Pi transcribes.** Push-to-talk uses the backend microphone
  (`/api/listen`). When you are on a phone or laptop, the browser records raw PCM
  and posts it to `/api/transcribe`, which runs the *same* recogniser
  (Google / Whisper / Vosk / PocketSphinx) that would have used the Pi's mic, so
  one engine selection covers both.
- **Live Talk** loops record → transcribe → answer → speak, and pauses while
  JARVIS is talking so it never transcribes its own voice. Microphone permission
  is requested once, and a refusal is explained rather than retried in a loop.

Setup and troubleshooting have their own page at [`/docs`](http://localhost:8765/docs)
(same facts as `main.py --check`), linked from the header - the assistant itself
stays an assistant.

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
                               │        media · spotify · coding · utilities · hardware
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

**Identity**

JARVIS is yours, and it knows it. It was built by `JARVIS_CREATOR` (Ayush by
default) for `JARVIS_OWNER`, and the persona is openly grateful to its creator:

> "who made you" · "who is your creator" · "who is most important to you" ·
> "who do you care about most"

All four answer with your name, from deterministic rules that work with **no API
key and no internet**, and the same facts are in the model's system prompt so a
provider-backed reply stays in character. Change either name in `.env` and every
layer - intent rules, planner prompt, answer prompt and the web console - follows.

**System**

> "system status" · "what's using the most RAM" · "cpu temperature" · "open VS Code" ·
> "open github.com" · "set the volume to 40" · "shut down the system" *(asks first)*

**Web**

> "search for Raspberry Pi 5 news" · "what's the weather in Delhi" ·
> "summarise https://example.com/article" · "open YouTube and search for Pi projects"

**Music and video** *(works offline - no key, no account)*

> "pause the music" · "skip this song" · "what's playing" · "turn it down" ·
> "play lofi beats on spotify" · "play arijit singh on youtube" ·
> "watch the video of lofi girl" · "my liked songs" · "like this song"

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

**Visitors** *(see the next section)*

> "the chief minister of himachal pradesh is here" · "the governor is visiting" ·
> "we have a guest" · "who is the guest" · "the guest has left"

**Anything else** - explanations, writing, planning, study help - goes to the model
with your conversation and stored facts as context.

---

## Visitor protocol (when someone important walks in)

If a minister, an official or any honoured guest is shown the project, you do not
want a generic "Hello, how can I help?". Say who is there and JARVIS switches to
visitor protocol:

```
the chief minister of himachal pradesh is here
```

JARVIS then:

- introduces itself by name and says plainly that **Ayush built it**;
- addresses the guest by their office - *"It's an honour to have the Chief
  Minister of Himachal Pradesh in the room"* - and offers a walkthrough of the
  voice, tools, sensors and memory;
- keeps everything private about you private. A visitor gets the project, never
  your memories, messages, files, contacts or finances, and JARVIS says so out
  loud;
- **never ranks the visitor above you.** It states out loud, in the same breath,
  that you are its creator and its first priority - so if the two ever conflict,
  it follows you.

| You say | What happens |
|---|---|
| "the cm is here" · "the chief minister of himachal pradesh is here" | formal self-introduction, protocol on |
| "we have a guest" · "there is a vip with me" · "my friend ravi is here" | same, warmer when it is a named friend |
| "the district collector of kullu is visiting" · "mr sharma is here" | office or honourific is enough - no name needed |
| "who is the guest" · "is anyone here" | reports who is currently with you |
| "the guest has left" · "the visit is over" | stands down, back to normal |

The console shows a **visitor protocol** banner while it is active, and the read
route works with no API key at all: recognition, the greeting and the stand-down
are deterministic rules in `core/visitors.py`, and the same facts are injected
into both system prompts so a Gemini/Groq reply stays in the same register. Add an
office to the table in `core/visitors.py` and JARVIS knows how to address it.

---

## AI providers and automatic fallback

Providers are tried in order, and a failing one is parked in a cooldown so the
next request skips straight to a healthy one:

```
gemini → groq → openrouter → openai → <every other provider you have a key for>
       → pollinations (keyless) → offline
```

- a **rate limit** cools a provider for ~90 s (longer if it keeps failing)
- a **rejected key** cools it for ~15 min, so one bad key never stalls every request
- a **transient network error** is retried in place, then fails over
- every provider has its own **timeout and retry budget**, so one slow endpoint
  can never hold the whole chain hostage (the keyless endpoint gets one try and
  20 s; a normal provider gets the full `AI_REQUEST_TIMEOUT` and your retries)
- when everything online is down, the **offline engine** answers instead of erroring

### It does not die when the keys run out

Four separate things keep JARVIS answering, and each one is visible in the
console rather than being a claim:

1. **Any key is a fallback.** Set a key and that provider joins the chain even if
   `AI_PROVIDERS` never mentions it - the order in that one variable is only a
   hint about priority.
2. **Keyless safety net.** With no key at all, JARVIS still answers online through
   **Pollinations**, which needs no signup and sits *after* every provider you
   have credentials for. It is a shared public endpoint, so anything sent through
   it leaves the machine - set `POLLINATIONS_ENABLED=false` to forbid it.
3. **Local engines.** **Ollama** and **LM Studio** answer with no internet and no
   key. Switch one on and JARVIS works even when the router does not.
4. **Offline engine.** Time, dates, maths, unit conversions, system status,
   memory, GPIO and the visitor protocol never touch the network at all.

The **Ai core** panel draws all of this as a circuit: one node per brain in
fallback order, the node answering right now lit up, cooling nodes amber, and
any node that only needs a key showing a **get key** link straight to that
vendor's key page. *brains 3/9* in the header is that same number.

Supported out of the box: **Google Gemini**, **Groq**, **OpenRouter**, **OpenAI**,
**Together**, **Cerebras**, **Mistral**, **DeepSeek**, **SambaNova**,
**NVIDIA NIM**, **Hugging Face**, **xAI**, any other OpenAI-compatible endpoint,
**Ollama** / **LM Studio** for a fully local model, and the keyless
**Pollinations** endpoint:

```ini
OLLAMA_ENABLED=true          # local, no key, no internet
LMSTUDIO_ENABLED=true        # local, no key
POLLINATIONS_ENABLED=true    # keyless cloud fallback (default)
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

**No microphone? Nothing is broken.** Voice input is a bonus, never a dependency:

- every feature - chat, tools, memory, reminders, search, images, GPIO and the
  visitor protocol - is driven by **typed text** and works with no microphone, no
  sound card, no USB device and no speech engine installed at all;
- the console **disables** the *Mic* and *Live Talk* buttons and says why, instead
  of offering you a control that cannot work, and the composer keeps the focus;
- a missing device or a missing engine is reported **once**, with the exact command
  to install voice if you want it. It never retries in a loop and never floods the
  chat with errors;
- a Pi with no microphone of its own can still recognise audio a **phone's browser**
  sends to `/api/transcribe`, as long as an STT engine is installed;
- plug a USB microphone in later and voice resumes on the next start - no code
  change, no reinstall.

**With a microphone, or without - the same console.** Voice input is routed by what
is actually available, so whichever row describes your machine, the *Mic* and
*Live Talk* buttons do the right thing instead of failing:

| Your setup | What *Mic* / *Live Talk* do |
|---|---|
| Pi has a microphone and a speech engine | listen on the Pi's own microphone (`/api/listen`, `/api/live`) |
| Pi has no microphone, your phone or laptop does | record on that device and send the audio to the Pi's STT engine (`/api/transcribe`) |
| Pi has no microphone, your device has none either | both buttons are disabled and the chat explains why - type every message |
| No speech engine installed | the console names *that* problem and the install command, rather than blaming a missing microphone |

Typed chat, tools, memory, reminders, search, images, GPIO, the visitor protocol and
JARVIS's spoken replies work in **every** row of that table.

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

## Music and video (Spotify, YouTube, anything)

JARVIS drives whatever is playing on the machine, and he needs no account, no key
and no internet to do it:

> "pause the music" · "resume" · "skip this song" · "go back a track" ·
> "stop the music" · "turn it down" · "mute the music" · "what's playing"

Under the hood that is **MPRIS over D-Bus**, through `playerctl` - the same
interface your keyboard's media keys use. One interface, so it works with
everything at once:

| Playing where | JARVIS controls it |
|---|---|
| the Spotify desktop app | yes |
| the Spotify web player in Chromium | yes |
| a **YouTube** (or any) video in a browser tab | yes |
| VLC, mpv, Rhythmbox, Lollypop, `cvlc` | yes |

One small package is required:

```bash
sudo apt install playerctl
```

Without it JARVIS says exactly that instead of pretending, and every other
feature keeps working. `main.py --check` and the `/docs` page both report whether
it is installed.

**Playing something specific.** The YouTube and Spotify rules turn a name into the
real thing:

> "play lofi beats on spotify" · "play Bohemian Rhapsody" ·
> "play arijit singh on youtube" · "watch the video of lofi girl"

`youtube_play` finds the top result and opens the video itself, not a results
page. A browser may still want one click before a freshly opened page makes
sound, and JARVIS says so when that happens - after which "pause" and "next"
drive the tab through MPRIS like anything else.

**Liked Songs (optional).** Spotify's own API adds the two things MPRIS cannot
know: your **saved songs**, and starting a *specific* track, artist or playlist.

> "my liked songs" · "like this song" · "add this to my liked songs" · "unlike this"

Link it once, on the machine running JARVIS:

```bash
.venv/bin/python scripts/spotify_auth.py
```

It prints the exact Redirect URI to paste into your Spotify app, opens the
consent page, catches the callback locally, and tells you the three values to put
in `.env` (`SPOTIFY_CLIENT_ID`, `SPOTIFY_CLIENT_SECRET`, `SPOTIFY_REFRESH_TOKEN`).
On a Pi with no browser (SSH) add `--manual`: it prints the link and asks for the
address Spotify redirects you to. Add `--write` and it puts the refresh token
into `.env` for you. Re-run it any time to relink.

Two honest limits, both Spotify's rather than JARVIS's:

- **starting playback needs Spotify Premium**; listing and saving liked songs
  works on a free account
- playback control needs an **active device** - open Spotify on the Pi or your
  phone first. With none, JARVIS says so and offers to open the track in the
  browser instead.

**In the console.** The telemetry panel has a *Media* block: the real track and
artist read from the player, a disc that spins while it plays, and
previous / play-pause / next / quieter / louder buttons. They call the same code
as the voice commands, so tapping the phone and saying "pause the music" from
across the room are one action. Nothing is invented: with no player running the
block says so.

The whole MPRIS half is flagged `offline_safe`, so **"pause the music" works with
no API key, no model and no internet** - it is in the offline engine's capability
list.

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
| `JARVIS_CREATOR` | value of `JARVIS_OWNER` | who built JARVIS; "who made you" answers with this name |
| `JARVIS_LANGUAGE` | `auto` | language JARVIS answers in: `auto` (mirror the message), `en`, `hi` or `hinglish`. You can also just say "speak in hindi" in chat - write in Hindi / Hinglish and JARVIS answers in kind |
| `AI_PROVIDERS` | `gemini,groq,openrouter` | fallback order hint; every provider with a key joins the chain anyway |
| `GEMINI_API_KEY`, `GROQ_API_KEY`, `OPENROUTER_API_KEY`, `OPENAI_API_KEY`, `TOGETHER_API_KEY`, `CEREBRAS_API_KEY`, `MISTRAL_API_KEY`, `DEEPSEEK_API_KEY`, `SAMBANOVA_API_KEY`, `NVIDIA_API_KEY`, `HF_TOKEN`, `XAI_API_KEY` | – | provider keys; each one is another fallback |
| `OLLAMA_ENABLED`, `OLLAMA_MODEL` | `false` | fully local model, no key, no internet |
| `LMSTUDIO_ENABLED`, `LMSTUDIO_MODEL` | `false` | local LM Studio server (no key) |
| `POLLINATIONS_ENABLED` | `true` | keyless public fallback, used only after every provider above |
| `AI_TEMPERATURE`, `AI_MAX_TOKENS`, `AI_REQUEST_TIMEOUT`, `AI_MAX_RETRIES` | `0.4`, `900`, `45`, `2` | model behaviour |
| `JARVIS_HISTORY_TURNS` | `12` | conversation window sent to the model |
| `SEARCH_PROVIDER` | `duckduckgo` | `duckduckgo` / `tavily` / `brave` / `searxng` |
| `IMAGE_PROVIDER` | `pollinations` | `pollinations` or `openai` |
| `EMAIL_ENABLED`, `SMTP_*`, `EMAIL_AUTO_SEND` | `false`, …, `false` | SMTP sending |
| `TTS_ENABLED`, `TTS_ENGINE`, `TTS_VOICE_EN`, `TTS_VOICE_HI`, `TTS_RATE`, `TTS_SPEAK_LIMIT` | `true`, `edge`, … | speech output - `TTS_SPEAK_LIMIT` is how many characters of a long answer are spoken (240 by default, `0` reads all of it) |
| `STT_ENABLED`, `STT_ENGINE`, `STT_WAKE_WORD`, `VOSK_MODEL_PATH` | `false`, `google`, `jarvis` | speech input |
| `JARVIS_WEB_HOST`, `JARVIS_WEB_PORT`, `JARVIS_WEB_TOKEN` | `0.0.0.0`, `8765`, – | web interface (`PORT` from the environment wins, for containers) |
| `JARVIS_VOICE_OUTPUT` | `device` | where replies are spoken: `device`, `browser` or `off` |
| *(system)* `playerctl` | – | media control (play/pause/skip/volume) for Spotify, browsers and VLC. `sudo apt install playerctl` |
| `SPOTIFY_CLIENT_ID`, `SPOTIFY_CLIENT_SECRET`, `SPOTIFY_REFRESH_TOKEN` | – | optional Spotify link: liked songs + starting a specific track. See `scripts/spotify_auth.py` |
| `CONFIRM_DESTRUCTIVE`, `ALLOWED_APPS`, `ALLOWED_PATHS`, `TOOL_TIMEOUT` | `true`, … | safety limits |

---

## Testing

```bash
.venv/bin/python -m pytest tests -q          # 353 tests
.venv/bin/python -m pyflakes config core ai tools voice hardware gui server main.py   # clean
npx --yes -p typescript tsc -b --noEmit      # type-checks the browser client
```

The suite is hermetic: temporary directories, no API keys, no network. It covers
intent matching, expression safety, the file sandbox, the trash-based delete,
reminders, migration of the old `ChatLog.json`, provider failover and cooldowns,
plan validation, the confirmation flow, the GPIO tools and their spoken-name
resolution, the "never fake a result" guarantee, and the HTTP/WebSocket API.

The media layer is tested against a stand-in `playerctl` on the `PATH`, so the
tests assert the exact command line JARVIS builds and the exact wording it
reports - including the case where the player ignores the command. Spotify's API
is tested against canned HTTP responses: token refresh, liked songs, starting a
track, and each of Spotify's refusals (Premium, no active device, rate limit).

The voice chain has its own tests (`tests/test_voice_api.py`,
`tests/test_console_flow.py`) with stub speech engines, so they check the real
routes and the real speaker without a microphone: browser audio routing, the
mute controls, transcription, Live Talk's honest "no microphone" answer, and the
full request sequence a browser session performs.

`python main.py --check` is the manual equivalent for a real install - it is the
first thing to run on the Pi.

---

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| Blank page in the browser | The page served but could not reach the brain. Start it with `.venv/bin/python main.py --web` and reload; the UI shows an offline banner in that case. |
| `FastAPI is not installed` | If the message also names a second interpreter and `.venv`, JARVIS was started with the system Python - pick the venv with *Python: Select Interpreter* (`./.venv/bin/python`), or run `.venv/bin/python main.py`. Otherwise: `pip install -r requirements.txt`. |
| `No supported WebSocket library detected` | Live console updates need a WebSocket implementation. `.venv/bin/pip install websockets` (or `uvicorn[standard]`), then restart. Until then the console still works - it just falls back to polling, so state and voice arrive a few seconds late. |
| Assistant answers "I don't have an AI provider available" | No key is set and the keyless fallback is switched off. Add one key to `.env`, set `POLLINATIONS_ENABLED=true`, or start Ollama. `--check` names the exact variable each provider is missing. |
| `I'm running offline right now` | Every provider failed or is cooling down. Check the internet, then press *reset provider cooldowns* in the status panel. |
| Answers are slower or less private than expected | You are on the keyless fallback (`pollinations` lit in the *Ai core* panel). Add any provider key for a private, faster brain, or set `POLLINATIONS_ENABLED=false` to forbid the shared endpoint. |
| Voice silent | Run `--check`. Install `pygame` (playback) and `edge-tts`, or `espeak-ng` for offline. |
| `speech output : none` | No TTS engine is installed, so the console can only type. `sh scripts/setup-pi.sh --voice`, then **restart JARVIS** - the engine is chosen at start-up. Only the microphone needs PyAudio; speech output does not, and the browser plays the audio, so the Pi needs no sound card for it. `espeak-ng` is the offline voice, `edge-tts` the natural one. |
| Microphone not found | Install `pyaudio` and `portaudio19-dev`; check `arecord -l`. Try `STT_ENGINE=vosk` for offline. |
| `Failed building wheel for PyAudio` | PyAudio has to be compiled, and PortAudio's C headers are missing. `sudo apt install -y portaudio19-dev python3-dev build-essential`, then `.venv/bin/pip install PyAudio` - or re-run the setup with `--system`. Everything else installs regardless, and talking through your phone's microphone still works without it. |
| `Failed building wheel for lgpio` / `command 'swig' failed` | `lgpio` generates its C bindings with swig and links against lgpio's C library. `sudo apt-get install -y swig python3-dev build-essential liblgpio-dev`, then `.venv/bin/pip install lgpio` - or re-run the setup with `--system`. Until then `gpiozero` uses another backend, or the mock one. |
| Assistant talks then answers its own voice | It should not - make sure `STT_ENABLED=true` so listening pauses while speaking. |
| `email is not configured yet` | Set `EMAIL_ENABLED=true`, `SMTP_USER` and `SMTP_PASSWORD`. Gmail needs an **App Password**, not your normal password. |
| Email login rejected | Gmail/Outlook require an app password or OAuth; a normal password will be refused. |
| Image generation failed | Needs internet (Pollinations) or `OPENAI_API_KEY`. |
| `token rejected - reopen the page with the correct ?token=` | `JARVIS_WEB_TOKEN` is set, so every request must carry it. Find the value in `.env`, then open the console once as `http://<pi-ip>:8765/?token=<value>` - the tab remembers it. To run without a token on a home network, blank that line in `.env` and restart. |
| `media control needs playerctl` | `sudo apt install playerctl`, then reload. The rest of JARVIS is unaffected meanwhile. |
| "nothing is playing ... nothing to control" | Start a song or a video first - MPRIS can only control a player that is running. |
| "Spotify refused ... needs Premium" | Spotify's Web API will not start playback on a free account. Playing, pausing and skipping through MPRIS still work. |
| "Spotify has no active device" | Open Spotify on the Pi or your phone (Spotify Connect) and ask again, or let JARVIS open the track in the browser. |
| Spotify tools say "not linked" | Optional feature: run `scripts/spotify_auth.py` once, or ignore it - media control needs no account. |
| The panel says `gpio mock` | `gpiozero` is missing, or this is not a Pi. On a Pi: `pip install -r requirements-hardware.txt`. Development off-Pi is meant to run this way. |
| Hardware tools say "I don't have a device called …" | That id is not in `config/hardware.json`. `list my hardware devices` prints the ones that are. |
| Search returns nothing useful | Your IP may be blocked by DuckDuckGo. Add `TAVILY_API_KEY` or `BRAVE_API_KEY`. |
| Everything is slow | See the performance notes below; usually it is thermal throttling or an SD card. |
| `Unable to open X display` with `--gui` | No graphical session. Use the web interface instead. |
| Debugger stops on `SystemExit: 1` at `sys.exit(main())` | `main()` exited with a failure code, and the reason is printed in the terminal a few lines above that exception. The usual one is a busy port: `Port 8765 is already in use` means a JARVIS instance is already running - open it, or stop it with `sh scripts/cloud.sh --stop`, or start this one with `--port 8766`. |
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
- **Idle cost is small.** State is pushed over the WebSocket (the 20 s poll is
  only a fallback), the clock ticks once a second, and the now-playing read is
  cached for a few seconds - so an idle Pi does not fork `playerctl` on every
  poll. Ask it for the CPU temperature any time: `read the cpu temperature`.
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
│   ├── media.py             play/pause/skip/volume for anything playing (MPRIS)
│   ├── spotify.py           liked songs and playback control (optional link)
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
├── server/app.py            FastAPI: REST, WebSocket, voice + media, /docs
├── gui/
│   ├── web/                 the browser interface (plain HTML/CSS/JS)
│   └── desktop.py           optional Tkinter window
│
├── tests/                   353 hermetic tests
├── scripts/                 install.sh, run.sh, setup-pi.sh,
│                            spotify_auth.py (one-time Spotify link)
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
