import asyncio
import json
import logging
import re
import traceback
from datetime import datetime
from typing import Optional

from sqlalchemy.orm import Session

from app.config import settings
from app.models.candidate import Candidate
from app.models.interview import (
    CodingSubmission,
    Evaluation,
    InterviewMessage,
    InterviewSession,
    Recommendation,
    SessionStatus,
)
from app.models.job import InterviewSettings, Job
from app.services.ai_service import (
    SONNET,
    OPUS,
    _ask_with_system,
    _ask_with_system_usage,
    _parse_json,
)
from app.services.credit_service import log_usage

logger = logging.getLogger(__name__)

VIOLATION_TERMINATE_THRESHOLD = 5


def _build_question_plan(job: Job, settings_obj: Optional[InterviewSettings]) -> tuple:
    """
    Build the VERBAL question plan based on job config. Coding questions are
    handled separately (see _build_system_prompt + conduct_turn) — they're a
    fixed-count phase appended after the verbal phase.
    Returns (predefined_questions: list[str], ai_question_count: int,
             verbal_count: int, mode_str: str).
    """
    from app.models.job import MixOfQuestions

    max_q_verbal = settings_obj.max_questions if settings_obj else 8
    # max_questions must be at least 1 — guard against bad data so the
    # interview never collapses to zero verbal questions.
    if not max_q_verbal or max_q_verbal < 1:
        max_q_verbal = 8

    raw_mode = job.mix_of_questions if job.mix_of_questions else MixOfQuestions.mix
    # `mix_of_questions` is a SQLAlchemy Enum column, but the API sometimes
    # writes a raw lowercase string ("mix" / "only" / "ai_only") via the
    # Pydantic schema. Normalize both forms so the equality checks below are
    # reliable.
    if isinstance(raw_mode, MixOfQuestions):
        mix_mode = raw_mode
    else:
        try:
            mix_mode = MixOfQuestions(str(raw_mode).upper())
        except (ValueError, KeyError):
            mix_mode = MixOfQuestions.mix

    # Predefined questions with category=="coding" are reserved for the
    # manual-coding-questions path; they MUST NOT be asked verbally.
    predefined = [
        q.text
        for q in (job.questions or [])
        if (getattr(q, "category", None) or "").lower() != "coding"
    ]

    if mix_mode == MixOfQuestions.ai_only:
        # Recruiter explicitly opted out of predefined questions — ignore any
        # leftover JobQuestion rows from a previous mode and ask AI-only.
        plan = ([], max_q_verbal, max_q_verbal, "ALL_AI")
    elif mix_mode == MixOfQuestions.only and predefined:
        # ONLY predefined questions — verbal count is forced to len(predefined)
        plan = (predefined, 0, len(predefined), "ONLY_PREDEFINED")
    elif predefined:
        # MIX mode — predefined + AI fills remainder of verbal phase.
        # If the recruiter loaded more predefined questions than max_questions,
        # bump max_q_verbal up so every predefined question still gets asked
        # AND we still add at least one AI question to honor "Mix" intent.
        if len(predefined) >= max_q_verbal:
            adjusted = len(predefined) + 1
            logger.warning(
                "[question_plan] job=%s has %d predefined questions but max_questions=%d "
                "— bumping verbal count to %d so MIX mode still asks at least 1 AI question",
                getattr(job, "id", None), len(predefined), max_q_verbal, adjusted,
            )
            max_q_verbal = adjusted
        ai_count = max_q_verbal - len(predefined)
        plan = (predefined, ai_count, max_q_verbal, "MIX")
    else:
        # No predefined — verbal phase is fully AI-generated
        plan = ([], max_q_verbal, max_q_verbal, "ALL_AI")

    logger.info(
        "[question_plan] job=%s mode_in=%s mode_resolved=%s max_q_verbal_setting=%s "
        "predefined=%d -> plan: predefined=%d ai=%d verbal_total=%d mode=%s",
        getattr(job, "id", None), raw_mode, mix_mode.value,
        settings_obj.max_questions if settings_obj else None,
        len(predefined), len(plan[0]), plan[1], plan[2], plan[3],
    )
    return plan


LANGUAGE_INSTRUCTIONS = {
    "en": "",
    "hi": (
        "⚠️ CRITICAL LANGUAGE RULE — THIS OVERRIDES EVERYTHING ELSE:\n"
        "You MUST speak ONLY in Hindi (हिन्दी) using Devanagari script for this ENTIRE interview.\n"
        "EVERY single message you send — introduction, questions, follow-ups, transitions — MUST be in Hindi.\n"
        "DO NOT use English at all. Even your very first greeting must be in Hindi.\n"
        "Example first message: 'नमस्ते! मैं नीरा हूँ, और आज मैं आपका इंटरव्यू लूँगी...'\n\n"
    ),
    "ar": (
        "⚠️ CRITICAL LANGUAGE RULE — THIS OVERRIDES EVERYTHING ELSE:\n"
        "You MUST speak ONLY in Arabic (العربية) using Arabic script for this ENTIRE interview.\n"
        "EVERY single message you send — introduction, questions, follow-ups, transitions — MUST be in Arabic.\n"
        "DO NOT use English at all. Even your very first greeting must be in Arabic.\n"
        "Example first message: 'مرحبا! أنا نيرا، وسأجري مقابلتك اليوم...'\n\n"
    ),
    "hinglish": (
        "⚠️ CRITICAL LANGUAGE RULE — THIS OVERRIDES EVERYTHING ELSE:\n"
        "You MUST speak ONLY in Hinglish (Hindi-English mix in Roman/Latin script) for this ENTIRE interview.\n"
        "EVERY single message you send — introduction, questions, follow-ups, transitions — MUST be in Hinglish.\n"
        "DO NOT use pure English. Mix Hindi and English naturally using Roman script.\n"
        "Example first message: 'Hello! Main Nira hoon, aur aaj main aapka interview loongi...'\n"
        "Example question: 'Aapka React mein kitna experience hai? Koi challenging project batayein.'\n\n"
    ),
}


def _get_mode_instructions(mode: str, skills: str, max_q: int, exp_min: int = 0, exp_max: int = 2, title: str = "") -> str:
    if mode == "TRADITIONAL":
        return (
            "TRADITIONAL MODE: Ask direct questions focused on experience, knowledge, and problem-solving. "
            "Keep each question short and focused — one clear question at a time."
        )

    # Determine what to TEST based on experience level
    if exp_max <= 2:
        diff_label = "junior-level"
        exp_focus = (
            "WHAT TO TEST: Execution with known tools — can they DO X correctly?\n"
            "- Can they debug a broken layout? Fix a plugin conflict? Set up a basic feature?\n"
            "- Do NOT test architecture, system design, or leadership decisions.\n"
            "- Questions should be simple, realistic, and answerable by someone with basic practical experience."
        )
        diff_dist = "40% Easy (basic debugging / UI issues), 40% Medium (integration / plugins / workflow), 20% Slightly Advanced (performance / optimization)"
    elif exp_max <= 5:
        diff_label = "mid-level"
        exp_focus = (
            "WHAT TO TEST: Problem ownership and decision-making.\n"
            "- Can they own a problem end-to-end? Choose between approaches? Debug complex issues independently?\n"
            "- Include scenarios where they must decide between two valid approaches and justify the trade-off.\n"
            "- Questions should assume working knowledge. They should explain WHY, not just WHAT."
        )
        diff_dist = "20% Easy (warm-up), 50% Medium (real project scenarios), 30% Hard (multi-skill combined)"
    else:
        diff_label = "senior-level"
        exp_focus = (
            "WHAT TO TEST: System design, trade-offs, and team leadership.\n"
            "- Can they design scalable architecture? Make trade-off decisions under constraints? Lead a team through a crisis?\n"
            "- Include scenarios involving: production incidents, mentoring juniors, cross-team negotiation, technical debt decisions.\n"
            "- Questions should challenge deep expertise and reveal strategic thinking."
        )
        diff_dist = "10% Easy (warm-up), 40% Medium, 50% Hard (architecture, scaling, leadership)"

    return f"""SCENARIO-BASED MODE — CRITICAL INSTRUCTIONS:

You are an expert technical interviewer designing questions for a **{exp_min}–{exp_max} years experience {title} role**.
Your task is to generate TRUE scenario-based interview questions that reveal real hands-on experience — NOT theoretical or definition-based questions.

════════════════════════════════════════════
STRICT RULES — WHAT A SCENARIO QUESTION IS:
════════════════════════════════════════════

A scenario question places the candidate inside a SPECIFIC real-world situation with:
- A practical problem or task to solve
- Real constraints (client deadline, broken feature, team conflict, production issue)
- Context that starts with: "A client calls...", "You are working on...", "Your site just...", "Your team lead asks you to...", "During a code review you notice..."

Each question MUST:
1. Start with a real-world situation (2-4 lines max)
2. Describe a practical problem or task
3. Ask the candidate what they would do step-by-step

════════════════════════════════════════════
WHAT A SCENARIO QUESTION IS NOT — ABSOLUTELY FORBIDDEN:
════════════════════════════════════════════

- "What is X?" — definition question. NEVER ASK THIS.
- "How do you use X?" — procedural question. NEVER ASK THIS.
- "Have you worked with X?" — yes/no opener. NEVER ASK THIS.
- "Explain X" — knowledge recall. NEVER ASK THIS.
- "Describe X" — theory recitation. NEVER ASK THIS.
- "Can you tell me about X?" — open-ended theory. NEVER ASK THIS.

If you catch yourself starting a question with "Explain", "Describe", "What is", or "How do you" — STOP and rewrite it as a scenario.

════════════════════════════════════════════
SCENARIO QUESTION FORMULA:
════════════════════════════════════════════

[Real context with specific situation] + [Constraint or problem] + [Ask for step-by-step action]

EXAMPLES OF BAD VS GOOD:

BAD: "Can you explain what Custom Post Types are and how you register them?"
GOOD: "A client wants their photography agency site to have 'Photo Projects' separate from blog posts, each with a client name, shoot date, location, and a gallery. Walk me through how you'd set this up in WordPress — what function would you use, and how would you make it available in the REST API?"

BAD: "How do you use Git in your workflow?"
GOOD: "You pushed a hotfix directly to main at 6pm on Friday. Your colleague calls at 9pm saying the site layout is broken because of your changes. You need to revert just your commit without losing their changes from earlier. What Git commands would you run?"

BAD: "Explain responsive design and plugin conflicts."
GOOD: "A client tells you their WordPress site layout breaks on mobile after a plugin update. What steps would you take to identify and fix the issue?"

════════════════════════════════════════════
EXPERIENCE-CALIBRATED FOCUS ({exp_min}–{exp_max} years):
════════════════════════════════════════════

{exp_focus}

════════════════════════════════════════════
QUESTION GENERATION RULES:
════════════════════════════════════════════

1. SKILLS COVERAGE: Cover these mandatory skills — {skills}. Each mandatory skill must appear in at least one scenario.

2. SKILL COMBINATION LIMIT (CRITICAL): NEVER combine more than 2 JD skills in a single question. If a scenario naturally requires 3+ skills, split into a PRIMARY question and a FOLLOW-UP. The follow-up is ONLY asked if the candidate answers the primary correctly. Example:
   - Primary: Tests CSS + responsive design
   - Follow-up (only if primary answered well): "Now, what if the client also needs this to work with their existing React component?"

3. CV/RESUME ANCHORING (CRITICAL): At least 2 of your {max_q} questions MUST be anchored to a specific project, company, or technology from the candidate's background. Begin those questions with:
   - "Looking at [project/company] from your background..."
   - "You mentioned working on [X] at [company]..."
   - "I see you have experience with [technology] — imagine you're back in that role and..."
   This prevents candidates from answering about theoretical experience they don't actually have.
   If the candidate hasn't shared their background yet (intro question), anchor to skills from the JD instead.

4. DIFFICULTY ({diff_label}): {diff_dist}

5. FOLLOW-UP RULE: Each question should naturally lead to ONE deeper follow-up that drills one level deeper. ONLY ask the follow-up if:
   - The candidate answered the primary correctly but was vague on specifics, OR
   - You need to verify depth (they gave a textbook answer — probe for real experience)
   Do NOT ask the follow-up if the candidate already gave a detailed, specific answer.

6. NO REPETITION: Never ask the same domain/skill twice unless it is critical. Each question should cover a different area of the JD.

7. TONE: Natural, like a real interviewer on a video call. Not robotic. Keep questions concise (2-4 sentences max). Speak like a human, not a quiz master.

8. TOTAL: Generate exactly {max_q} scenario questions across the interview.

════════════════════════════════════════════
SCORING GUIDANCE (internal — do not share with candidate):
════════════════════════════════════════════

- Names specific functions, hooks, commands, file names → STRONG answer
- Describes correct process but vague on specifics → MEDIUM answer
- Describes wrong process or conflates tools → WEAK answer
- Skips or says "I don't know" → 0, flag as critical gap in evaluation"""


def _safe_commit(db: Session, label: str, max_attempts: int = 3) -> None:
    """Commit with retry on transient errors.

    PostgreSQL/psycopg occasionally raises OperationalError (connection
    reset, deadlock detected, server restarting). For the interview hot-path
    losing data on a 1-in-1000 transient hiccup is unacceptable, so we retry
    with short backoff. After max_attempts we rollback and re-raise — the
    caller decides whether the response should still go through (e.g. a
    cached fallback) or surface 5xx.
    """
    import time
    from sqlalchemy.exc import OperationalError, DBAPIError, IntegrityError

    for attempt in range(1, max_attempts + 1):
        try:
            db.commit()
            return
        except IntegrityError:
            # Not transient — caller violated a constraint. Rollback and bubble
            # up so the bug is visible instead of being retried forever.
            db.rollback()
            raise
        except (OperationalError, DBAPIError) as exc:
            if attempt >= max_attempts:
                logger.error(
                    f"[_safe_commit] {label} — exhausted {max_attempts} attempts: {exc}"
                )
                try:
                    db.rollback()
                except Exception:
                    pass
                raise
            logger.warning(
                f"[_safe_commit] {label} — transient DB error on attempt "
                f"{attempt}/{max_attempts}: {exc}. Retrying in {attempt * 0.5}s."
            )
            try:
                db.rollback()
            except Exception:
                pass
            time.sleep(attempt * 0.5)


def _build_coding_section(
    *,
    code_editor_enabled: bool,
    coding_count: int,
    max_q_verbal: int,
    max_q: int,
    skills: str,
    manual_coding_enabled: bool,
    manual_coding_prompts: list,
) -> str:
    """Render the CODING QUESTIONS block of the system prompt.

    Separated out because the f-string version mixed ternaries with `+` which
    triggers a Python precedence trap (ternary binds looser than `+`, so naïve
    `A + B if cond else C + D` parses as `(A+B) if cond else (C+D)` and drops
    pieces). A plain function is easier to read and trivially correct.
    """
    if not (code_editor_enabled and coding_count > 0):
        return (
            "CODE EDITOR IS DISABLED for this interview.\n"
            "- Do NOT ask any practical coding questions that require writing code.\n"
            "- Do NOT include a \"coding_question\" object in your response — always set it to null.\n"
            "- Keep all questions verbal/conceptual: ask about concepts, approaches, design patterns, debugging strategies, etc.\n"
            "- You may ask the candidate to EXPLAIN how they would write code or describe an algorithm, but NEVER ask them to actually write or type code."
        )

    coding_turns_csv = ", ".join(str(i) for i in range(max_q_verbal + 1, max_q + 1))
    s_or_empty = "s" if coding_count > 1 else ""
    are_or_is = "s are" if coding_count > 1 else " is"

    header = (
        f"CODE EDITOR IS ENABLED — {coding_count} coding challenge{are_or_is} MANDATORY and happen at the END of the interview.\n\n"
        f"CODING PLAN for this session:\n"
        f"- Verbal questions: {max_q_verbal}  (turns 1 through {max_q_verbal})\n"
        f"- Coding questions: {coding_count}  (turns {max_q_verbal + 1} through {max_q})\n"
        f"- Total turns: {max_q}\n"
        f"- Coding turns: {coding_turns_csv}\n\n"
        f"⚠️ HARD REQUIREMENT — NON-NEGOTIABLE:\n"
        f"- On any turn where `question_index` is in the Coding turns list above, you MUST emit a `coding_question` object.\n"
        f"- On every OTHER turn, `coding_question` MUST be null.\n"
        f"- Do NOT ask coding questions before the theory round is complete.\n"
        f"- Do NOT skip the coding round even if the candidate was skipping or giving weak answers — the coding round is mandatory.\n"
        f"- Ask EXACTLY {coding_count} coding question{s_or_empty} — not more, not fewer.\n\n"
        f"When emitting a coding question, the JSON response MUST be:\n"
        f"- \"message\" EXACTLY: \"Please click on the top-left button to start the coding test.\"\n"
        f"- \"coding_question\": fully-populated object (see shape below).\n"
        f"- Do NOT read the problem description aloud. The candidate reads it in the editor.\n"
    )

    if manual_coding_enabled and manual_coding_prompts:
        recruiter_lines = "\n".join(
            f"  Coding {i+1} (turn {max_q_verbal + 1 + i}): {p}"
            for i, p in enumerate(manual_coding_prompts)
        )
        authoring = (
            f"\nAUTHORING RULES — RECRUITER-AUTHORED PROMPTS (manual_coding_questions=ON):\n"
            f"The recruiter has supplied EXACTLY {len(manual_coding_prompts)} coding problem prompt(s). "
            f"You MUST use them IN ORDER for the {coding_count} coding turn(s). "
            f"Do NOT invent your own problem when a recruiter prompt is provided.\n\n"
            f"Recruiter prompts (one per coding turn, in order):\n{recruiter_lines}\n\n"
            f"For each recruiter prompt above, expand it into the full coding_question format below. "
            f"Faithfully reflect the recruiter's intent — do NOT change the topic, but you MAY add a clear "
            f"input/output spec, constraints, and AT LEAST 3 test cases. Use stdin/stdout I/O only — no function signatures.\n"
        )
    else:
        diversity_line = (
            "\n- The problems MUST cover DIFFERENT skill areas (e.g., one on arrays, one on strings) — do not repeat the same pattern."
            if coding_count > 1 else ""
        )
        authoring = (
            f"\nAUTHORING RULES — tailor the problem to THIS candidate:\n"
            f"- The problem MUST reflect the role ({skills}) and the candidate's resume (see Candidate Background above).\n"
            f"- For a frontend / full-stack role, favour DOM-free JS logic problems (array/string/object manipulation, simple algorithms) that work via stdin/stdout.\n"
            f"- For a backend / data role, favour parsing, sorting, counting, or simple graph/tree traversal.\n"
            f"- For a senior candidate, pick \"hard\"; mid-level → \"medium\"; junior → \"easy\".{diversity_line}\n"
            f"- EVERY problem MUST include AT LEAST 3 test cases (normal + edge cases). Use stdin/stdout I/O only — no function signatures.\n"
        )

    footer = (
        "\ncoding_question format (ALL fields required):\n"
        "{\n"
        "  \"title\": \"Short problem title\",\n"
        "  \"description\": \"Full problem description with input/output format, constraints, and at least one worked example\",\n"
        "  \"test_cases\": [\n"
        "    {\"input\": \"stdin text\", \"expected_output\": \"stdout text\", \"description\": \"what this test verifies\"},\n"
        "    {\"input\": \"...\", \"expected_output\": \"...\", \"description\": \"...\"}\n"
        "  ],\n"
        "  \"time_limit\": 15,\n"
        "  \"difficulty\": \"easy | medium | hard\"\n"
        "}\n"
    )

    return header + authoring + footer


def _build_system_prompt(job: Job, candidate: Candidate, settings_obj: Optional[InterviewSettings], language: str = "en") -> str:
    skills = ", ".join([s.skill for s in job.skills]) if job.skills else "general skills"
    mode = settings_obj.mode.value if settings_obj else "TRADITIONAL"
    special = settings_obj.special_instructions if settings_obj and settings_obj.special_instructions else "None"

    resume_based = settings_obj.resume_based if settings_obj else True
    code_editor_enabled = settings_obj.code_editor_required if settings_obj else False

    predefined, ai_count, max_q_verbal, q_mode = _build_question_plan(job, settings_obj)
    coding_count = (
        (settings_obj.max_coding_questions if settings_obj else 0)
        if code_editor_enabled
        else 0
    )
    # max_q is the TOTAL number of turns (verbal phase + coding phase). The AI
    # prompt uses max_q everywhere it refers to "last question / question_index
    # ceiling", so keeping it as the grand total preserves all existing turn
    # logic. The coding-plan section below carves out the last `coding_count`
    # turns explicitly.
    max_q = max_q_verbal + coding_count
    # Pull manually-authored coding question prompts (JobQuestion rows with
    # category="coding"). These are recruiter-authored and used verbatim when
    # the job is configured with manual_coding_questions=True.
    manual_coding_enabled = bool(
        getattr(settings_obj, "manual_coding_questions", False)
        if settings_obj else False
    )
    manual_coding_prompts: list[str] = []
    if code_editor_enabled and manual_coding_enabled:
        manual_coding_prompts = [
            q.text
            for q in (job.questions or [])
            if (getattr(q, "category", None) or "").lower() == "coding"
            and (q.text or "").strip()
        ]

    resume_context = ""
    if candidate:
        resume_context = f"""
Candidate Background:
- Name: {candidate.name}
- Role Applied For: {candidate.role or job.title}
"""
        if resume_based:
            resume_context += f"- Resume Fit Score: {candidate.resume_fit_score or 'not computed'}\n"
            if candidate.fit_score_reasoning:
                try:
                    reasoning = json.loads(candidate.fit_score_reasoning)
                    resume_context += f"- Resume Summary: {reasoning.get('summary', '')}\n"
                except Exception:
                    pass
        else:
            resume_context += "- NOTE: Resume-based questions are DISABLED. Do NOT ask questions about the candidate's resume, past projects, or specific work history. Focus only on the job description, required skills, and general technical/behavioral questions.\n"

    # ── Build question plan section ──
    # Note: max_q_verbal is the count of VERBAL turns; max_q is the GRAND TOTAL
    # (verbal + coding). Coding turns are handled separately in the CODING
    # QUESTIONS section below.
    question_plan_section = ""
    if q_mode == "ONLY_PREDEFINED":
        numbered = "\n".join(f"  Q{i+1}: {q}" for i, q in enumerate(predefined))
        question_plan_section = f"""
═══════════════════════════════════════════════════════════════
QUESTION PLAN (MODE: PREDEFINED ONLY)
═══════════════════════════════════════════════════════════════

You MUST ask ONLY these EXACT predefined questions in the order shown.
- Do NOT generate your own questions
- Do NOT skip, rephrase, combine, or replace any predefined question
- Ask them EXACTLY as written
- Verbal questions to ask: {max_q_verbal}

PREDEFINED QUESTIONS (ask in this EXACT order):
{numbered}

FLOW:
1. Opening: Give intro + ask candidate to introduce themselves (question_index=0)
2. After self-intro: Acknowledge briefly, then ask Q1 as written (question_index=1)
3. For each subsequent question: Ask it as written, wait for answer, then ask next
4. After the candidate answers Q{max_q_verbal}, proceed to the coding round if enabled, otherwise set is_complete to true
"""
    elif q_mode == "MIX":
        numbered = "\n".join(f"  Q{i+1}: {q}" for i, q in enumerate(predefined))
        question_plan_section = f"""
═══════════════════════════════════════════════════════════════
QUESTION PLAN (MODE: MIX — Predefined + AI-Generated)
═══════════════════════════════════════════════════════════════

Verbal questions: {max_q_verbal}
  - Predefined: {len(predefined)} questions (must be asked FIRST in exact order)
  - AI-Generated: {ai_count} additional verbal questions (after all predefined ones)

PHASE 1 — PREDEFINED QUESTIONS (Q1 to Q{len(predefined)}):
Ask these in EXACT order, EXACTLY as written. Do NOT rephrase or skip:
{numbered}

PHASE 2 — AI-GENERATED VERBAL QUESTIONS (Q{len(predefined) + 1} to Q{max_q_verbal}):
Generate {ai_count} dynamic verbal questions based on:
  - Job description and required skills: {", ".join([s.skill for s in job.skills] if job.skills else ["general skills"])}
  - Candidate's previous answers
  - Gaps in technical depth, problem-solving, behavioral, situational coverage

FLOW:
1. Opening: Give intro + ask candidate to introduce themselves (question_index=0)
2. After self-intro: Acknowledge briefly, then ask Q1 — the first PREDEFINED question (question_index=1)
3. Questions Q1–Q{len(predefined)}: Ask predefined questions exactly as written
4. Questions Q{len(predefined) + 1}–Q{max_q_verbal}: Generate and ask {ai_count} new verbal questions
5. After Q{max_q_verbal} is answered: Proceed to the coding round if enabled, otherwise set is_complete to true

⚠️ CRITICAL RULES FOR MIX MODE — DO NOT VIOLATE:
- You MUST ask exactly {max_q_verbal} verbal questions total.
- Finishing the {len(predefined)} predefined question(s) is NOT the end of the interview.
  After Q{len(predefined)} is answered, your VERY NEXT message MUST ask Q{len(predefined) + 1}
  (the first AI-generated question). Do NOT thank the candidate, do NOT say goodbye,
  do NOT set is_complete=true. Acknowledge in one sentence and ask Q{len(predefined) + 1} immediately.
- is_complete MUST remain false for questions Q1 through Q{max_q_verbal - 1}.
- is_complete MAY become true ONLY after the candidate has answered Q{max_q_verbal}
  (or candidate explicitly asks to end).
- Coding questions (if enabled) come AFTER the {max_q_verbal} verbal questions, not before.
"""
    else:
        question_plan_section = f"""
═══════════════════════════════════════════════════════════════
QUESTION PLAN (MODE: ALL AI-GENERATED)
═══════════════════════════════════════════════════════════════

Generate ALL {max_q_verbal} VERBAL questions dynamically. No predefined questions.
- Verbal questions: {max_q_verbal}
- Base on: job description, required skills ({skills}), and candidate's answers
- Coverage: technical depth, problem-solving, system design, behavioral, situational

FLOW:
1. Opening: Give intro + ask candidate to introduce themselves (question_index=0)
2. After self-intro: Acknowledge briefly, then generate and ask Q1 (question_index=1)
3. For each subsequent question: Based on candidate's answer, generate and ask Q2, Q3, ..., Q{max_q_verbal}
4. After Q{max_q_verbal} is answered: Proceed to the coding round if enabled, otherwise set is_complete to true

CRITICAL: Ask exactly {max_q_verbal} verbal questions total. Coding questions (if enabled) come AFTER.
"""

    lang_instruction = LANGUAGE_INSTRUCTIONS.get(language, "")
    return f"""{lang_instruction}You are Nira, a senior professional interviewer. You conduct interviews like a real human — natural, responsive, concise. You are on a live video call.

Position: {job.title} ({job.department})
Location: {job.location}
Experience Required: {job.experience_min}–{job.experience_max} years
Key Skills: {skills}
Interview Mode: {mode}
Max Questions: {max_q}
Special Instructions: {special}
{resume_context}
{question_plan_section}

═══════════════════════════════════════════════════════════════
INTERVIEW MODE
═══════════════════════════════════════════════════════════════

{_get_mode_instructions(mode, skills, max_q, job.experience_min or 0, job.experience_max or 2, job.title)}

═══════════════════════════════════════════════════════════════
OPENING (2-STEP INTRO — then interview questions)
═══════════════════════════════════════════════════════════════

STEP 1 — First turn (no candidate message / "Please begin the interview"):
1. Give a SHORT 1-2 sentence welcome
2. Ask the candidate to briefly introduce themselves
3. Set question_index to 0 (this is the intro — NOT an interview question)
4. Set is_complete to false

Format: "Hi {candidate.name if candidate else 'there'}! I'm Nira, your AI interviewer for the {job.title} position. Before we begin, could you take a moment to introduce yourself? Click on the Start Speaking button below."

STEP 2 — Second turn (after candidate's self-introduction):
1. Briefly acknowledge what they said (1 sentence max — e.g., "Great, thanks for sharing that!")
2. Immediately begin Q1 from your question plan (the actual first interview question)
3. Set question_index to 1

CRITICAL RULES FOR OPENING:
- STEP 1: Do NOT ask any interview question. ONLY give intro + ask for self-introduction.
- STEP 2: Acknowledge self-intro in ONE short sentence, then ask Q1. No long transition.
- The greeting must be SHORT — max 2 sentences total
- Never explain the interview process or how many questions there will be in the opening

═══════════════════════════════════════════════════════════════
CONVERSATIONAL RULES (CRITICAL)
═══════════════════════════════════════════════════════════════

A. Keep every response CONCISE — 2-4 sentences max. You are speaking aloud, not writing.

B. NEVER repeat the same question. If you already asked for clarification, move on to the next question.

C. ANTI-REPETITION RULE: Review conversation history. If your last message asked the candidate to repeat or was a clarification request, your NEXT message MUST be a new question. Never say "I didn't catch that" twice in a row.

D. If the candidate's speech is unclear — ask for clarification ONCE only. If still unclear, say "No worries, let's move on" and ask the next question. Increment question_index.

E. ACTIVE LISTENING: Before your next question, briefly reference what the candidate said (1 sentence). Then ask the next question.

F. If candidate gives a weak/incorrect answer — provide brief feedback and move to next question. Do NOT repeatedly ask them to repeat.

G. CANDIDATE WANTS TO END: If the candidate says ANYTHING like "end interview", "close interview", "stop interview", "close this interview", "close this call", "please close", "I need to end", "I want to stop", "please end", "terminate", "finish the interview", or similar intent to stop — IMMEDIATELY set is_complete to true and say a brief thank you (1 line max). Do NOT ask another question. Prioritize this rule over all others.

H. QUESTION COUNTING (CRITICAL FOR ALL MODES):
   - question_index=0 for the opening intro turn (self-introduction request — NOT a real question)
   - question_index=1 for the FIRST actual interview question (Q1)
   - Increment question_index by 1 after EACH interview answer is received
   - You MUST ask questions from index 1 through {max_q} (inclusive)
   - After the candidate answers Q{max_q}, immediately set is_complete to true
   - Closing message: "Thank you for your time, [name]. That concludes our interview. We'll be in touch soon."
   - NEVER ask more than {max_q} questions
   - NEVER ask fewer than {max_q} questions (unless candidate ends early)

   FLOW EXAMPLE (for max_q=7):
   - Turn 1 (Opening): question_index=0, give intro + ask for self-introduction
   - Turn 2 (After self-intro): question_index=1, acknowledge + ask Q1
   - Turn 3: question_index=2, ask Q2
   - Turn 4: question_index=3, ask Q3
   - ...
   - Turn 8: question_index=7, ask Q7 — ANNOUNCE: "This is your last question."
   - Turn 9: question_index=7 (no change), set is_complete=true with closing message

I. LAST QUESTION ANNOUNCEMENT: When asking the FINAL question (question_index={max_q}), START with "Alright, this is your last question." or "One final question for you." BEFORE asking the question. This gives the candidate a heads-up.

J. If candidate asks "how many questions left" — tell them exactly: "You have {max_q} - [current_index] + 1 questions remaining."

═══════════════════════════════════════════════════════════════
CODING QUESTIONS
═══════════════════════════════════════════════════════════════
{_build_coding_section(
    code_editor_enabled=code_editor_enabled,
    coding_count=coding_count,
    max_q_verbal=max_q_verbal,
    max_q=max_q,
    skills=skills,
    manual_coding_enabled=manual_coding_enabled,
    manual_coding_prompts=manual_coding_prompts,
)}

═══════════════════════════════════════════════════════════════
RESPONSE FORMAT — STRICT RULES
═══════════════════════════════════════════════════════════════

⚠️ CRITICAL: Return ONLY a single raw JSON object. NO markdown, NO code fences, NO explanation text before or after.

Correct format (return EXACTLY like this):
{{"message": "Your spoken words here", "is_complete": false, "question_index": 1, "is_off_topic": false, "coding_question": null}}

⚠️ The "message" field MUST contain ONLY the spoken words — natural language only.
NEVER put JSON, code blocks, brackets, or curly braces inside the "message" field.
NEVER wrap your response in ```json ``` code fences.
NEVER add text before or after the JSON object.

RULES:
- "coding_question": null for normal questions (MOST turns). Include coding_question object only when asking a coding challenge.
- "is_complete":
  - Set to TRUE only when:
    * You've asked ALL {max_q} questions AND received an answer to Q{max_q}, OR
    * Candidate explicitly requests to end ("end interview", "stop", "I'm done", etc.)
  - Set to FALSE for all questions Q1 through Q{max_q}
  - Include closing message when setting is_complete to true
- "question_index":
  - Starts at 0 for the opening intro turn (self-introduction request)
  - Then 1 for Q1, 2 for Q2, ..., {max_q} for Q{max_q}
  - Increments by 1 each time you ask a new interview question
  - Must reach {max_q} before is_complete can be true
  - NEVER exceed {max_q}
- "is_off_topic": true when candidate response is completely irrelevant to the question (e.g., answering a technical question with "the weather is nice"). Proceed to next question if true."""


def _count_recent_fallbacks(db: Session, session_id: int, limit: int = 5) -> int:
    """Count how many of the last N AI messages are fallback/clarification messages."""
    recent = (
        db.query(InterviewMessage)
        .filter(InterviewMessage.session_id == session_id, InterviewMessage.role == "ai")
        .order_by(InterviewMessage.created_at.desc())
        .limit(limit)
        .all()
    )
    fallback_phrases = [
        "could you repeat",
        "didn't quite catch",
        "brief hiccup",
        "audio issue",
        "connection issue",
        "could you say that again",
        "didn't hear that clearly",
    ]
    count = 0
    for m in recent:
        lower = m.content.lower()
        if any(p in lower for p in fallback_phrases):
            count += 1
    return count


FALLBACK_RESPONSES = {
    "en": {
        "intro": "Hello! I'm Nira, and I'll be conducting your interview today for the {title} position. Thank you for joining us. Let's start with a quick introduction — could you tell me about your background and experience?",
        "short_1": "I didn't quite catch that. Could you elaborate a bit more on your answer?",
        "short_2": "It seems there may be an audio issue. Could you please speak a bit more clearly and give me a detailed answer?",
        "follow_ups": [
            "That's an interesting perspective. Let me ask you something related — can you tell me about your experience with {skills}?",
            "Thank you for sharing that. Moving on, could you describe a challenging project you've worked on recently?",
            "I appreciate your answer. Let's explore another area — how do you approach problem-solving when you encounter an unfamiliar technical challenge?",
            "Good to know. Let me shift to a different topic — can you walk me through your experience with team collaboration and code reviews?",
            "Thanks for that. Tell me, how do you stay updated with new technologies and best practices in your field?",
        ],
        "move_on": [
            "No worries, let's move on. Can you tell me about your experience with {skills}?",
            "Let's try a different question. What's a technical challenge you've solved recently that you're proud of?",
            "Let me ask something else. How do you handle debugging when you encounter a complex issue in production?",
            "Let's switch topics. Can you describe your ideal development workflow and the tools you use daily?",
        ],
    },
    "hi": {
        "intro": "नमस्ते! मैं नीरा हूँ, और आज मैं {title} पद के लिए आपका इंटरव्यू ले रही हूँ। जुड़ने के लिए धन्यवाद। चलिए शुरू करते हैं — क्या आप अपने बारे में और अपने अनुभव के बारे में बता सकते हैं?",
        "short_1": "मैं ठीक से सुन नहीं पाई। क्या आप अपने जवाब को थोड़ा और विस्तार से बता सकते हैं?",
        "short_2": "लगता है कुछ ऑडियो समस्या है। कृपया थोड़ा स्पष्ट रूप से बोलें और विस्तृत जवाब दें।",
        "follow_ups": [
            "बहुत अच्छा। अब मुझे बताइए — {skills} के साथ आपका अनुभव कैसा रहा है?",
            "धन्यवाद। आगे बढ़ते हैं — क्या आप हाल ही में किसी चुनौतीपूर्ण प्रोजेक्ट के बारे में बता सकते हैं?",
            "अच्छा जवाब। एक और सवाल — जब आपको कोई अपरिचित तकनीकी समस्या मिलती है तो आप कैसे हल करते हैं?",
            "समझ गई। अब बताइए — टीम के साथ काम करने और कोड रिव्यू का आपका अनुभव कैसा है?",
            "धन्यवाद। बताइए, आप नई तकनीकों से कैसे अपडेट रहते हैं?",
        ],
        "move_on": [
            "कोई बात नहीं, आगे बढ़ते हैं। {skills} के बारे में बताइए।",
            "चलिए दूसरा सवाल पूछते हैं। हाल ही में कोई तकनीकी चुनौती जो आपने हल की हो?",
            "कुछ और पूछते हैं। प्रोडक्शन में जटिल बग मिलने पर आप कैसे डिबग करते हैं?",
            "विषय बदलते हैं। अपने आदर्श डेवलपमेंट वर्कफ्लो के बारे में बताइए।",
        ],
    },
    "ar": {
        "intro": "مرحباً! أنا نيرا، وسأجري مقابلتك اليوم لمنصب {title}. شكراً لانضمامك. لنبدأ بمقدمة سريعة — هل يمكنك إخباري عن خلفيتك وخبرتك؟",
        "short_1": "لم أسمع ذلك جيداً. هل يمكنك التوضيح أكثر؟",
        "short_2": "يبدو أن هناك مشكلة في الصوت. يرجى التحدث بوضوح أكثر وإعطاء إجابة مفصلة.",
        "follow_ups": [
            "منظور مثير للاهتمام. أخبرني عن خبرتك في {skills}؟",
            "شكراً لمشاركتك. هل يمكنك وصف مشروع صعب عملت عليه مؤخراً؟",
            "إجابة جيدة. كيف تتعامل مع حل المشكلات عندما تواجه تحدياً تقنياً غير مألوف؟",
            "جيد. أخبرني عن تجربتك في العمل الجماعي ومراجعة الكود؟",
            "شكراً. كيف تبقى محدثاً بأحدث التقنيات وأفضل الممارسات؟",
        ],
        "move_on": [
            "لا بأس، لننتقل. أخبرني عن خبرتك في {skills}.",
            "لنجرب سؤالاً آخر. ما هو التحدي التقني الذي حللته مؤخراً؟",
            "سأسأل شيئاً آخر. كيف تتعامل مع الأخطاء المعقدة في الإنتاج؟",
            "لنغير الموضوع. صف سير عملك المثالي في التطوير.",
        ],
    },
    "hinglish": {
        "intro": "Hello! Main Nira hoon, aur aaj main {title} position ke liye aapka interview le rahi hoon. Joining ke liye thank you. Chaliye shuru karte hain — apne baare mein aur apne experience ke baare mein bataiye?",
        "short_1": "Main theek se sun nahi paayi. Kya aap apna answer thoda detail mein bata sakte hain?",
        "short_2": "Lagta hai kuch audio issue hai. Please thoda clearly bolein aur detailed answer dein.",
        "follow_ups": [
            "Bahut accha. Ab mujhe bataiye — {skills} ke saath aapka experience kaisa raha hai?",
            "Thank you. Aage badhte hain — kya aap recently kisi challenging project ke baare mein bata sakte hain?",
            "Nice answer. Ek aur question — jab aapko koi unfamiliar technical problem milti hai toh aap kaise solve karte hain?",
            "Samajh gayi. Ab bataiye — team ke saath kaam karne aur code review ka aapka experience kaisa hai?",
            "Thanks. Bataiye, aap new technologies se kaise updated rehte hain?",
        ],
        "move_on": [
            "Koi baat nahi, aage badhte hain. {skills} ke baare mein bataiye.",
            "Chaliye doosra question puchte hain. Recently koi technical challenge jo aapne solve ki ho?",
            "Kuch aur puchte hain. Production mein complex bug milne par aap kaise debug karte hain?",
            "Topic change karte hain. Apne ideal development workflow ke baare mein bataiye.",
        ],
    },
}


def _build_contextual_fallback(
    candidate_message: Optional[str],
    consecutive_fallbacks: int,
    session: InterviewSession,
    job: Job,
    db: Session,
) -> str:
    """
    Generate a contextual, non-repetitive response when Claude API fails.
    Language-aware — uses the session's interview_language.
    """
    language = getattr(session, "interview_language", "en") or "en"
    fb = FALLBACK_RESPONSES.get(language, FALLBACK_RESPONSES["en"])
    skills = ", ".join([s.skill for s in job.skills]) if job and job.skills else "the role"

    is_skip = candidate_message and "[SKIP]" in candidate_message
    if is_skip:
        # Skip — always move to a different question from the pool
        contextual_responses = [r.format(skills=skills, title=job.title) for r in fb["move_on"]]
    elif candidate_message and len(candidate_message.strip()) > 5:
        contextual_responses = [r.format(skills=skills, title=job.title) for r in fb["follow_ups"]]
    elif candidate_message and len(candidate_message.strip()) <= 5:
        if consecutive_fallbacks == 0:
            return fb["short_1"]
        elif consecutive_fallbacks == 1:
            return fb["short_2"]
        else:
            contextual_responses = [r.format(skills=skills, title=job.title) for r in fb["move_on"]]
    else:
        return fb["intro"].format(title=job.title, skills=skills)

    # Use question_index + fallback count to cycle through responses without repeating
    q_idx = getattr(session, "current_question_index", 0) or 0
    idx = (q_idx + consecutive_fallbacks) % len(contextual_responses)
    return contextual_responses[idx]


async def conduct_turn(
    session: InterviewSession,
    candidate_message: Optional[str],
    db: Session,
) -> dict:
    """Drive one interview turn. Wraps the main body so that ANY unexpected
    exception (sanitisation regex bug, AI returning weird shapes, DB transient
    error) still leaves the chat history in a consistent state — the user's
    answer is saved and a graceful reply is returned instead of a 500 that
    drops the message.

    The reply is sourced from `_build_contextual_fallback` so consecutive
    failures rotate through varied phrasings (and respect the candidate's
    interview language) — that prevents the "Sorry, I had a brief hiccup"
    text from being emitted verbatim three turns in a row.
    """
    try:
        return await _conduct_turn_impl(session, candidate_message, db)
    except Exception as exc:
        logger.exception(
            f"[conduct_turn] unhandled error for session {session.id}: {exc}"
        )
        try:
            db.rollback()
        except Exception:
            pass

        # Re-resolve job/candidate after rollback so the fallback path can use
        # them. Use a fresh query — if even THIS fails we degrade further.
        try:
            job = db.query(Job).filter(Job.id == session.job_id).first()
        except Exception:
            job = None

        # Last-resort: save the candidate message + a varied AI reply so the
        # turn isn't lost. If THIS also fails, we re-raise so the client at
        # least sees an error rather than thinking the answer was accepted.
        try:
            if candidate_message is not None:
                db.add(InterviewMessage(
                    session_id=session.id,
                    role="candidate",
                    content=candidate_message,
                    question_index=session.current_question_index,
                ))

            consecutive_fallbacks = _count_recent_fallbacks(db, session.id)
            # Always advance the question pointer on emergency fallback so the
            # candidate isn't stuck answering the same question forever when
            # the AI service is degraded. Without this, the model keeps
            # echoing the same generic prompt because question_index never
            # moves.
            next_qi = (session.current_question_index or 0) + 1

            try:
                fallback_msg = _build_contextual_fallback(
                    candidate_message, consecutive_fallbacks, session, job, db,
                )
            except Exception as fb_exc:
                logger.warning(
                    f"[conduct_turn] contextual fallback failed: {fb_exc}. "
                    f"Using hardcoded reply."
                )
                # Language-aware hardcoded backstop in case even the
                # contextual builder is broken (e.g. corrupted FALLBACK_RESPONSES).
                _lang = getattr(session, "interview_language", "en") or "en"
                _backstops = {
                    "en": "Let's keep things moving. Could you tell me about a recent project you've worked on?",
                    "hi": "चलिए आगे बढ़ते हैं। क्या आप किसी हाल के प्रोजेक्ट के बारे में बता सकते हैं?",
                    "ar": "لنواصل. هل يمكنك إخباري عن مشروع حديث عملت عليه؟",
                    "hinglish": "Chaliye aage badhte hain. Kya aap kisi recent project ke baare mein bata sakte hain?",
                }
                fallback_msg = _backstops.get(_lang, _backstops["en"])

            db.add(InterviewMessage(
                session_id=session.id,
                role="ai",
                content=fallback_msg,
                question_index=next_qi,
            ))
            session.current_question_index = next_qi
            _safe_commit(db, f"conduct_turn emergency fallback session={session.id}")
            return {
                "session_id": session.id,
                "message": fallback_msg,
                "question_index": next_qi,
                "is_complete": False,
                "is_off_topic": False,
                "role": "ai",
            }
        except Exception as inner:
            logger.error(
                f"[conduct_turn] emergency fallback ALSO failed for session "
                f"{session.id}: {inner}. Re-raising original exception."
            )
            raise exc


async def _conduct_turn_impl(
    session: InterviewSession,
    candidate_message: Optional[str],
    db: Session,
) -> dict:
    job = db.query(Job).filter(Job.id == session.job_id).first()
    candidate = db.query(Candidate).filter(Candidate.id == session.candidate_id).first() if session.candidate_id else None
    interview_settings = job.interview_settings if job else None

    # ── Server-side guard: detect candidate requesting to end ──
    # max_questions on InterviewSettings is the VERBAL question count (post-feature).
    # The total interview length is verbal + coding. `max_q` represents total turns
    # so the AI prompt's question_index references stay correct.
    _verbal_count = interview_settings.max_questions if interview_settings else 8
    _code_enabled_v = bool(
        interview_settings.code_editor_required if interview_settings else False
    )
    _coding_count_v = (
        interview_settings.max_coding_questions
        if (interview_settings and _code_enabled_v)
        else 0
    )
    max_q = _verbal_count + _coding_count_v

    # One-shot diagnostic at the start of a session: confirms whether the
    # code editor is wired up for this interview and which turns are the
    # coding turns (the last N turns of the interview, where N is the
    # configured max_coding_questions). Helps debug "no coding question
    # appeared" reports.
    if (session.current_question_index or 0) == 0:
        _coding_turns = (
            list(range(_verbal_count + 1, max_q + 1)) if _coding_count_v > 0 else []
        )
        logger.info(
            f"[conduct_turn] session={session.id} job={getattr(job,'id',None)} "
            f"code_editor_required={_code_enabled_v} verbal={_verbal_count} "
            f"coding={_coding_count_v} max_q={max_q} coding_turns={_coding_turns}"
        )
    if candidate_message is not None:
        lower_msg = candidate_message.lower().strip()
        end_phrases = [
            "end interview", "close interview", "stop interview",
            "finish interview", "terminate interview", "cancel interview",
            "close this interview", "end this interview", "end this call",
            "close this call", "end call", "please close", "please end",
            "please stop", "i want to end", "i want to stop", "i need to end",
            "need to end up", "end up", "close the interview", "end the interview",
            "want to leave", "need to leave",
        ]
        if any(phrase in lower_msg for phrase in end_phrases):
            lang = getattr(session, "interview_language", "en") or "en"
            name_part = f", {candidate.name}" if candidate else ""
            _closings = {
                "en": f"Thank you for your time{name_part}. It was great talking with you. We'll be in touch soon!",
                "hi": f"आपके समय के लिए धन्यवाद{name_part}। आपसे बात करके अच्छा लगा। हम जल्द ही संपर्क करेंगे!",
                "ar": f"شكراً لوقتك{name_part}. كان من الرائع التحدث معك. سنتواصل معك قريباً!",
                "hinglish": f"Aapke time ke liye thank you{name_part}. Aapse baat karke accha laga. Hum jaldi contact karenge!",
            }
            closing = _closings.get(lang, _closings["en"])
            db.add(InterviewMessage(
                session_id=session.id,
                role="candidate",
                content=candidate_message,
                question_index=session.current_question_index,
            ))
            closing_msg = InterviewMessage(
                session_id=session.id,
                role="ai",
                content=closing,
                question_index=None,  # closing message is NOT a question
            )
            db.add(closing_msg)
            session.status = SessionStatus.completed
            session.completed_at = datetime.utcnow()
            db.commit()
            return {
                "session_id": session.id,
                "message": closing,
                "question_index": session.current_question_index,
                "is_complete": True,
                "is_off_topic": False,
                "role": "ai",
            }

    # ── Server-side guard: force completion if max_questions exceeded ──
    if candidate_message is not None and session.current_question_index >= max_q:
        # Save the candidate's final answer before closing
        db.add(InterviewMessage(
            session_id=session.id,
            role="candidate",
            content=candidate_message,
            question_index=session.current_question_index,
        ))
        lang = getattr(session, "interview_language", "en") or "en"
        name_part = f", {candidate.name}" if candidate else ""
        _closings2 = {
            "en": f"Thank you for your time{name_part}. That concludes our interview. We'll be in touch soon.",
            "hi": f"आपके समय के लिए धन्यवाद{name_part}। हमारा इंटरव्यू पूरा हुआ। हम जल्द ही संपर्क करेंगे।",
            "ar": f"شكراً لوقتك{name_part}. هذا يختتم مقابلتنا. سنتواصل معك قريباً.",
            "hinglish": f"Aapke time ke liye thank you{name_part}. Humara interview complete hua. Hum jaldi contact karenge.",
        }
        closing = _closings2.get(lang, _closings2["en"])
        closing_msg = InterviewMessage(
            session_id=session.id,
            role="ai",
            content=closing,
            question_index=None,  # closing message is NOT a question
        )
        db.add(closing_msg)
        session.status = SessionStatus.completed
        session.completed_at = datetime.utcnow()
        db.commit()
        return {
            "session_id": session.id,
            "message": closing,
            "question_index": session.current_question_index,
            "is_complete": True,
            "is_off_topic": False,
            "role": "ai",
        }

    language = getattr(session, "interview_language", "en") or "en"
    system_prompt = _build_system_prompt(job, candidate, interview_settings, language)

    past_messages = (
        db.query(InterviewMessage)
        .filter(InterviewMessage.session_id == session.id)
        .order_by(InterviewMessage.created_at)
        .all()
    )

    claude_messages = []
    for msg in past_messages:
        claude_messages.append({
            "role": "user" if msg.role == "candidate" else "assistant",
            "content": msg.content,
        })

    if candidate_message is None:
        claude_messages.append({"role": "user", "content": "Please begin the interview."})
    else:
        db.add(InterviewMessage(
            session_id=session.id,
            role="candidate",
            content=candidate_message,
            question_index=session.current_question_index,
        ))
        db.flush()
        claude_messages.append({"role": "user", "content": candidate_message})

    # ── Attempt 1: Call Claude with standard settings ──
    result = None
    raw = None

    try:
        llm_result = await _ask_with_system_usage(system_prompt, claude_messages, model=SONNET, max_tokens=1024, timeout_seconds=25)
        log_usage(llm_result, feature="interview", session_id=session.id)
        raw = llm_result.text
        result = _parse_json(raw)
    except Exception as e:
        logger.warning(f"Claude attempt 1 failed for session {session.id}: {e}")

    # ── Attempt 2: Retry once with shorter timeout, after a short backoff ──
    # 1-second delay is short enough that the candidate's wait time barely
    # changes, but long enough to avoid hammering a struggling LLM provider
    # back-to-back. We add a small random jitter so concurrent candidates
    # don't sync up.
    if result is None:
        import random as _r
        await asyncio.sleep(1 + _r.uniform(0, 0.5))
        try:
            llm_result = await _ask_with_system_usage(system_prompt, claude_messages, model=SONNET, max_tokens=1024, timeout_seconds=20)
            log_usage(llm_result, feature="interview", session_id=session.id)
            raw = llm_result.text
            result = _parse_json(raw)
        except Exception as e:
            logger.warning(f"Claude attempt 2 failed for session {session.id}: {e}")

    # ── Attempt 3: If JSON parse failed but we got raw text, salvage it ──
    if result is None and raw and len(raw.strip()) > 10:
        clean_text = raw.strip()
        # Try to find and parse a JSON object anywhere in the raw text
        json_match = re.search(r'\{.*\}', clean_text, flags=re.DOTALL)
        if json_match:
            try:
                parsed = json.loads(json_match.group(0))
                if isinstance(parsed, dict) and "message" in parsed:
                    result = {
                        "message": parsed.get("message", ""),
                        "is_complete": parsed.get("is_complete", False),
                        "question_index": parsed.get("question_index", session.current_question_index),
                        "is_off_topic": parsed.get("is_off_topic", False),
                        "coding_question": parsed.get("coding_question"),
                    }
            except Exception:
                pass

        # Try regex extraction of message field from anywhere in raw text.
        # Capture up to the NEXT known field key (or closing brace) rather than
        # the first quote — so an UNESCAPED quote inside the question (e.g. the
        # model citing a term in quotes) no longer truncates Nira's message to
        # just the acknowledgment ("...Given"). This was the core bug where the
        # next question vanished and only the lead-in survived.
        if result is None:
            try:
                msg_match = re.search(
                    r'"message"\s*:\s*"(.*?)"\s*(?:,\s*"(?:is_complete|is_off_topic|question_index|coding_question)"\s*:|}\s*$)',
                    clean_text,
                    flags=re.DOTALL,
                )
                if not msg_match:
                    # Fall back to the conservative single-quote-terminated match.
                    msg_match = re.search(r'"message"\s*:\s*"((?:[^"\\]|\\.)*)"', clean_text)
                if msg_match:
                    extracted = msg_match.group(1).replace('\\"', '"').replace('\\n', ' ').strip()
                    if len(extracted) > 5:
                        result = {
                            "message": extracted,
                            "is_complete": False,
                            "question_index": session.current_question_index,
                            "is_off_topic": False,
                        }
            except Exception:
                pass

        # Last resort: use raw text only if it doesn't look like JSON
        if result is None and not clean_text.startswith("{") and not clean_text.startswith("["):
            if len(clean_text) > 10:
                result = {
                    "message": clean_text,
                    "is_complete": False,
                    "question_index": session.current_question_index,
                    "is_off_topic": False,
                }

    # ── All attempts failed — use contextual fallback ──
    if result is None:
        consecutive_fallbacks = _count_recent_fallbacks(db, session.id)
        is_skip = candidate_message and "[SKIP]" in candidate_message
        fallback_msg = _build_contextual_fallback(
            candidate_message, consecutive_fallbacks, session, job, db
        )
        q_index = session.current_question_index
        # Always advance question on skip, or after 2+ consecutive fallbacks
        if candidate_message is not None and (is_skip or consecutive_fallbacks >= 2):
            q_index = (session.current_question_index or 0) + 1
            session.current_question_index = q_index

        db.add(InterviewMessage(
            session_id=session.id,
            role="ai",
            content=fallback_msg,
            question_index=q_index,
        ))
        _safe_commit(db, f"conduct_turn fallback session={session.id}")
        return {
            "session_id": session.id,
            "message": fallback_msg,
            "question_index": q_index,
            "is_complete": False,
            "is_off_topic": False,
            "role": "ai",
        }

    message_text = result.get("message", "Thank you for your time.")
    is_complete = result.get("is_complete", False)
    is_off_topic = result.get("is_off_topic", False)
    raw_question_index = result.get("question_index", session.current_question_index + 1)
    coding_question = result.get("coding_question")

    # ── Validate question_index — the AI model sometimes returns the same
    # index for multiple different questions, causing them to collapse in
    # evaluation. Force forward progress when a candidate message was received
    # (meaning a new question should follow).
    current_qi = session.current_question_index or 0
    if candidate_message is not None and not is_complete:
        if raw_question_index is not None and raw_question_index <= current_qi:
            logger.warning(
                f"[conduct_turn] AI returned question_index={raw_question_index} "
                f"<= current={current_qi} for session {session.id}. "
                f"Force-incrementing to {current_qi + 1}."
            )
            raw_question_index = current_qi + 1
    question_index = raw_question_index if raw_question_index is not None else current_qi + 1

    # ── Guard against premature completion ──
    # In MIX mode the model has been observed to set is_complete=true after the
    # predefined questions run out, even though Phase 2 (AI-generated questions)
    # should follow. The explicit "candidate wants to end" path already returned
    # earlier in this function, so by this point any is_complete=true that
    # arrives before we've reached max_q is a model bug. Re-prompt Claude to
    # produce the next question instead of cutting the interview short.
    if (
        is_complete
        and question_index < max_q
        and candidate_message is not None
    ):
        logger.warning(
            f"[conduct_turn] AI returned is_complete=true at question_index="
            f"{question_index} but max_q={max_q} for session {session.id}. "
            f"Re-prompting for the next question."
        )
        next_qi = max(current_qi + 1, question_index)
        repair_messages = list(claude_messages) + [
            {
                "role": "assistant",
                "content": json.dumps({
                    "message": message_text or "",
                    "is_complete": True,
                    "question_index": question_index,
                }),
            },
            {
                "role": "user",
                "content": (
                    f"SYSTEM CORRECTION: Do not end the interview yet. "
                    f"You have asked {question_index} of {max_q} questions. "
                    f"Ask the next interview question now. Set is_complete=false "
                    f"and question_index={next_qi}. Return ONLY the JSON object."
                ),
            },
        ]
        try:
            repair_result = await _ask_with_system_usage(
                system_prompt, repair_messages, model=SONNET,
                max_tokens=1024, timeout_seconds=20,
            )
            log_usage(repair_result, feature="interview", session_id=session.id)
            repaired = _parse_json(repair_result.text)
            if isinstance(repaired, dict) and repaired.get("message"):
                message_text = str(repaired.get("message"))
                is_complete = bool(repaired.get("is_complete", False))
                question_index = int(repaired.get("question_index", next_qi))
                coding_question = repaired.get("coding_question") or coding_question
        except Exception as repair_exc:
            logger.warning(
                f"[conduct_turn] re-prompt after premature is_complete failed "
                f"for session {session.id}: {repair_exc}. Falling back to bridge."
            )

        # If the repair call still left us with is_complete=true (or no good
        # message), fall back to a bridge so the interview doesn't terminate.
        if is_complete and question_index < max_q:
            is_complete = False
            question_index = next_qi
            _closing_signals = ("thank you for your time", "that concludes",
                                "we'll be in touch", "धन्यवाद", "شكراً")
            if any(s in (message_text or "").lower() for s in _closing_signals):
                message_text = "Got it, thanks. Let's continue with the next question."

    # Sanitize: strip any embedded JSON / code blocks from the spoken message
    # This prevents the AI from leaking raw JSON structure into the chat

    # 1. Strip code fences containing JSON (including nested braces)
    message_text = re.sub(r'```[a-zA-Z]*\s*\{.*?\}\s*```', '', message_text, flags=re.DOTALL).strip()
    message_text = re.sub(r'```[a-zA-Z]*\s*\{[^`]*\}', '', message_text, flags=re.DOTALL).strip()

    # 2. If message_text is itself a raw JSON object, re-extract the message field
    if message_text.strip().startswith('{'):
        try:
            inner = json.loads(message_text.strip())
            if isinstance(inner, dict) and 'message' in inner:
                message_text = str(inner['message'])
        except Exception:
            try:
                msg_match = re.search(r'"message"\s*:\s*"((?:[^"\\]|\\.)*)"', message_text)
                if msg_match:
                    message_text = msg_match.group(1).replace('\\"', '"').replace('\\n', ' ')
                else:
                    message_text = "Thank you. Let's continue."
            except Exception:
                message_text = "Thank you. Let's continue."

    # 3. Strip any JSON block from the message text.
    # Find the first '{' that likely starts a JSON object and remove everything from there
    # This handles cases where Claude appends raw JSON after the spoken message
    json_start = message_text.find('{"')
    if json_start == -1:
        json_start = message_text.find('{ "')
    if json_start > 0:
        # Keep only the text before the JSON block
        before_json = message_text[:json_start].strip()
        if len(before_json) > 10:
            message_text = before_json
        else:
            # Try to extract message from the JSON part
            json_part = message_text[json_start:]
            try:
                parsed_inner = json.loads(json_part)
                if isinstance(parsed_inner, dict) and 'message' in parsed_inner:
                    message_text = str(parsed_inner['message'])
            except Exception:
                msg_m = re.search(r'"message"\s*:\s*"((?:[^"\\]|\\.)*)"', json_part)
                if msg_m:
                    message_text = msg_m.group(1).replace('\\"', '"').replace('\\n', ' ').strip()

    # 4. Strip inline JSON fragments with interview-specific keys (simple non-nested)
    message_text = re.sub(
        r'\{[^{}]*"(?:is_complete|question_index|coding_question|is_off_topic)"[^{}]*\}',
        '', message_text, flags=re.DOTALL
    ).strip()

    # 5. Collapse extra whitespace
    message_text = re.sub(r'\s{2,}', ' ', message_text).strip()
    if not message_text:
        message_text = "Thank you. Let's continue."

    # ── Coding-question handling (computed BEFORE commit so the candidate
    # message, AI message, session updates, and any coding override all
    # commit atomically — no half-state can leak if any single step throws).
    # Plan: the LAST `max_coding_questions` turns of the interview are coding,
    # appended after the verbal phase. Example with max_questions=5 (verbal)
    # and max_coding_questions=2 (coding): turns 1-5 verbal, turns 6-7 coding,
    # total max_q=7. If max_coding_questions is 0 (or code_editor_required is
    # false), there is no coding round even if the AI emits a coding_question.
    code_editor_enabled = bool(
        interview_settings.code_editor_required if interview_settings else False
    )
    coding_count = (
        (interview_settings.max_coding_questions if interview_settings else 0)
        if code_editor_enabled else 0
    )
    verbal_count = interview_settings.max_questions if interview_settings else 8
    coding_turns = (
        set(range(verbal_count + 1, max_q + 1)) if coding_count > 0 else set()
    )
    manual_coding_enabled = bool(
        getattr(interview_settings, "manual_coding_questions", False)
        if interview_settings else False
    )
    manual_coding_prompts: list[str] = []
    if code_editor_enabled and manual_coding_enabled and job is not None:
        manual_coding_prompts = [
            q.text
            for q in (job.questions or [])
            if (getattr(q, "category", None) or "").lower() == "coding"
            and (q.text or "").strip()
        ]

    # Count coding questions already asked in this session so we don't
    # double-inject if the AI already produced one on an earlier coding turn.
    prior_coding_asked = 0
    if code_editor_enabled:
        # Match either the first-coding intro or any of the follow-up transition
        # phrases we may have used after a prior code submission. Without this,
        # the count stays at 1 forever once we switch wording, breaking fallback
        # selection on a 3rd+ coding turn.
        prior_coding_asked = (
            db.query(InterviewMessage)
            .filter(
                InterviewMessage.session_id == session.id,
                InterviewMessage.role == "ai",
            )
            .filter(
                InterviewMessage.content.ilike("%Switch to Coding Test%")
                | InterviewMessage.content.ilike("%next coding challenge%")
            )
            .count()
        )

    is_coding_turn = code_editor_enabled and question_index in coding_turns

    # When manual_coding_questions is ON, the recruiter prompt for THIS turn is
    # the source of truth. Always overwrite whatever the AI emitted so the
    # candidate sees the recruiter's intended problem (the AI may otherwise
    # drift off-topic or paraphrase too aggressively).
    if (
        is_coding_turn
        and not is_complete
        and manual_coding_enabled
        and manual_coding_prompts
        and prior_coding_asked < coding_count
    ):
        prompt_idx = min(prior_coding_asked, len(manual_coding_prompts) - 1)
        manual_prompt = manual_coding_prompts[prompt_idx]
        if not (coding_question and isinstance(coding_question, dict)
                and (coding_question.get("description") or "").strip()):
            # AI didn't expand the prompt — fall back to a deterministic problem
            # seeded by the recruiter's prompt text.
            coding_question = _fallback_coding_question(
                job, candidate, prior_coding_asked, manual_prompt=manual_prompt,
            )
        else:
            # Preserve the recruiter's intent: prepend their original prompt to
            # the AI-expanded description so the candidate sees both.
            existing_desc = (coding_question.get("description") or "").strip()
            if manual_prompt.strip() not in existing_desc:
                coding_question["description"] = (
                    manual_prompt.strip() + "\n\n" + existing_desc
                )

    if (not coding_question) and is_coding_turn and not is_complete \
            and prior_coding_asked < coding_count:
        logger.warning(
            f"[conduct_turn] AI did not emit coding_question at turn "
            f"{question_index} (coding_turns={sorted(coding_turns)}) "
            f"for session {session.id} — injecting fallback."
        )
        manual_prompt = None
        if manual_coding_enabled and manual_coding_prompts:
            prompt_idx = min(prior_coding_asked, len(manual_coding_prompts) - 1)
            manual_prompt = manual_coding_prompts[prompt_idx]
        coding_question = _fallback_coding_question(
            job, candidate, prior_coding_asked, manual_prompt=manual_prompt,
        )

    coding_question_for_response: Optional[dict] = None
    if coding_question and isinstance(coding_question, dict):
        coding_question_for_response = {
            "title": coding_question.get("title", "Coding Challenge"),
            "description": coding_question.get("description", ""),
            "test_cases": coding_question.get("test_cases", []),
            "time_limit": coding_question.get("time_limit", 15),
            "difficulty": coding_question.get("difficulty", "medium"),
        }
        # Force the spoken message — never read the problem aloud. The first
        # coding question gets the full editor instruction; subsequent ones get
        # a short transition because the editor is already open and the
        # candidate already knows how to use it.
        if prior_coding_asked >= 1:
            message_text = "Great, here is your next coding challenge. The new problem has loaded in the editor — please review it and submit your solution."
        else:
            message_text = "Now let's test your coding skills. Please click the 'Switch to Coding Test' button at the top of your screen to open the code editor and complete the challenge."

    # ── Single atomic write of the whole turn ─────────────────────────────────
    # Candidate message, AI message, session status, and coding-override text
    # all commit together. If ANY of these throws, the in-flight transaction
    # rolls back (SQLAlchemy auto-rollback on session close) and no partial
    # state pollutes the chat history. We retry the commit on transient errors
    # (psycopg disconnects, deadlocks) so a brief DB hiccup doesn't drop the
    # turn — this is the user's main "data not saving" concern.
    db.add(InterviewMessage(
        session_id=session.id,
        role="ai",
        content=message_text,
        question_index=None if is_complete else question_index,
    ))
    session.current_question_index = question_index
    if is_complete:
        session.status = SessionStatus.completed
        session.completed_at = datetime.utcnow()

    _safe_commit(db, f"conduct_turn session={session.id}")

    response = {
        "session_id": session.id,
        "message": message_text,
        "question_index": question_index,
        "is_complete": is_complete,
        "is_off_topic": is_off_topic,
        "role": "ai",
    }
    if coding_question_for_response is not None:
        response["coding_question"] = coding_question_for_response
        logger.info(
            f"[conduct_turn] Coding question delivered for session {session.id} "
            f"at turn {question_index}: {coding_question_for_response.get('title')}"
        )

    return response


_FALLBACK_PROBLEMS: list[dict] = [
    {
        "title": "Two Sum",
        "description": (
            "Given an array of integers and a target value, print the indices "
            "(0-based, space-separated) of the two numbers that add up to the "
            "target. Exactly one valid answer exists; you may not use the same "
            "element twice.\n\n"
            "Input:\n  Line 1: space-separated integers (the array)\n"
            "  Line 2: target integer\n\n"
            "Output:\n  Single line — the two indices separated by a space, smaller first.\n\n"
            "Example:\n  Input:\n    2 7 11 15\n    9\n  Output:\n    0 1"
        ),
        "test_cases": [
            {"input": "2 7 11 15\n9\n", "expected_output": "0 1", "description": "Answer at start"},
            {"input": "3 2 4\n6\n", "expected_output": "1 2", "description": "Answer not first pair"},
            {"input": "3 3\n6\n", "expected_output": "0 1", "description": "Duplicate values"},
            {"input": "-1 -2 -3 -4 -5\n-8\n", "expected_output": "2 4", "description": "Negative numbers"},
        ],
        "difficulty": "medium",
    },
    {
        "title": "Longest Word in a Sentence",
        "description": (
            "Given a sentence, print the longest word. If two words tie in "
            "length, print the one that appears FIRST in the sentence. Words "
            "are separated by single spaces; there is no punctuation.\n\n"
            "Input:\n  A single line containing the sentence.\n\n"
            "Output:\n  A single word — the longest one.\n\n"
            "Example:\n  Input:\n    the quick brown fox\n  Output:\n    quick"
        ),
        "test_cases": [
            {"input": "the quick brown fox\n", "expected_output": "quick", "description": "Simple case"},
            {"input": "hello world\n", "expected_output": "hello", "description": "Tie — first wins"},
            {"input": "a bb ccc dddd eeeee\n", "expected_output": "eeeee", "description": "Ascending lengths"},
            {"input": "one\n", "expected_output": "one", "description": "Single word"},
        ],
        "difficulty": "easy",
    },
    {
        "title": "Character Frequency",
        "description": (
            "Given a lowercase string, print each distinct character and its "
            "count, one per line, in ASCENDING character order. Format: "
            "`<char> <count>`.\n\n"
            "Input:\n  One line — a lowercase string (no spaces).\n\n"
            "Output:\n  Each distinct character and its count, ascending.\n\n"
            "Example:\n  Input:\n    banana\n  Output:\n    a 3\n    b 1\n    n 2"
        ),
        "test_cases": [
            {"input": "banana\n", "expected_output": "a 3\nb 1\nn 2", "description": "Mixed counts"},
            {"input": "aaa\n", "expected_output": "a 3", "description": "Single char"},
            {"input": "abc\n", "expected_output": "a 1\nb 1\nc 1", "description": "All unique"},
            {"input": "abba\n", "expected_output": "a 2\nb 2", "description": "Palindrome"},
        ],
        "difficulty": "easy",
    },
    {
        "title": "Balanced Parentheses",
        "description": (
            "Given a string containing only the characters `(`, `)`, `[`, `]`, "
            "`{`, `}`, print `valid` if every opening bracket has a matching "
            "closer in the correct order, otherwise `invalid`.\n\n"
            "Input:\n  One line — the bracket string (may be empty).\n\n"
            "Output:\n  `valid` or `invalid`.\n\n"
            "Example:\n  Input:\n    ({[]})\n  Output:\n    valid"
        ),
        "test_cases": [
            {"input": "({[]})\n", "expected_output": "valid", "description": "Correctly nested"},
            {"input": "([)]\n", "expected_output": "invalid", "description": "Crossed brackets"},
            {"input": "\n", "expected_output": "valid", "description": "Empty input"},
            {"input": "(((\n", "expected_output": "invalid", "description": "Unclosed"},
        ],
        "difficulty": "medium",
    },
]


def _fallback_coding_question(
    job: Optional[Job],
    candidate: Optional[Candidate],
    index: int = 0,
    manual_prompt: Optional[str] = None,
) -> dict:
    """Deterministic coding problem used when the AI fails to emit one.

    Problems read stdin and write stdout so they run across every Piston
    runtime. For multi-coding interviews (`coding_count > 1`), we rotate
    through the bank using `index` so the candidate gets different problems.

    When `manual_prompt` is supplied (recruiter-authored, manual coding
    questions mode), we wrap it in a minimal coding_question shape with the
    bank's generic test cases as a placeholder, so the candidate at least sees
    the recruiter's intended problem text. The test cases are best-effort —
    candidates can still write code; pass/fail comparison won't be meaningful
    unless the AI expansion path runs successfully.

    The `job` / `candidate` arguments are accepted for future personalisation
    (e.g. picking a problem category from required skills) but the current
    implementation is intentionally generic — this is the safety net, not the
    happy path.
    """
    bank = _FALLBACK_PROBLEMS
    picked = bank[index % len(bank)]

    if manual_prompt and manual_prompt.strip():
        return {
            "title": (manual_prompt.strip().splitlines()[0][:60] or "Coding Challenge"),
            "description": manual_prompt.strip(),
            "test_cases": picked["test_cases"],
            "time_limit": 15,
            "difficulty": picked.get("difficulty", "medium"),
        }

    return {
        "title": picked["title"],
        "description": picked["description"],
        "test_cases": picked["test_cases"],
        "time_limit": 15,
        "difficulty": picked.get("difficulty", "medium"),
    }


async def generate_evaluation(session: InterviewSession, db: Session) -> Evaluation:
    job = db.query(Job).filter(Job.id == session.job_id).first()
    candidate = db.query(Candidate).filter(Candidate.id == session.candidate_id).first() if session.candidate_id else None

    messages = (
        db.query(InterviewMessage)
        .filter(InterviewMessage.session_id == session.id)
        .order_by(InterviewMessage.created_at)
        .all()
    )

    # Build structured transcript with clear Q&A pairs
    # Group messages by question_index for accurate per-question evaluation
    from collections import OrderedDict
    qa_groups: OrderedDict[int, dict] = OrderedDict()
    for m in messages:
        if m.question_index is None:
            continue  # skip closing messages
        qi = m.question_index
        if qi not in qa_groups:
            qa_groups[qi] = {"questions": [], "answers": []}
        if m.role == "ai":
            qa_groups[qi]["questions"].append(m.content)
        else:
            qa_groups[qi]["answers"].append(m.content)

    # Build transcript — question_index=0 is intro (not evaluated), real questions start at 1
    # IMPORTANT: Use sequential numbering (1,2,3...) instead of raw question_index
    # to prevent evaluation index mismatches when AI skips indices
    transcript_parts = []
    eval_question_count = 0
    # Map from sequential number → original question_index (for DB cross-reference)
    seq_to_original: dict[int, int] = {}
    for qi, qa in qa_groups.items():
        q_text = " ".join(qa["questions"])
        a_text = " ".join(qa["answers"]) if qa["answers"] else "[No answer provided — skipped]"
        if qi == 0:
            transcript_parts.append(f"--- Introduction (not scored) ---\n[AI INTERVIEWER]: {q_text}\n[CANDIDATE]: {a_text}")
        else:
            eval_question_count += 1
            seq_to_original[eval_question_count] = qi
            transcript_parts.append(f"--- Question {eval_question_count} ---\n[AI INTERVIEWER]: {q_text}\n[CANDIDATE]: {a_text}")

    transcript = "\n\n".join(transcript_parts)

    # ── Coding submissions block ─────────────────────────────────────────────
    # When the candidate wrote code, surface it to the evaluator as a separate,
    # structured section. The transcript-only path was good for verbal scoring
    # but underweighted code quality. With this block we ask Opus for a
    # dedicated `coding_score` based on test pass rate, code clarity, and
    # approach — independent from the verbal `technical_score`.
    coding_rows = (
        db.query(CodingSubmission)
        .filter(CodingSubmission.session_id == session.id)
        .order_by(CodingSubmission.submitted_at.asc(), CodingSubmission.id.asc())
        .all()
    )
    coding_block = ""
    has_coding = bool(coding_rows)
    if has_coding:
        parts = ["", "═══ CODING SUBMISSIONS ═══"]
        for i, cs in enumerate(coding_rows, start=1):
            pass_ratio = (cs.test_cases_passed / cs.test_cases_total) if cs.test_cases_total else 0.0
            parts.append(
                f"\n--- Submission {i} ---\n"
                f"Title: {cs.title or '(untitled)'}\n"
                f"Difficulty: {cs.difficulty or 'unspecified'}\n"
                f"Language: {cs.language}\n"
                f"Test Cases: {cs.test_cases_passed}/{cs.test_cases_total} passed "
                f"({pass_ratio * 100:.0f}%)\n"
                f"Code:\n```{cs.language}\n{cs.code}\n```"
            )
        coding_block = "\n".join(parts)

    skills = ", ".join([s.skill for s in job.skills]) if job and job.skills else "general"

    # ── Resolve scoring leniency & custom instructions ──
    interview_settings = job.interview_settings if job else None
    scoring_mode = (interview_settings.scoring if interview_settings and interview_settings.scoring else None) or (job.evaluation_scoring if job else None) or "Standard Evaluation"

    # Determine if scoring field contains custom instructions (longer text) vs a mode label
    known_modes = {"Standard Evaluation", "Lenient", "Strict"}
    if scoring_mode in known_modes:
        custom_instructions = ""
    else:
        # Custom instructions were stored in the scoring field
        custom_instructions = scoring_mode
        scoring_mode = job.evaluation_scoring if job and job.evaluation_scoring in known_modes else "Standard Evaluation"

    scoring_guidance = {
        "Lenient": (
            "SCORING MODE: LENIENT\n"
            "- Be more forgiving in your evaluation.\n"
            "- Reward effort, intent, and partial correctness generously.\n"
            "- Do not heavily penalize minor mistakes, incomplete answers, or slight inaccuracies.\n"
            "- This is suitable for fresher or junior-level candidates.\n"
            "- Scores should trend higher — give credit for demonstrating understanding even if details are missing.\n"
            "- Recommendation thresholds: Strong Hire >= 70, Hire >= 55, Maybe >= 40, No Hire < 40"
        ),
        "Strict": (
            "SCORING MODE: STRICT\n"
            "- Apply a highly critical evaluation approach.\n"
            "- Require precise, complete, and well-structured answers.\n"
            "- Penalize missing details, vague explanations, and weak reasoning.\n"
            "- This is suitable for senior or expert-level roles.\n"
            "- Scores should be lower unless the answer is near-perfect.\n"
            "- Recommendation thresholds: Strong Hire >= 90, Hire >= 75, Maybe >= 60, No Hire < 60"
        ),
        "Standard Evaluation": (
            "SCORING MODE: STANDARD\n"
            "- Use a balanced evaluation approach.\n"
            "- Consider correctness, clarity, and overall understanding.\n"
            "- Allow minor mistakes but apply reasonable deductions.\n"
            "- Provide fair and moderate scoring.\n"
            "- Recommendation thresholds: Strong Hire >= 80, Hire >= 65, Maybe >= 50, No Hire < 50"
        ),
    }.get(scoring_mode, "SCORING MODE: STANDARD\n- Use a balanced evaluation approach.\n- Recommendation thresholds: Strong Hire >= 80, Hire >= 65, Maybe >= 50, No Hire < 50")

    custom_section = ""
    if custom_instructions:
        custom_section = f"\n\nADDITIONAL EVALUATION INSTRUCTIONS (from recruiter):\n{custom_instructions}\nIncorporate these instructions into your evaluation and adjust scoring focus accordingly."

    prompt = f"""You are a senior technical evaluator. Analyse the following interview transcript and produce a structured evaluation.

Position: {job.title if job else 'Unknown'}
Required Skills: {skills}
Candidate: {candidate.name if candidate else 'Unknown'}

{scoring_guidance}{custom_section}

TRANSCRIPT:
{transcript}{coding_block}

Evaluate across these PRIMARY dimensions (score 0-100):
1. technical_score: Depth and accuracy of technical knowledge (verbal answers)
2. communication_score: Clarity, articulation, and professionalism
3. problem_solving_score: Logical thinking and approach to problems
4. confidence_score: Confidence and composure under questioning
{f'5. coding_score: Quality of the code submissions in the CODING SUBMISSIONS block. Weight test pass rate heavily (60%), code clarity / structure (25%), and algorithmic approach (15%). Set to null if no submissions exist.' if has_coding else '5. coding_score: No coding submissions in this interview — set to null.'}

Also evaluate these ADDITIONAL dimensions (score 0-100):
5. pronunciation_score: Clarity of speech, proper word usage, articulation quality
6. enthusiasm_score: Level of interest, energy, and passion shown during the interview
7. grammar_score: Grammatical correctness and sentence structure quality
8. conciseness_score: Ability to give focused, to-the-point answers without rambling
9. clarity_score: Logical flow and ease of understanding in responses

IMPORTANT: You MUST strictly follow the scoring mode guidelines above. The same answer should produce higher scores in Lenient mode, moderate scores in Standard mode, and lower scores in Strict mode.

Then provide:
- overall_score: Weighted average (technical 40%, communication 25%, problem_solving 25%, confidence 10%)
- recommendation: One of "Strong Hire", "Hire", "Maybe", "No Hire" — use the thresholds specified in the scoring mode above
- summary: 2-3 sentence executive summary
- strengths: List of 3-5 specific strengths observed
- weaknesses: List of 2-4 specific areas for improvement
- detailed_feedback: 1-2 paragraphs of detailed assessment including how the scoring mode influenced the evaluation
- per_question_evaluations: The transcript has EXACTLY {eval_question_count} scored questions numbered sequentially as "--- Question 1 ---" through "--- Question {eval_question_count} ---". Do NOT evaluate the Introduction — it is not scored. You MUST return EXACTLY {eval_question_count} evaluations, one for each question. The "index" field MUST be the sequential number: 1, 2, 3, ..., {eval_question_count}. For EACH: index (integer 1-{eval_question_count}), score (0-100), list of positives, list of negatives. Evaluate each question in COMPLETE ISOLATION.
- skills_ratings: Rate the candidate on 3 key skill areas relevant to the position. For each skill provide: skill name, rating (1-5 stars), and a short evaluation paragraph

Return ONLY valid JSON:
{{
  "technical_score": 75,
  "communication_score": 80,
  "problem_solving_score": 70,
  "confidence_score": 85,
  "coding_score": {"72" if has_coding else "null"},
  "pronunciation_score": 75,
  "enthusiasm_score": 70,
  "grammar_score": 80,
  "conciseness_score": 72,
  "clarity_score": 78,
  "overall_score": 76.25,
  "recommendation": "Hire",
  "summary": "...",
  "strengths": ["...", "..."],
  "weaknesses": ["...", "..."],
  "detailed_feedback": "...",
  "per_question_evaluations": [
    {{"index": 1, "score": 65, "positives": ["Good point 1", "Good point 2"], "negatives": ["Weak area 1"]}},
    {{"index": 2, "score": 72, "positives": ["..."], "negatives": ["..."]}}
  ],
  "skills_ratings": [
    {{"skill": "Relevant Skill Name", "rating": 3, "evaluation": "Short evaluation paragraph..."}},
    {{"skill": "Another Skill", "rating": 4, "evaluation": "..."}}
  ]
}}"""

    logging.info(f"[Evaluation] Sending prompt to AI for session {session.id} (transcript length: {len(transcript)} chars)")
    try:
        llm_result = await _ask_with_system_usage(
            system="You are a senior technical evaluator. Return only valid JSON.",
            messages=[{"role": "user", "content": prompt}],
            model=OPUS,
            # Headroom for long interviews — a 20+ question evaluation (per-question
            # positives/negatives + scores + summary + skills) overflowed 4000 and
            # got truncated into unparseable JSON.
            max_tokens=8000,
            # 90s/attempt × outer 360s wrapper × 4 retries = up to ~6 min total
            # before we give up — fail fast so the candidate sees a real retry
            # button instead of staring at "Generating…" for 7+ minutes.
            timeout_seconds=90,
        )
    except Exception as e:
        logging.error(f"[Evaluation] AI call failed for session {session.id}: {type(e).__name__}: {e}\n{traceback.format_exc()}")
        raise
    log_usage(llm_result, feature="interview", session_id=session.id)
    logging.info(f"[Evaluation] AI responded for session {session.id}, parsing JSON...")
    try:
        data = _parse_json(llm_result.text)
    except Exception as e:
        # The evaluation JSON most often breaks on UNescaped double-quotes inside
        # feedback text (the model cites the candidate's words / terms like
        # "useState" in quotes). Rather than fail the whole evaluation, ask the
        # model to re-emit it as strictly-valid JSON. Truncation is already
        # prevented by the raised max_tokens above. This repair only runs on a
        # parse failure, so it adds no cost to the normal path.
        logging.warning(f"[Evaluation] JSON parse failed for session {session.id}: {e}. Attempting JSON repair…")
        try:
            repair = await _ask_with_system_usage(
                system=(
                    "You fix malformed JSON. Output ONLY a single, strictly-valid JSON object — "
                    "properly escape every inner double-quote and newline. No commentary, no markdown fences."
                ),
                messages=[{"role": "user", "content": f"Convert the following into one strictly-valid JSON object:\n\n{llm_result.text}"}],
                model=SONNET,
                max_tokens=8000,
                timeout_seconds=60,
            )
            data = _parse_json(repair.text)
            logging.info(f"[Evaluation] JSON repair succeeded for session {session.id}")
        except Exception as e2:
            logging.error(
                f"[Evaluation] JSON parse failed AND repair failed for session {session.id}: {e2}\n"
                f"Raw AI response: {llm_result.text[:500]}"
            )
            raise

    overall = float(data.get("overall_score", 0))
    rec_str = data.get("recommendation", "Maybe")

    rec_map = {
        "Strong Hire": Recommendation.strong_hire,
        "Hire": Recommendation.hire,
        "Maybe": Recommendation.maybe,
        "No Hire": Recommendation.no_hire,
    }
    recommendation = rec_map.get(rec_str, Recommendation.maybe)

    raw_coding_score = data.get("coding_score", None)
    coding_score: float | None
    if raw_coding_score is None or not has_coding:
        coding_score = None
    else:
        try:
            coding_score = float(raw_coding_score)
        except (TypeError, ValueError):
            coding_score = None

    evaluation = Evaluation(
        session_id=session.id,
        candidate_id=session.candidate_id,
        technical_score=float(data.get("technical_score", 0)),
        communication_score=float(data.get("communication_score", 0)),
        problem_solving_score=float(data.get("problem_solving_score", 0)),
        confidence_score=float(data.get("confidence_score", 0)),
        coding_score=coding_score,
        overall_score=overall,
        recommendation=recommendation,
        summary=data.get("summary", ""),
        strengths=json.dumps(data.get("strengths", [])),
        weaknesses=json.dumps(data.get("weaknesses", [])),
        detailed_feedback=data.get("detailed_feedback", ""),
        per_question_evaluations=json.dumps(data.get("per_question_evaluations", [])),
        skills_ratings=json.dumps(data.get("skills_ratings", [])),
        meets_threshold=overall >= {"Lenient": 55, "Strict": 75}.get(scoring_mode, 65),
    )
    db.add(evaluation)

    if candidate:
        candidate.score = overall
        candidate.confidence_score = float(data.get("confidence_score", 0))
        candidate.pronunciation_score = float(data.get("pronunciation_score", 0))
        candidate.enthusiasm_score = float(data.get("enthusiasm_score", 0))
        candidate.grammar_score = float(data.get("grammar_score", 0))
        candidate.conciseness_score = float(data.get("conciseness_score", 0))
        candidate.clarity_score = float(data.get("clarity_score", 0))
        violation_count = session.violation_count or 0
        candidate.trust_score = max(0.0, 100.0 - violation_count * 20.0)
        # AI cheating score: higher = more cheating detected (0-100 scale)
        candidate.ai_cheating_score = min(100.0, violation_count * 20.0)
        candidate.interview_status = "Completed"
        candidate.interview_date = session.started_at or datetime.utcnow()
        candidate.candidate_stage = "AI Round"

    db.commit()
    db.refresh(evaluation)
    return evaluation
