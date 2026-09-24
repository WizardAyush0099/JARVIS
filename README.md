# JARVIS
JARVIS — COMPLETE REBUILD & INTELLIGENCE UPGRADE

You are working on my existing Raspberry Pi JARVIS project.

IMPORTANT:
Do NOT blindly create a new project from scratch.
First inspect and understand the ENTIRE existing repository/folder, including all Python files, GUI files, ".env" usage, JSON files, assets, functions, APIs, automation code, TTS/STT code, and existing "jugaad" implementations.

My goal is to transform this existing project into a genuinely useful, intelligent personal AI assistant called JARVIS.

The project must remain compatible with Raspberry Pi 4 and should be practical to run on Raspberry Pi hardware.

---

1. FIRST: AUDIT THE EXISTING PROJECT

Before modifying anything:

- Read every relevant source file.
- Understand the current architecture.
- Identify duplicate code.
- Find broken imports.
- Find deprecated libraries.
- Find hard-coded paths.
- Find API-key problems.
- Find blocking/infinite loops.
- Find GUI freezing problems.
- Find crashes and exception-handling problems.
- Find broken automation.
- Find unsafe shell/system commands.
- Find unnecessary resource-heavy processes.
- Find problems specific to Raspberry Pi.
- Understand the existing "FirstLayerDMM" / decision-making architecture.
- Understand the existing GUI.
- Understand "ChatLog.json".
- Understand existing automation functions.
- Understand current microphone/STT/TTS implementation.
- Understand all existing "jugaad" solutions.

Do not remove useful existing functionality simply because it is old.

Preserve working features while replacing weak implementations where necessary.

---

2. CORE IDENTITY

The assistant is:

Name: JARVIS
Owner/User: Ayush

JARVIS is my personal AI assistant.

It should naturally know that its primary user is Ayush.

Examples:

User:
"Who am I?"

JARVIS:
"You are Ayush."

User:
"Who is your owner?"

JARVIS:
"You're Ayush, my primary user."

Do not repeatedly say my name unnaturally in every response.

Use "Ayush" naturally when appropriate.

The assistant should have a consistent personality inspired by a futuristic personal AI assistant, while still being useful, accurate and conversational.

---

3. REAL AI BRAIN

Upgrade the existing chatbot system into a proper AI orchestration layer.

JARVIS should be able to:

- Understand natural language.
- Maintain conversation context.
- Understand follow-up questions.
- Remember relevant conversation context.
- Correct misunderstandings.
- Ask clarification questions when genuinely necessary.
- Explain things simply or deeply depending on the request.
- Reason through multi-step tasks.
- Summarize information.
- Rewrite text.
- Generate code.
- Debug code.
- Explain code.
- Help with school/study work.
- Perform calculations.
- Search for current information when required.
- Use tools instead of merely describing what it could do.
- Decide which tool/function is appropriate for a request.
- Combine multiple tools for one task.
- Recover from tool/API failures.

Do NOT make JARVIS a simple collection of keyword-based "if/elif" responses.

Build a proper:

USER INPUT
↓
INTENT / AI UNDERSTANDING
↓
PLANNER
↓
TOOL SELECTION
↓
TOOL EXECUTION
↓
RESULT VALIDATION
↓
AI RESPONSE
↓
VOICE + GUI

architecture.

---

4. MULTI-API / FREE API ARCHITECTURE

The system should prioritize APIs/services that are free or have useful free tiers.

Do NOT design the application around one API key.

Create an API provider abstraction.

For example:

AI Provider Manager

- Provider A
- Provider B
- Provider C
- Local/offline fallback where practical

If one provider reaches its limit, fails, times out, or becomes unavailable:

1. Detect the failure.
2. Automatically try another configured provider.
3. Continue without crashing.
4. Tell the user only when necessary.

API keys must NEVER be hard-coded.

Use ".env".

Provide a clean configuration system such as:

AI_PROVIDER=
PROVIDER_A_API_KEY=
PROVIDER_B_API_KEY=
SEARCH_API_KEY=
EMAIL configuration=
TTS configuration=

Do not expose keys in source code, logs, GitHub or the GUI.

Prefer services with generous free tiers / renewable quotas, but NEVER falsely assume that an API has an unlimited daily quota.

The code should gracefully handle:

- rate limits
- quota exhaustion
- invalid keys
- network failure
- timeout
- provider downtime

---

5. SMART TOOL SYSTEM

Create a modular tool system.

JARVIS should have tools/functions for tasks such as:

Computer/System

- Open applications
- Close applications
- Launch websites
- Search files
- Read files
- Create files
- Edit files
- Organize files
- Get system information
- CPU usage
- RAM usage
- Disk usage
- Temperature
- Network status
- Battery/power information where available
- Shutdown/restart with confirmation
- Execute safe system operations

Web

- Search the web
- Search current information
- Open webpages
- Extract useful information
- Summarize webpages
- Search YouTube
- Open YouTube
- Find relevant resources

Productivity

- Notes
- Reminders
- To-do items
- Timers
- Calculations
- Date/time
- Unit conversion
- Basic scheduling

Files

- Read TXT
- Read JSON
- Read PDF where practical
- Create TXT
- Create JSON
- Generate useful documents
- Search local project files

Coding

- Generate code
- Explain code
- Debug code
- Modify project files
- Analyze errors
- Create small scripts

IMPORTANT:
Before making destructive file changes, ask for confirmation unless the user has explicitly requested the exact change.

---

6. EMAIL SYSTEM

JARVIS should be capable of sending emails.

Create a proper email tool.

It should support:

- Recipient
- Subject
- Body
- Optional attachment
- Confirmation before sending

Example:

User:
"Send an email to Rahul saying the project is ready."

JARVIS should prepare the email and ask:

"Your email is ready. Should I send it?"

Only send after confirmation unless the user has explicitly configured trusted automatic sending.

Do not store email passwords directly in source code.

Use environment variables or an appropriate secure authentication method.

---

7. IMAGE GENERATION

Add an image-generation capability.

When the user asks:

"JARVIS, generate an image of a futuristic city."

JARVIS should:

1. Understand the request.
2. Use a configured image-generation API/service.
3. Generate the image.
4. Save it locally.
5. Display/open it through the GUI where practical.
6. Tell the user where it was saved.

The architecture should allow the image provider to be changed later.

Do not hard-code one provider into the entire application.

---

8. VOICE INPUT

Improve the existing microphone system.

The current system should be replaced/upgraded so JARVIS feels like an actual voice assistant.

Requirements:

- Microphone input
- Speech-to-text
- Noise/error handling
- Timeout handling
- Recognition failure recovery
- Clear listening state in GUI
- Optional wake-word support
- Push-to-talk fallback

If a completely offline STT solution is too heavy for Raspberry Pi 4, use a lightweight architecture and document the tradeoff.

Do not make the whole application crash when speech recognition fails.

---

9. JARVIS-LIKE VOICE OUTPUT

The current Google voice/TTS does not feel like JARVIS.

Replace or upgrade it with a more natural neural-style voice system that has a deep, calm, futuristic assistant feel.

The target is:

- Natural
- Clear
- Smooth
- Slightly deep
- Professional
- Calm
- Futuristic
- Similar in overall feel to the Hindi-dubbed JARVIS style

Do NOT copy or clone a real actor's voice.

Use a legitimate free-tier or open-source TTS solution where practical.

Create a TTS abstraction so the voice engine can be changed later.

JARVIS should support both:

- English
- Hindi/Hinglish where practical

The voice system should not block the GUI.

---

10. GUI UPGRADE

Do not unnecessarily throw away the existing GUI.

Upgrade it.

The GUI should clearly show:

- JARVIS identity
- Listening state
- Processing state
- Speaking state
- User message
- JARVIS response
- Tool activity/status
- Errors
- Connection/API status
- Microphone status

Add a proper chat interface.

Every JARVIS response should have a:

COPY

button.

The user must be able to copy JARVIS's answer to the clipboard easily.

Also support:

- scrolling conversation
- clear chat
- timestamps
- text input
- microphone button
- send button
- status indicator

The interface must work properly on Raspberry Pi.

Do not use unnecessarily heavy animations that make Raspberry Pi 4 slow.

---

11. MEMORY

Improve the existing "ChatLog.json" system.

Create proper short-term conversation memory.

JARVIS should understand:

User:
"My project is called X."

Later:

User:
"What was my project called?"

JARVIS should understand the context.

Do NOT blindly store every conversation forever.

Separate:

- Current conversation
- Useful persistent preferences/facts
- Temporary information

Provide a way to clear conversation history.

Do not store sensitive credentials.

---

12. PERSONAL ASSISTANT FEATURES

JARVIS should be able to help with everyday tasks.

Examples:

"JARVIS, what time is it?"

"Search today's news about Raspberry Pi."

"Open VS Code."

"Find my Python project."

"Create a file called notes.txt."

"Explain this error."

"Calculate 27 × 43."

"Search YouTube for Raspberry Pi projects."

"Write an email."

"Generate an image."

"Read this file."

"Summarize this PDF."

"What's using most of my RAM?"

"Open the browser."

"Remind me about this later."

The important part is that JARVIS should actually execute supported tasks rather than simply reply with instructions.

---

13. RASPBERRY PI 4 OPTIMIZATION

This project MUST remain practical on Raspberry Pi 4.

Optimize for:

- Low RAM usage
- Low CPU usage
- Fast startup
- Non-blocking GUI
- Background threads/processes where appropriate
- API timeouts
- Connection retries
- Lazy loading
- Lightweight libraries
- Avoiding unnecessary browser instances
- Avoiding memory leaks

Do not assume a powerful desktop GPU exists.

Anything requiring heavy computation should preferably use an API/cloud service or an appropriate lightweight alternative.

---

14. HARDWARE INTEGRATION

Keep the architecture ready for Raspberry Pi GPIO hardware.

The system should have a modular hardware layer so I can later connect:

- LEDs
- Buttons
- Sensors
- Servo motors
- Relays
- Camera
- Other GPIO devices

Do not mix GPIO logic throughout the AI code.

Use something like:

AI Layer
↓
Tool Layer
↓
Hardware Layer
↓
GPIO

This will make future hardware expansion easier.

---

15. EXISTING "JUGAAD" IMPROVEMENT

I already have some workaround/"jugaad" implementations in the old project.

Do NOT simply delete them.

For each workaround:

1. Understand what problem it was solving.
2. Determine whether it is still necessary.
3. Replace it with a clean implementation if possible.
4. Keep a fallback if useful.
5. Make the system robust instead of dependent on hacks.

The final system should feel engineered, not patched together.

---

16. ERROR HANDLING

JARVIS must never randomly crash because:

- API failed
- Internet disconnected
- microphone failed
- TTS failed
- invalid command
- website unavailable
- file doesn't exist
- permission denied
- API quota exhausted
- malformed response
- library unavailable

Use:

- try/except
- meaningful error messages
- logging
- retry logic
- timeouts
- provider fallback
- safe recovery

Do not hide all errors silently.

Create a useful log system.

---

17. SECURITY

Security is extremely important.

Never expose:

- API keys
- passwords
- email credentials
- tokens

Do not allow arbitrary destructive shell commands without confirmation.

Potentially dangerous actions should require confirmation.

Examples:

- Delete files
- Shutdown
- Restart
- Send emails
- Execute unknown scripts
- Modify important system files

Do not make JARVIS an unrestricted remote shell.

---

18. PROJECT STRUCTURE

Refactor the project into a clean modular architecture.

For example:

JARVIS/
│
├── main.py
├── config/
├── core/
│   ├── brain.py
│   ├── planner.py
│   ├── memory.py
│   └── router.py
│
├── ai/
│   ├── providers.py
│   └── fallback.py
│
├── tools/
│   ├── system.py
│   ├── web.py
│   ├── files.py
│   ├── email.py
│   ├── image.py
│   └── utilities.py
│
├── voice/
│   ├── stt.py
│   └── tts.py
│
├── hardware/
│   └── gpio.py
│
├── gui/
│   └── ...
│
├── data/
│   └── ...
│
├── assets/
│
├── logs/
│
├── .env
├── .env.example
├── requirements.txt
└── README.md

Adapt this structure to the existing project rather than forcing it if another architecture is better.

---

19. INSTALLATION

Create/update:

"requirements.txt"

and provide a clear setup process for Raspberry Pi 4.

The README must explain:

1. Python version
2. Required packages
3. Virtual environment
4. ".env" setup
5. API keys
6. Microphone setup
7. Speaker setup
8. Running JARVIS
9. Troubleshooting
10. Optional hardware setup

Avoid dependencies that are incompatible with Raspberry Pi.

---

20. OFFLINE FALLBACK

JARVIS should still provide basic functionality when the internet is unavailable.

At minimum:

- Time
- Basic calculations
- System information
- Local file operations
- Local commands
- Local memory
- GUI
- Basic voice handling if the chosen STT/TTS supports offline operation

Clearly separate online and offline capabilities.

---

21. DO NOT FAKE CAPABILITIES

Very important:

JARVIS must NEVER claim:

"I sent the email"

unless it actually sent it.

Never say:

"I generated the image"

if generation failed.

Never pretend that a web search was performed when it wasn't.

Never invent tool results.

The AI response must reflect the actual tool result.

---

22. TESTING

After modifications, test:

- Application startup
- GUI
- Text input
- Voice input
- TTS
- AI response
- Provider fallback
- Web search
- File operations
- Email workflow
- Image generation workflow
- Clipboard copy
- Memory
- System commands
- Raspberry Pi compatibility

Fix errors you discover.

Do not stop after generating code.

---

23. IMPORTANT DEVELOPMENT RULE

Do not blindly rewrite everything.

Follow this process:

AUDIT
→ PLAN
→ REFACTOR
→ IMPLEMENT
→ TEST
→ FIX
→ OPTIMIZE
→ DOCUMENT

Preserve useful existing work.

Remove obsolete code only after understanding its purpose.

---

24. FINAL GOAL

The final JARVIS should feel like a real personal AI assistant rather than a Python chatbot.

It should be:

- Intelligent
- Conversational
- Context-aware
- Tool-capable
- Modular
- Extensible
- Voice-controlled
- Raspberry-Pi compatible
- Resource-conscious
- Secure
- Able to use multiple API providers
- Able to recover from failures
- Able to execute useful tasks
- Able to work with my existing hardware
- Able to evolve with new tools later

The most important principle:

JARVIS should not just tell Ayush how to do something when it has the capability to actually do it.

Build the architecture so new abilities can be added later simply by creating/registering another tool.

Do not sacrifice stability just to add flashy features.

First make the existing system reliable, then make it powerful.
