"""Separate Nira's feedback on the previous answer from the next question.

Each AI turn used to be one string — "Excellent — that's strong experience with
audits. Now, how do you define delivery excellence?" — so the live UI spoke it
as a single run-on utterance and the report's question cards showed the
reaction glued to the question. The model now returns the two parts as separate
JSON fields (`feedback` / `message`) and `InterviewMessage.feedback` stores the
reaction, leaving `content` as the question alone.

`split_feedback_and_question` is the deterministic backstop for text that still
arrives combined: a model turn that ignored the format, the hardcoded fallback
replies, and every message saved before the split existed (the report still has
to show clean questions for those interviews). It is deliberately conservative
— it only splits off a LEADING sentence that reads as an acknowledgment, so a
question that merely contains praise, or context that frames the question, is
left intact.
"""

import re

# A sentence counts as feedback only if it OPENS with an acknowledgment. Bare
# ack words must be followed by punctuation ("Great," / "Perfect!") so that a
# question such as "Great leaders inspire people. How do you…?" is not split.
_BARE_ACK = (
    r"(?:great|perfect|excellent|wonderful|fantastic|awesome|brilliant|good|nice|"
    r"lovely|impressive|interesting|understood|noted|okay|ok|alright|all right|"
    r"got it|i see|fair enough|no worries|absolutely|certainly|indeed|wow|sure|"
    r"right|cool|terrific|superb|outstanding|amazing|makes sense|appreciate it|"
    r"appreciated|very good|very well|well said|well done|nicely done|nicely put|"
    r"good stuff|great stuff|love it|love that|fair point|good point|great point|"
    r"valid point|i understand|thanks|thank you|thanks a lot|thank you so much)"
)
_PRAISE_WORDS = (
    r"great|perfect|excellent|good|nice|wonderful|fantastic|awesome|amazing|"
    r"solid|strong|interesting|helpful|clear|thoughtful|valuable|insightful|"
    r"detailed|comprehensive|practical|concrete|relevant|impressive|rich|deep|"
    r"extensive|thorough|useful|remarkable"
)
_PRAISE_ADJ = r"(?:" + _PRAISE_WORDS + r")"
_ACK_OPENER = re.compile(
    r"^(?:"
    + _BARE_ACK + r"\s*(?:[,.!:;]|[—–-]\s|$)"
    r"|(?:(?:really|very|quite|such\s+a|a)\s+)?" + _PRAISE_ADJ + r"\s+"
    r"(?:answers?|points?|examples?|explanations?|insights?|overview|responses?|"
    r"perspectives?|context|one|stuff|work|details?|approach|breakdown|summary|"
    r"walkthrough|story|experience|background|journey|foundation|understanding|"
    r"grasp|knowledge|to hear|share|sharing)\b"
    r"|thanks?\b|thank you\b|i appreciate\b|appreciate\b"
    r"|(?:that|this|it)(?:'?s|\s+is|\s+was)\s+(?:a\s+|an\s+|really\s+|very\s+|some\s+|"
    r"quite\s+|such\s+a\s+|definitely\s+)*(?:" + _PRAISE_WORDS + r"|fair|well)\b"
    r"|(?:really|very|quite)\s+" + _PRAISE_ADJ + r"\b"
    r"|what\s+a\s+" + _PRAISE_ADJ + r"\b"
    r"|(?:good|nice|great)\s+to\s+(?:hear|know)\b"
    r"|(?:i\s+)?(?:love|like)\s+(?:that|this|how|the\s+way)\b"
    r"|(?:that|this|it)\s+(?:makes|made)\s+(?:total\s+|a\s+lot\s+of\s+)?sense\b"
    r"|you'?ve\s+(?:clearly|certainly|really|definitely|"
    r"got\s+(?:some\s+|a\s+|very\s+|really\s+)?" + _PRAISE_ADJ + r")\b"
    r"|you\s+(?:clearly|certainly|really|definitely)\s+(?:have|know|understand|bring)\b"
    # Remarks on how the candidate is doing ("I see you're skipping questions.")
    r"|i\s+(?:see|notice|understand)\s+(?:that\s+)?you(?:'re|\s+are|'ve|\s+have)?\b"
    r"|no\s+problem\b|that'?s\s+(?:okay|ok|alright|fine|completely\s+fine)\b"
    r"|sounds\s+(?:great|good|like)\b|glad\b"
    r")",
    re.IGNORECASE,
)

# An acknowledgment glued onto the question in ONE sentence: "Thanks for
# sharing that — how do you measure SLAs?" / "Great, tell me about …".
_QUESTION_START = (
    r"(?:how|what|why|when|where|which|who|whom|can|could|would|will|do|did|does|"
    r"have|has|had|are|is|was|were|should|tell|walk|talk|describe|explain|share|give)\b"
)
_INLINE_ACK = re.compile(
    r"^(?P<ack>(?:" + _BARE_ACK + r"|(?:thanks|thank you)\s+(?:so\s+much\s+)?for\s+[^,—–?]{1,80}?)"
    r"(?:,?\s+\w+)?)"  # optional name: "Great, Raja, how …"
    r"\s*(?:,|[—–-])\s*"
    r"(?:(?:so|and|now|okay|alright)\s*,?\s+)?"
    r"(?P<question>" + _QUESTION_START + r".*)$",
    re.IGNORECASE | re.DOTALL,
)

# A sentence that starts with one of these is where the question begins.
_TRANSITION = re.compile(
    r"^(?:(?:so|and|okay|ok|alright|all right)\s*,?\s+)*"
    r"(?:now\b|next\b|moving on\b|let(?:'s|\s+me|\s+us)\b|here'?s\b|my next\b|"
    r"the next\b|building on\b|on that note\b|shifting\b|switching\b|turning to\b|"
    r"for (?:the|your|my) (?:next|final|last)\b|one (?:final|last|more)\b|"
    r"this is (?:your|the|our) (?:last|final)\b|to wrap up\b|finally\b)",
    re.IGNORECASE,
)

# Verbal lead-ins that carry no content of their own ("Now, …", "So let me
# start with our first question: …"). Every alternative needs trailing
# punctuation so "Let's talk about Kubernetes" or "Next-gen …" survive.
_LEAD_IN = re.compile(
    r"^(?:"
    r"(?:so|and|now|okay|ok|alright|all right|next|moving on|finally)\s*[,:—–-]\s*"
    r"|(?:so|and|now)\s+(?=let|here|this|one|my|the|for)"
    r"|let(?:'s|\s+me|\s+us)\s+(?:start|begin|kick off|move on|continue|get started|"
    r"dive in|go ahead|proceed)(?:\s+(?:with|to)\s+(?:our|the|my|your)\s+"
    r"(?:first|next|final|last)\s+(?:question|one))?\s*[.:!,—–-]+\s*"
    r"|here'?s\s+(?:the|my|your)\s+(?:first|next|final|last)\s+(?:question|one)(?:\s+for\s+you)?\s*[.:!,—–-]+\s*"
    # "Let's dive into a technical scenario." / "Let me jump into our first
    # question." — generic scene-setting with no content of its own.
    r"|let(?:'s|\s+me|\s+us)\s+(?:dive|jump|get|move|start|begin|kick\s+off|go|turn|head|try)"
    r"(?:\s+(?:right\s+)?(?:into|with|on|to|over\s+to))?"
    r"(?:\s+(?:our|the|a|an|some|my|your|this|one|another))?"
    r"(?:\s+(?:first|next|few|practical|technical|real-world|quick|simple|simpler|"
    r"different|more|direct|straightforward|final|last))*"
    r"\s+(?:questions?|scenarios?|ones?|topics?|areas?|problems?|examples?)"
    r"(?:\s+for\s+you)?\s*[.:!—–-]+\s*"
    # "Let me ask this differently." / "Let me ask something simpler:"
    r"|let\s+me\s+(?:ask|put|phrase|try)\s+(?:this|it|that|something)(?:\s+[\w-]+){0,3}\s*[.:!—–-]+\s*"
    r"|let(?:'s|\s+us|\s+me)\s+(?:shift|switch|change)\s+(?:gears|topics?|direction)\s*[.:!—–-]+\s*"
    r"|here'?s\s+another(?:\s+(?:question|one))?\s*[.:!,—–-]+\s*"
    r"|let\s+me\s+ask(?:\s+you)?(?:\s+(?:this|something))?\s*[.:!,—–-]+\s*"
    # "Let me try one more direct question:" / "Let me ask a more direct one."
    r"|let\s+(?:me|us)\s+(?:try|ask)(?:\s+you)?(?:\s+[\w-]+){0,4}?\s+(?:question|one)\s*[.:!,—–-]+\s*"
    r"|(?:the|my)\s+next\s+question\s+is\s*[.:!,—–-]*\s*"
    r"|next\s+question\s*[.:!,—–-]+\s*"
    r")+",
    re.IGNORECASE,
)

_SENTENCE_BREAK = re.compile(r"(?<=[.!?])\s+")

# Questions phrased as instructions carry no "?" ("Tell me about a time …").
_IMPERATIVE_QUESTION = re.compile(
    r"^(?:(?:so|and|now|okay|alright)\s*,?\s+)*(?:please\s+)?"
    r"(?:tell|walk|talk|describe|explain|share|give|take|help|outline|list)\b",
    re.IGNORECASE,
)

# "Alright, this is your last question." — spoken with the feedback, before the
# pause, so the candidate still gets the heads-up.
_ANNOUNCEMENT = re.compile(r"\b(?:last|final)\s+question\b", re.IGNORECASE)


def _asks_sentence(sentence: str) -> bool:
    return "?" in sentence or bool(_IMPERATIVE_QUESTION.match(sentence.strip()))


def asks_question(text: str) -> bool:
    """True when the text asks something — a "?" or an instruction-style
    question ("Tell me about …"). Feedback must never do this."""
    return any(_asks_sentence(s) for s in _SENTENCE_BREAK.split(text or ""))


def _split_inline_ack(sentence: str):
    m = _INLINE_ACK.match(sentence.strip())
    if not m:
        return None
    ack = m.group("ack").strip()
    question = m.group("question").strip()
    if len(question) < 10:
        return None
    if ack[-1] not in ".!":
        ack += "."
    return ack[0].upper() + ack[1:], question[0].upper() + question[1:]


def _cut_lead_in(question: str) -> tuple[str, str]:
    """``(lead_in, question)`` with content-free lead-ins removed from the front."""
    m = _LEAD_IN.match(question)
    stripped = question[m.end():].strip() if m else question
    # Never strip the question down to nothing / a fragment.
    if not m or len(stripped) < 10:
        return "", question
    return question[: m.end()].strip(), stripped[0].upper() + stripped[1:]


def _spoken_lead_in(lead_in: str) -> str:
    """A lead-in that is a whole sentence ("Let's shift gears.") is kept as part
    of the spoken feedback; a fragment ("Now,", "Here's my first question:")
    means nothing on its own and is dropped."""
    sentences = [s.strip() for s in _SENTENCE_BREAK.split(lead_in) if s.strip()]
    return " ".join(s for s in sentences if s[-1] in ".!")


def split_feedback_and_question(text: str) -> tuple[str, str]:
    """Return ``(feedback, question)``. ``feedback`` is "" when the text does
    not open with an acknowledgment, in which case ``question`` is the text
    unchanged."""
    t = (text or "").strip()
    sentences = [s.strip() for s in _SENTENCE_BREAK.split(t) if s.strip()]
    if not sentences:
        return "", t
    first = sentences[0]
    if first.endswith("?") or len(sentences) == 1 or not _ACK_OPENER.match(first):
        # The acknowledgment may still be glued onto the question inside the
        # first sentence: "Thanks for sharing — how do you …?".
        inline = _split_leading_inline_ack("", sentences)
        if inline:
            return inline
        # No acknowledgment, but maybe a content-free lead-in ("Let's shift
        # gears. You're working on …?").
        lead_in, question = _cut_lead_in(t)
        if lead_in and asks_question(question):
            return _spoken_lead_in(lead_in), question
        return "", t

    # Everything before the first transition sentence ("Now, …") is feedback —
    # that captures multi-sentence reactions like "Perfect, thanks. You've got
    # solid ITIL experience. Now, have you…?". Without a transition only the
    # opening sentence goes, so framing context ("You mentioned KPMG. How
    # did…?") stays with the question.
    cut = None
    for i in range(1, len(sentences)):
        if _TRANSITION.match(sentences[i]):
            cut = i
            break
        if _asks_sentence(sentences[i]):
            break
    if cut is None:
        if not any(_asks_sentence(s) for s in sentences[1:]):
            # No transition and no question — a closing line or a statement
            # ("Great, here is your next coding challenge. …"). Leave it whole.
            return "", t
        cut = 1
    while (
        cut < len(sentences) - 1
        and _ANNOUNCEMENT.search(sentences[cut])
        and not _asks_sentence(sentences[cut])
    ):
        cut += 1

    feedback = " ".join(sentences[:cut])
    rest = sentences[cut:]
    return _split_leading_inline_ack(feedback, rest) or _finish(feedback, " ".join(rest))


def _finish(feedback: str, question: str) -> tuple[str, str]:
    """Drop lead-ins from the question; whole-sentence ones stay spoken."""
    lead_in, question = _cut_lead_in(question)
    spoken = _spoken_lead_in(lead_in) if lead_in else ""
    return " ".join(p for p in (feedback, spoken) if p), question


def _split_leading_inline_ack(feedback: str, rest: list[str]):
    """If ``rest`` opens with "Ack — question…", move the ack onto feedback."""
    inline = _split_inline_ack(rest[0])
    if not inline:
        return None
    ack, question = inline
    return _finish(" ".join(p for p in (feedback, ack) if p), " ".join([question] + rest[1:]))


def feedback_and_question(message) -> tuple[str, str]:
    """``(feedback, question)`` for a stored AI message.

    Messages that carry a stored ``feedback`` were split at write time, so
    their ``content`` is already just the question. Older rows (and turns the
    model returned without feedback) go through the splitter.
    """
    content = (getattr(message, "content", "") or "").strip()
    feedback = (getattr(message, "feedback", None) or "").strip()
    if feedback:
        return feedback, content
    return split_feedback_and_question(content)


def question_text(message) -> str:
    """The question alone, for report / evaluation views of an AI message."""
    return feedback_and_question(message)[1]
