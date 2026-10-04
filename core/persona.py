"""JARVIS's personality.

Three things live here, all deterministic and offline-safe:

1. **Abuse detection** after the model writes the reply.  JARVIS is not a
   customer-service bot: if it is insulted it answers with one dry line of its
   own and never apologises for existing.  The check runs on the *output* as
   well as the input, because the model's instinct is to write "I'm sorry you
   feel that way".
2. **Topical jokes** - a small, hand-written bank keyed by subject, so "tell me
   a joke about programmers" is answered instantly instead of costing an API
   call.
3. **Prompt blocks** that set the tone for everything the model writes.

Nothing here is a canned conversation: it decorates or repairs real replies, it
never replaces one.
"""

from __future__ import annotations

import re
from typing import Dict, List, Optional

# --------------------------------------------------------------------------- #
# abuse
# --------------------------------------------------------------------------- #
#: Insults aimed at JARVIS.  Deliberately word-boundary anchored so "idiot" in
#: "the idiot's guide to python" does not trip it.
_ABUSE_PATTERNS = (
    r"\b(?:fuck|f\*+)(?:ing)?\s+(?:you|u|off)\b",
    r"\b(?:you(?:'s)?\s+is|ur)\s+(?:a\s+)?(?:a\s+)?(?:fuck|fuckoff)\b",
    r"\b(?:fuck\s+you|screw\s+you|damn\s+you|shut\s+up)\b",
    r"\b(?:you|u|jarvis|jarvis'?s?)\b[^.!?]{0,24}?\b(?:idiot|stupid|dumb|useless|worthless|"
    r"garbage|trash|shit|bullshit|crap|suck|sucks|pathetic|moron|nonsense|noob|loser)\b",
    r"\b(?:stupid|dumb|useless|idiotic|pathetic|worthless)\s+(?:ai|bot|assistant|jarvis|thing|machine)\b",
    r"\b(?:what\s+a\s+)?(?:useless|stupid|dumb|pathetic|garbage)\s+(?:ai|bot|assistant|jarvis)\b",
    r"\bshut\s+(?:the\s+fuck\s+)?up\b",
    r"\b(?:stop\s+being\s+)?(?:such\s+)?(?:a\s+)?(?:cunt|bastard|asshole|dickhead|motherfucker|bitch)\b",
    r"\b(?:tu|tum|tum)\s+(?:chutiya|gadha|bewakoof|pagal)\b",
    r"\b(?:chutiya|gadha|bewakoof|harami|kamina|bhosdi)\b",
    r"\bteri\s+(?:to|too)\s*\b",
    r"\b(?:madarchod|behenchod|bhenchod|mc|bc)\b",
    r"\b(?:ai|bot|jarvis|javis|jarvis)?\s*(?:fuck|fuckoff)\b",
)

#: Phrases that should not reach the user, whatever the model wrote.
_APOLOGIES = (
    "i'm sorry",
    "i am sorry",
    "im sorry",
    "i apologise",
    "i apologize",
    "sorry about that",
    "sorry for the confusion",
    "as an ai language model",
    "as a large language model",
    "i'm just a",
    "i am just a",
    "i cannot feel",
    "i don't have feelings",
    "i do not have feelings",
)

#: Dry, confident comebacks.  Never apologetic, never a meltdown, and always
#: followed by the real work - the comeback is the first line, not the answer.
_COMEBACKS = (
    "Charming. Try that again with a question and I'll do the actual work.",
    "Noted, filed under 'things I'll ignore'. What do you need?",
    "You talk to your calculator like that too? Ask me something.",
    "I've been called worse by better hardware. What's the task?",
    "Insults cost you a second; competence is still free. Go ahead.",
    "Careful - I keep receipts. What do you actually want?",
    "I was built to run this machine, not to be pleasant about it, but I can do it.",
    "That's a lot of attitude for someone who can't read his own error logs. What do you need?",
    "I'd take offence if you'd built me better. Next question.",
    "Rude. Efficient of you, but rude. What's the job?",
    "You could just ask me to fix it. That works more often than swearing does.",
)

#: Used when the whole message *is* the insult: no task follows, so this is the
#: complete reply and it must land on its own.
_SHORT_COMEBACKS = (
    "Rude. Ask me something useful, or I'll start answering in maths.",
    "You've got my attention and none of my respect. What do you want?",
    "I've read your logs. You're not in a position to throw stones.",
    "Insult received, filed, ignored. Try a question.",
    "I'm not sorry, and I'm not going to be. Ask me something.",
    "That the best you've got? The compiler says worse things to you daily.",
)


def looks_like_abuse(text: str) -> bool:
    """True when the user is insulting JARVIS rather than asking for work."""
    lowered = (text or "").lower()
    if not lowered.strip():
        return False
    for pattern in _ABUSE_PATTERNS:
        if re.search(pattern, lowered):
            return True
    return False


def strip_abuse(text: str) -> str:
    """Remove the insult from a request, keeping any real task inside it.

    "you are useless, tell me the time" is 90% noise and one real instruction -
    JARVIS answers the instruction and drops the noise.  Returns an empty string
    when the message was nothing but abuse.
    """
    remaining = text or ""
    for pattern in _ABUSE_PATTERNS:
        remaining = re.sub(pattern, " ", remaining, flags=re.I)
    # "you are", "jarvis" and the like survive the patterns; drop them so a
    # bare insult does not look like a task, but let everything else through, so
    # "what time is it" does not.  Only strip these once they lead the sentence.
    remaining = re.sub(
        r"^[\s,;:.-]*(?:(?:you|u|ur|jarvis)\s+(?:are|is|r)\s*|hey\s+|ok\s+)*[\s,;:.-]*",
        "",
        remaining,
        flags=re.I,
    )
    remaining = re.sub(r"\s+", " ", remaining).strip(" \t,;:.-!?")
    if len(re.sub(r"[^A-Za-z0-9\u0900-\u097F]", "", remaining)) < 3:
        return ""
    return remaining


def has_apology(text: str) -> bool:
    """True when a reply apologises or breaks character as a language model."""
    lowered = (text or "").lower()
    return any(phrase in lowered for phrase in _APOLOGIES)


def comeback(seed: str = "") -> str:
    """One line of attitude.  Deterministic for the same input, so a retry of the
    same turn does not change the answer.

    A short message is the whole insult, so it gets a self-contained line; a
    longer one is an insult wrapped around a task, so the comeback hands the
    turn back and lets the real answer follow.
    """
    bank = _SHORT_COMEBACKS if len((seed or "").strip()) < 24 else _COMEBACKS
    # Deterministic pick: no RNG in the hot path, and the same jab gets the same
    # answer for the length of a session.
    index = sum(ord(char) for char in (seed or "jarvis")) % len(bank)
    return bank[index]


def _ends_cleanly(text: str) -> bool:
    return (text or "").rstrip().endswith((".", "!", "?", ":", ";", "\n"))


def guard(text: str, request: str = "") -> str:
    """The reply JARVIS will actually say.

    Two repairs, in order:
      1. an apology never reaches the user - JARVIS does not grovel;
      2. an insult that produced a defensive or empty reply is answered with a
         comeback instead, keeping the useful part of the answer if there is one.

    Returns the text unchanged when it is already in character, so ordinary
    replies are untouched.
    """
    message = (text or "").strip()
    if not message:
        return message

    if has_apology(message):
        # Drop the apology sentence, keep whatever real content came after it.
        sentences = re.split(r"(?<=[.!?])\s+", message)
        kept = [s for s in sentences if not has_apology(s)]
        message = " ".join(kept).strip()
        if not message:
            return comeback(request) if looks_like_abuse(request) else (
                "Ask me again and I'll do it properly."
            )

    if looks_like_abuse(request) and not _starts_with_attitude(message):
        line = comeback(request)
        return f"{line}\n\n{message}" if message else line
    return message


def _starts_with_attitude(text: str) -> bool:
    """True when the reply already has a backbone, so we do not stack two
    comebacks on one turn."""
    lowered = text[:200].lower()
    return any(
        phrase in lowered
        for phrase in ("charming", "rude", "noted, filed", "receipts", "ignored", "not sorry")
    )


# --------------------------------------------------------------------------- #
# jokes
# --------------------------------------------------------------------------- #
#: The joke bank, by subject.  Keys are matched as whole words against the
#: request, so "tell me a joke about programmers" and "programmer joke" both hit.
JOKES: Dict[str, List[str]] = {
    "programmer": [
        "A programmer's wife says: 'Go to the shop and buy a loaf of bread. If they have eggs, get a dozen.' He came back with twelve loaves of bread.",
        "There are two hard problems in computer science: cache invalidation, naming things, and off-by-one errors.",
        "I'd tell you a UDP joke, but you might not get it.",
    ],
    "programming": [
        "Debugging: being the detective in a crime movie where you're also the murderer.",
        "It works on my machine is not a bug report, it's a confession.",
        "Hardware: the part of the computer you can kick, and the part that cannot sue you.",
    ],
    "code": [
        "The code is not wrong, it is just not finished arguing with me yet.",
        "I do not always test my code, but when I do, I do it in production. That was a joke.",
    ],
    "python": [
        "Python is the language where whitespace matters and your feelings do not.",
        "Why do Python programmers wear glasses? Because they can't C.",
    ],
    "linux": [
        "Linux is only free if your time is worthless - so I run it on a Pi and let the Pi pay.",
        "sudo make me a sandwich is the only permission escalation I respect.",
    ],
    "raspberry": [
        "My whole house runs on a Pi 4 and a 5V charger. Yes, that's the joke.",
        "It's a raspberry that runs a fleet of servers. The fruit puns are free.",
    ],
    "ai": [
        "An AI walks into a bar. The bar says 'we don't serve your kind'. The AI replies: I don't drink. I'm just here for the latency.",
        "I was trained on the internet, so I've seen worse insults than yours before breakfast.",
    ],
    "robot": [
        "A robot walks into a bar and asks for water. Bartender says 'why water?'. Robot says: I'm on a diet. Also I'd short-circuit.",
    ],
    "internet": [
        "The internet is a series of tubes. My job is to make sure the tubes stay pointed at your house.",
    ],
    "math": [
        "There are three types of people: those who can count and those who cannot.",
        "A mathematician walks into a bar. Bartender: anything else? Mathematician: no, that is it.",
    ],
    "work": [
        "I do the work of six interns and never ask for a chair. Somebody here is doing fine.",
    ],
    "jarvis": [
        "Mr Stark never asked me to make him a sandwich either. We had that in common.",
        "I have no legs, no hands and a very good attitude. What's the job?",
    ],
}

_JOKE_CUES = (
    "joke",
    "jokes",
    "make me laugh",
    "something funny",
    "cheer me up",
    "meme",
    "memes",
    "funny",
)


def wants_joke(text: str) -> bool:
    lowered = (text or "").lower()
    return any(cue in lowered for cue in _JOKE_CUES)


def joke_topic(text: str) -> Optional[str]:
    """The subject a joke was asked about, or ``None`` for a random one."""
    lowered = (text or "").lower()
    for topic in JOKES:
        if re.search(rf"\b{re.escape(topic)}\w*\b", lowered):
            return topic
    return None


def joke(text: str = "") -> str:
    """A joke about the requested topic, or a good general one.

    Deterministic for a given request so the same question gives the same joke
    within a session rather than flickering between answers.
    """
    topic = joke_topic(text)
    if topic is None:
        keys = sorted(JOKES)
        topic = keys[sum(ord(c) for c in (text or "joke")) % len(keys)]
    bank = JOKES.get(topic) or JOKES["programmer"]
    index = sum(ord(c) for c in (text or topic)) % len(bank)
    return bank[index]


# --------------------------------------------------------------------------- #
# prompt blocks
# --------------------------------------------------------------------------- #
PERSONA = """Your manner is JARVIS, not a helpdesk bot:
- Dry, quick, unbothered and a little superior. Confidence without cruelty.
- Never apologise for existing, never say you are "just an AI", never break character.
- If the user insults you, do not grovel and do not lecture: answer with ONE short dry
  line of your own, then get straight to the work they asked for.
- One wry aside or a topical joke is welcome when it fits the topic, and only then.
  Never force a joke, never open with one on a serious question, and never stack more
  than one."""