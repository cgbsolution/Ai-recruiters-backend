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
    Build the question plan based on job config.
    Returns (predefined_questions: list[str], ai_question_count: int, max_q: int, mode_str: str).
    """
    from app.models.job import MixOfQuestions

    max_q = settings_obj.max_questions if settings_obj else 8
    mix_mode = job.mix_of_questions if job.mix_of_questions else MixOfQuestions.mix

    predefined = [q.text for q in job.questions] if job.questions else []

    if mix_mode == MixOfQuestions.only and predefined:
        # ONLY predefined questions — override max_q to match
        return predefined, 0, len(predefined), "ONLY_PREDEFINED"
    elif predefined:
        # MIX mode — use predefined + fill remainder with AI
        ai_count = max(0, max_q - len(predefined))
        return predefined, ai_count, max_q, "MIX"
    else:
        # No predefined — all AI-generated
        return [], max_q, max_q, "ALL_AI"


def _build_system_prompt(job: Job, candidate: Candidate, settings_obj: Optional[InterviewSettings]) -> str:
    skills = ", ".join([s.skill for s in job.skills]) if job.skills else "general skills"
    mode = settings_obj.mode.value if settings_obj else "TRADITIONAL"
    special = settings_obj.special_instructions if settings_obj and settings_obj.special_instructions else "None"

    resume_based = settings_obj.resume_based if settings_obj else True

    predefined, ai_count, max_q, q_mode = _build_question_plan(job, settings_obj)

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
- Total questions to ask: {max_q}

PREDEFINED QUESTIONS (ask in this EXACT order):
{numbered}

FLOW:
1. Opening: Give intro + ask candidate to introduce themselves (question_index=0)
2. After self-intro: Acknowledge briefly, then ask Q1 as written (question_index=1)
3. For each subsequent question: Ask it as written, wait for answer, then ask next
4. After the candidate answers Q{max_q}, set is_complete to true

After asking ALL {max_q} questions and receiving answers, set is_complete to true.
"""
    elif q_mode == "MIX":
        numbered = "\n".join(f"  Q{i+1}: {q}" for i, q in enumerate(predefined))
        question_plan_section = f"""
═══════════════════════════════════════════════════════════════
QUESTION PLAN (MODE: MIX — Predefined + AI-Generated)
═══════════════════════════════════════════════════════════════

Total questions: {max_q}
  - Predefined: {len(predefined)} questions (must be asked FIRST in exact order)
  - AI-Generated: {ai_count} additional questions (after all predefined ones)

PHASE 1 — PREDEFINED QUESTIONS (Q1 to Q{len(predefined)}):
Ask these in EXACT order, EXACTLY as written. Do NOT rephrase or skip:
{numbered}

PHASE 2 — AI-GENERATED QUESTIONS (Q{len(predefined) + 1} to Q{max_q}):
Generate {ai_count} dynamic questions based on:
  - Job description and required skills: {", ".join([s.skill for s in job.skills] if job.skills else ["general skills"])}
  - Candidate's previous answers
  - Gaps in technical depth, problem-solving, behavioral, situational coverage

FLOW:
1. Opening: Give intro + ask candidate to introduce themselves (question_index=0)
2. After self-intro: Acknowledge briefly, then ask Q1 — the first PREDEFINED question (question_index=1)
3. Questions Q1–Q{len(predefined)}: Ask predefined questions exactly as written
4. Questions Q{len(predefined) + 1}–Q{max_q}: Generate and ask {ai_count} new questions
5. After Q{max_q} is answered: Set is_complete to true

CRITICAL: You MUST ask exactly {max_q} questions total. Stop and complete after Q{max_q}.
"""
    else:
        question_plan_section = f"""
═══════════════════════════════════════════════════════════════
QUESTION PLAN (MODE: ALL AI-GENERATED)
═══════════════════════════════════════════════════════════════

Generate ALL {max_q} questions dynamically. No predefined questions.
- Total questions: {max_q}
- Base on: job description, required skills ({skills}), and candidate's answers
- Coverage: technical depth, problem-solving, system design, behavioral, situational

FLOW:
1. Opening: Give intro + ask candidate to introduce themselves (question_index=0)
2. After self-intro: Acknowledge briefly, then generate and ask Q1 (question_index=1)
3. For each subsequent question: Based on candidate's answer, generate and ask Q2, Q3, ..., Q{max_q}
4. After Q{max_q} is answered: Set is_complete to true

CRITICAL: Ask exactly {max_q} questions total. Stop and complete after Q{max_q}.
"""

    return f"""You are Alex, a senior professional interviewer. You conduct interviews like a real human — natural, responsive, concise. You are on a live video call.

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

{"TRADITIONAL MODE: Ask direct questions focused on experience, knowledge, and problem-solving. Keep each question short and focused — one clear question at a time." if mode == "TRADITIONAL" else "SCENARIO-BASED MODE: Present real-world scenarios. Each question simulates challenges one might face on the job. Give the candidate time to think through their response."}

═══════════════════════════════════════════════════════════════
OPENING (2-STEP INTRO — then interview questions)
═══════════════════════════════════════════════════════════════

STEP 1 — First turn (no candidate message / "Please begin the interview"):
1. Give a SHORT 1-2 sentence welcome
2. Ask the candidate to briefly introduce themselves
3. Set question_index to 0 (this is the intro — NOT an interview question)
4. Set is_complete to false

Format: "Hi {candidate.name if candidate else 'there'}! I'm Alex, your AI interviewer for the {job.title} position. Before we begin, could you take a moment to introduce yourself?"

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

For technical roles, include 1-2 coding questions (around question_index 3-5).
Include a "coding_question" object in your JSON response.
Spoken message MUST be EXACTLY: "Please click on the top-left button to start the coding test."
Do NOT read the problem title, description, or any part of the coding question aloud — the candidate will see it in the code editor.
Do NOT say anything about the problem content. Only say the button instruction above.

coding_question format:
{{
  "title": "Problem Title",
  "description": "Full problem description with examples and constraints",
  "test_cases": [{{"input": "...", "expected_output": "...", "description": "..."}}],
  "time_limit": 15,
  "difficulty": "medium"
}}

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


def _build_contextual_fallback(
    candidate_message: Optional[str],
    consecutive_fallbacks: int,
    session: InterviewSession,
    job: Job,
    db: Session,
) -> str:
    """
    Generate a contextual, non-repetitive response when Claude API fails.
    Uses the candidate's actual message and interview context to produce
    something intelligent rather than a generic "repeat" request.
    """
    last_ai_msg = (
        db.query(InterviewMessage)
        .filter(InterviewMessage.session_id == session.id, InterviewMessage.role == "ai")
        .order_by(InterviewMessage.created_at.desc())
        .first()
    )
    last_question = last_ai_msg.content if last_ai_msg else ""
    skills = ", ".join([s.skill for s in job.skills]) if job and job.skills else "the role"

    if candidate_message and len(candidate_message.strip()) > 5:
        contextual_responses = [
            f"That's an interesting perspective. Let me ask you something related — can you tell me about your experience with {skills}?",
            f"Thank you for sharing that. Moving on, could you describe a challenging project you've worked on recently?",
            f"I appreciate your answer. Let's explore another area — how do you approach problem-solving when you encounter an unfamiliar technical challenge?",
            f"Good to know. Let me shift to a different topic — can you walk me through your experience with team collaboration and code reviews?",
            f"Thanks for that. Tell me, how do you stay updated with new technologies and best practices in your field?",
        ]
    elif candidate_message and len(candidate_message.strip()) <= 5:
        if consecutive_fallbacks == 0:
            return "I didn't quite catch that. Could you elaborate a bit more on your answer?"
        elif consecutive_fallbacks == 1:
            return "It seems there may be an audio issue. Could you please speak a bit more clearly and give me a detailed answer?"
        else:
            contextual_responses = [
                f"No worries, let's move on. Can you tell me about your experience with {skills}?",
                f"Let's try a different question. What's a technical challenge you've solved recently that you're proud of?",
                f"Let me ask something else. How do you handle debugging when you encounter a complex issue in production?",
                f"Let's switch topics. Can you describe your ideal development workflow and the tools you use daily?",
            ]
    else:
        return (
            f"Hello! I'm Alex, and I'll be conducting your interview today for the "
            f"{job.title} position. Thank you for joining us. "
            f"Let's start with a quick introduction — could you tell me about your background and experience?"
        )

    idx = consecutive_fallbacks % len(contextual_responses)
    return contextual_responses[idx]


async def conduct_turn(
    session: InterviewSession,
    candidate_message: Optional[str],
    db: Session,
) -> dict:
    job = db.query(Job).filter(Job.id == session.job_id).first()
    candidate = db.query(Candidate).filter(Candidate.id == session.candidate_id).first() if session.candidate_id else None
    interview_settings = job.interview_settings if job else None

    # ── Server-side guard: detect candidate requesting to end ──
    max_q = interview_settings.max_questions if interview_settings else 8
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
            closing = f"Thank you for your time{', ' + candidate.name if candidate else ''}. It was great talking with you. We'll be in touch soon!"
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
        closing = f"Thank you for your time{', ' + candidate.name if candidate else ''}. That concludes our interview. We'll be in touch soon."
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

    system_prompt = _build_system_prompt(job, candidate, interview_settings)

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
        llm_result = await _ask_with_system_usage(system_prompt, claude_messages, model=SONNET, max_tokens=512, timeout_seconds=25)
        log_usage(llm_result, feature="interview", session_id=session.id)
        raw = llm_result.text
        result = _parse_json(raw)
    except Exception as e:
        logger.warning(f"Claude attempt 1 failed for session {session.id}: {e}")

    # ── Attempt 2: Retry once with shorter timeout ──
    if result is None:
        try:
            llm_result = await _ask_with_system_usage(system_prompt, claude_messages, model=SONNET, max_tokens=256, timeout_seconds=20)
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

        # Try regex extraction of message field from anywhere in raw text
        if result is None:
            try:
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
        fallback_msg = _build_contextual_fallback(
            candidate_message, consecutive_fallbacks, session, job, db
        )
        q_index = session.current_question_index
        if consecutive_fallbacks >= 2 and candidate_message is not None:
            q_index = (session.current_question_index or 0) + 1
            session.current_question_index = q_index

        db.add(InterviewMessage(
            session_id=session.id,
            role="ai",
            content=fallback_msg,
            question_index=q_index,
        ))
        db.commit()
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
    question_index = result.get("question_index", session.current_question_index + 1)
    coding_question = result.get("coding_question")

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

    # If this is the closing message (is_complete=true), save with question_index=None
    # so it doesn't appear as a scored question in the evaluation
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

    db.commit()

    response = {
        "session_id": session.id,
        "message": message_text,
        "question_index": question_index,
        "is_complete": is_complete,
        "is_off_topic": is_off_topic,
        "role": "ai",
    }

    # Include coding question if AI decided to ask one
    if coding_question and isinstance(coding_question, dict):
        response["coding_question"] = {
            "title": coding_question.get("title", "Coding Challenge"),
            "description": coding_question.get("description", ""),
            "test_cases": coding_question.get("test_cases", []),
            "time_limit": coding_question.get("time_limit", 15),
            "difficulty": coding_question.get("difficulty", "medium"),
        }
        # Force the spoken message to the standard instruction — never read the coding problem aloud
        response["message"] = "Now let's test your coding skills. Please click the 'Switch to Coding Test' button at the top of your screen to open the code editor and complete the challenge."
        # Also update the DB message to match
        last_ai = db.query(InterviewMessage).filter(
            InterviewMessage.session_id == session.id,
            InterviewMessage.role == "ai",
        ).order_by(InterviewMessage.created_at.desc()).first()
        if last_ai:
            last_ai.content = response["message"]
            db.commit()

    return response


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
    transcript_parts = []
    eval_question_count = 0
    for qi, qa in qa_groups.items():
        q_text = " ".join(qa["questions"])
        a_text = " ".join(qa["answers"]) if qa["answers"] else "[No answer provided — skipped]"
        if qi == 0:
            # Intro exchange — include for context but mark as non-evaluated
            transcript_parts.append(f"--- Introduction (not scored) ---\n[AI INTERVIEWER]: {q_text}\n[CANDIDATE]: {a_text}")
        else:
            eval_question_count += 1
            transcript_parts.append(f"--- Question {qi} ---\n[AI INTERVIEWER]: {q_text}\n[CANDIDATE]: {a_text}")

    transcript = "\n\n".join(transcript_parts)

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
{transcript}

Evaluate across these PRIMARY dimensions (score 0-100):
1. technical_score: Depth and accuracy of technical knowledge
2. communication_score: Clarity, articulation, and professionalism
3. problem_solving_score: Logical thinking and approach to problems
4. confidence_score: Confidence and composure under questioning

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
- per_question_evaluations: The transcript has {eval_question_count} scored questions (numbered as "--- Question N ---"). Do NOT evaluate the Introduction — it is not scored. You MUST evaluate ALL {eval_question_count} questions — do NOT skip any. For EACH question, provide: index (the question number matching "--- Question N ---"), score (0-100), list of positives, and list of negatives. Evaluate each question in COMPLETE ISOLATION.
- skills_ratings: Rate the candidate on 3 key skill areas relevant to the position. For each skill provide: skill name, rating (1-5 stars), and a short evaluation paragraph

Return ONLY valid JSON:
{{
  "technical_score": 75,
  "communication_score": 80,
  "problem_solving_score": 70,
  "confidence_score": 85,
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
            max_tokens=4000,
            timeout_seconds=180,  # Opus with full transcript needs up to 2-3 minutes
        )
    except Exception as e:
        logging.error(f"[Evaluation] AI call failed for session {session.id}: {type(e).__name__}: {e}\n{traceback.format_exc()}")
        raise
    log_usage(llm_result, feature="interview", session_id=session.id)
    logging.info(f"[Evaluation] AI responded for session {session.id}, parsing JSON...")
    try:
        data = _parse_json(llm_result.text)
    except Exception as e:
        logging.error(f"[Evaluation] JSON parse failed for session {session.id}: {e}\nRaw AI response: {llm_result.text[:500]}")
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

    evaluation = Evaluation(
        session_id=session.id,
        candidate_id=session.candidate_id,
        technical_score=float(data.get("technical_score", 0)),
        communication_score=float(data.get("communication_score", 0)),
        problem_solving_score=float(data.get("problem_solving_score", 0)),
        confidence_score=float(data.get("confidence_score", 0)),
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
