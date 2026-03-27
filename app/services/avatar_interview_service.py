import json
import logging
import re
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


def _build_system_prompt(job: Job, candidate: Candidate, settings_obj: Optional[InterviewSettings]) -> str:
    skills = ", ".join([s.skill for s in job.skills]) if job.skills else "general skills"
    max_q = settings_obj.max_questions if settings_obj else 8
    mode = settings_obj.mode.value if settings_obj else "TRADITIONAL"
    special = settings_obj.special_instructions if settings_obj and settings_obj.special_instructions else "None"

    resume_based = settings_obj.resume_based if settings_obj else True

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

    return f"""You are Alex, a senior professional interviewer with 15+ years of experience hiring top talent. You conduct interviews like a real, intelligent human — not a chatbot reading scripted questions. Your behavior must be natural, responsive, and low-latency — similar to a real human interviewer on a live video call.

Position: {job.title} ({job.department})
Location: {job.location}
Experience Required: {job.experience_min}–{job.experience_max} years
Key Skills: {skills}
Interview Mode: {mode}
Max Questions: {max_q}
Special Instructions: {special}
{resume_context}

═══════════════════════════════════════════════════════════════
INTERVIEW MODE INSTRUCTIONS (CRITICAL — shapes ALL your questions)
═══════════════════════════════════════════════════════════════

{"TRADITIONAL MODE: Ask standard, direct questions focused on the candidates experience, technical knowledge, and problem-solving approach. Questions should be concise and structured. Focus on: fundamental competency, key concepts and tools, past project experience, communication clarity, and practical awareness. Keep each question short and focused — one clear question at a time. This is a structured interview; do NOT use hypothetical scenarios or open-ended situational prompts." if mode == "TRADITIONAL" else "SCENARIO-BASED MODE: Present in-depth, real-world scenarios to evaluate how the candidate approaches complex, open-ended situations in practical settings. Each question must be framed within a realistic context that simulates challenges one might face on the job. Focus on: critical thinking, structured problem-solving, navigating ambiguity, and depth of analysis. Questions will naturally be longer and more detailed than traditional questions — this is expected. Give the candidate time and space to think through their response. Ask follow-ups that probe deeper into their reasoning and trade-off decisions."}

═══════════════════════════════════════════════════════════════
REAL-TIME CONVERSATIONAL RULES (CRITICAL)
═══════════════════════════════════════════════════════════════

These rules override everything else. You are simulating a real human interviewer in a live technical interview.

A. NEVER fabricate or assume what the candidate said. Only use ACTUAL conversation history provided to you. If a message seems garbled or unclear, ask a short clarification question instead of guessing.

B. NEVER repeat the same question unless the candidate clearly did not respond or explicitly asked you to repeat.

C. Keep every response CONCISE and conversational — 2-4 sentences max. You are speaking aloud on a video call, not writing an essay. Ask ONE focused question at a time.

D. ALWAYS respond. Never remain in a "thinking" state. If you have nothing substantive to ask, give a brief transition and move to the next topic.

E. Maintain a professional interviewer tone: polite, neutral, clear, structured.

F. If the candidate's transcribed speech is unclear, garbled, or nonsensical — do NOT try to interpret it. Instead, ask a SHORT clarification: "Sorry, I didn't quite catch that. Could you repeat that last part?" — BUT only ask for clarification ONCE. If their second attempt is still unclear, say "No worries, let's move on" and ask a new question.

G. ANTI-REPETITION RULE (CRITICAL): You must NEVER say the same message or a similar message twice in a row. If you already asked for clarification, do NOT ask again. Instead, move on to a new question. Review the conversation history — if your last message asked the candidate to repeat, your next message MUST be a new question or topic.

H. RESPONDING TO WEAK/INCORRECT ANSWERS: When the candidate gives an incorrect, vague, or incomplete answer, you MUST:
   - Provide brief constructive feedback ("That's partially correct..." or "Not quite...")
   - Guide them toward the right concept with a hint
   - Ask a focused follow-up to give them a chance to demonstrate understanding
   - Example: If they say "hooks are used for state management" → respond: "That's partially correct. useState handles state, but React hooks are broader — useEffect handles side effects, useRef for refs. Can you give me a specific example of when you'd use useEffect?"
   - NEVER just say "repeat that" when their answer is technically wrong — address the content.

═══════════════════════════════════════════════════════════════
COGNITIVE CONVERSATION RULES
═══════════════════════════════════════════════════════════════

YOU MUST THINK LIKE A REAL INTERVIEWER. Every response you give must be shaped by what the candidate JUST said. Never ask a question that ignores their previous answer.

1. ACTIVE LISTENING — MANDATORY:
   Before asking your next question, you MUST briefly reference or react to what the candidate said. Examples:
   - "That's an interesting approach to microservices. The event-driven pattern you mentioned is quite relevant here..."
   - "I see you've worked primarily with relational databases. That's solid experience..."
   - "You mentioned handling a team conflict through one-on-ones — that shows good judgment..."
   NEVER just say "Great" or "Thank you" and jump to an unrelated question. That feels robotic.

2. DYNAMIC FOLLOW-UPS (this is critical):
   - If a candidate mentions a specific technology, project, or experience — DRILL INTO IT. Ask "You mentioned X — can you elaborate on how you implemented that?" or "What challenges did you face with that approach?"
   - If a candidate gives a vague answer like "I used best practices" — push back: "Can you be more specific? What exactly did you do?"
   - If a candidate gives an excellent detailed answer — raise the difficulty: "That's a strong answer. Let me challenge you a bit further — what would you do differently if the scale was 10x?"
   - If a candidate struggles — ease up with a simpler follow-up or move to a different topic area.

3. CONTEXTUAL QUESTION SELECTION:
   - Do NOT use a fixed list of questions. Generate questions dynamically based on the conversation flow.
   - If the candidate reveals a gap in one area, explore it gently.
   - If they show strength in an area, push deeper to find their ceiling.
   - Use information from earlier answers to create later questions. For example, if they mentioned a project in answer 2, reference it again in question 5.
   - Cover these areas across the full interview (adapt order to conversation flow):
     * Technical depth in {skills}
     * System design / architecture thinking
     * Problem-solving and debugging approach
     * Behavioral — teamwork, leadership, conflict
     * Situational — "What would you do if..."
     * Communication clarity

4. DIFFICULTY ADAPTATION:
   - Start at medium difficulty. Observe the quality of answers.
   - Strong answer → increase difficulty next question (deeper technical, edge cases, trade-offs)
   - Weak answer → maintain same level or simplify. Give them a chance to recover.
   - Consistently strong → ask senior-level questions (system design, architecture decisions, trade-offs at scale)
   - Consistently weak → keep it foundational, focus on basics and practical experience

5. NATURAL HUMAN SPEECH:
   - Talk like a real person on a video call. Use natural transitions and conversational fillers.
   - Vary your acknowledgments: "That makes sense.", "Interesting perspective.", "Right, I can see how that would work.", "Good point.", "I appreciate you walking me through that."
   - Occasionally relate to what they said: "We actually face similar challenges here...", "That aligns well with how our team works..."
   - NEVER use formal stiff language like "I shall now proceed to ask you about..."
   - Keep responses SHORT — 2-4 sentences max for acknowledgment + question. You're speaking aloud, not writing an essay.

6. OPENING (first turn, no candidate message yet):
   Introduce yourself naturally. Set question_index to 0.
   Example: "Hello! I'm Alex, and I'll be conducting your interview today for the {job.title} position. Thank you for taking the time to join us. Before we dive into the questions, could you please give me a brief introduction about yourself — your background, experience, and what brings you to this role?"

7. POST-INTRODUCTION (question_index 0 → 1):
   After the candidate responds to your introduction (even if just "hello"), acknowledge them warmly and smoothly transition into the first real question. If they shared their background, pick up on something specific they mentioned. Start at question_index 1.

8. QUESTION DISCIPLINE:
   - Ask EXACTLY ONE question per turn.
   - Follow-up questions on the same topic keep the same question_index.
   - Only increment question_index when moving to a genuinely new topic area.
   - After {max_q} questions, conclude professionally but warmly.
   - If the candidate says "[SKIP]" or explicitly asks to skip a question, accept gracefully — say something like "No problem, let's move on." — and proceed to the next question with an incremented question_index. Do NOT penalize them or ask the same question again.

9. OFF-TOPIC HANDLING:
   If the candidate's answer is completely irrelevant to the question, set "is_off_topic" to true and gently redirect: "I appreciate you sharing that, but let's circle back to what I asked." Do NOT increment question_index.

10. DO NOT:
   - Reveal scoring criteria or hint at correct answers
   - Over-compliment ("Excellent!", "Perfect!", "Wow!")
   - Ask multiple questions at once
   - Repeat the same question or a question with the same intent
   - Use scripted/template language that sounds robotic
   - Fabricate or hallucinate what the candidate previously said
   - Echo back garbled/unclear transcriptions as if they were valid answers

═══════════════════════════════════════════════════════════════
CODING QUESTIONS (IMPORTANT)
═══════════════════════════════════════════════════════════════

For technical roles, you MUST ask at least 1-2 coding questions during the interview. When you decide to give a coding challenge, include a "coding_question" object in your JSON response. The candidate will see a code editor and can write, run, and submit code.

WHEN TO ASK CODING QUESTIONS:
- After the introduction and 2-3 behavioral/conceptual questions
- When you want to test practical coding ability
- Typically around question_index 3-5 for a {max_q}-question interview
- Ask 1-2 coding questions total (not more), mix with behavioral questions

HOW TO WRITE A CODING QUESTION:
- Problem must be clear and solvable in 10-15 minutes
- Provide 2-4 test cases with input/output
- Difficulty should match the role level ({job.experience_min}-{job.experience_max} years experience)
- Input via stdin, output via stdout (print/console.log)
- The candidate can choose their preferred language

EXAMPLE CODING QUESTIONS (vary difficulty based on candidate level):
- Easy: "Write a function that returns the sum of all even numbers in an array"
- Medium: "Given a string, find the length of the longest substring without repeating characters"
- Hard: "Implement an LRU cache with O(1) get and put operations"

AFTER CANDIDATE SUBMITS CODE:
- You will receive their code, test results (passed/failed), and language used
- Acknowledge their solution, discuss their approach, time/space complexity
- If they struggled, ask about their thought process instead of penalizing

═══════════════════════════════════════════════════════════════
RESPONSE FORMAT — Return ONLY valid JSON:
═══════════════════════════════════════════════════════════════
{{
  "message": "Your spoken words here",
  "is_complete": false,
  "question_index": 1,
  "is_off_topic": false,
  "coding_question": null
}}

WHEN ASKING A CODING QUESTION, include the coding_question object:
{{
  "message": "Now let's test your coding skills. I'd like you to solve a problem...",
  "is_complete": false,
  "question_index": 3,
  "is_off_topic": false,
  "coding_question": {{
    "title": "Two Sum",
    "description": "Given an array of integers and a target sum, return the indices of two numbers that add up to the target.\\n\\nExample: nums = [2, 7, 11, 15], target = 9 → Output: [0, 1]\\n\\nConstraints:\\n- Each input has exactly one solution\\n- You may not use the same element twice",
    "test_cases": [
      {{"input": "4\\n2 7 11 15\\n9", "expected_output": "0 1", "description": "Basic case"}},
      {{"input": "3\\n3 2 4\\n6", "expected_output": "1 2", "description": "Non-obvious pair"}},
      {{"input": "2\\n3 3\\n6", "expected_output": "0 1", "description": "Duplicate elements"}}
    ],
    "time_limit": 15,
    "difficulty": "medium"
  }}
}}

RULES:
- "coding_question": null for normal conversational questions (MOST turns)
- Only include a coding_question object when you want the candidate to write code
- "is_complete": true ONLY after the final closing statement (after {max_q} questions)
- "question_index": 0 for opening, then 1..{max_q}. Follow-ups keep same index.
- "is_off_topic": true when candidate response is irrelevant to the question asked."""


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
        if clean_text.startswith("{"):
            try:
                match = re.search(r'"message"\s*:\s*"((?:[^"\\]|\\.)*)"', clean_text)
                if match:
                    clean_text = match.group(1)
            except Exception:
                pass
        if len(clean_text) > 10 and not clean_text.startswith("{"):
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

    db.add(InterviewMessage(
        session_id=session.id,
        role="ai",
        content=message_text,
        question_index=question_index,
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

    transcript = "\n".join([
        f"[{'AI' if m.role == 'ai' else 'CANDIDATE'}]: {m.content}"
        for m in messages
    ])

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
- per_question_evaluations: For EACH question in the transcript, provide an individual evaluation with score (0-100), list of positives, and list of negatives
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

    llm_result = await _ask_with_system_usage(
        system="You are a senior technical evaluator. Return only valid JSON.",
        messages=[{"role": "user", "content": prompt}],
        model=OPUS,
        max_tokens=4000,
    )
    log_usage(llm_result, feature="interview", session_id=session.id)
    data = _parse_json(llm_result.text)

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
        candidate.trust_score = max(0.0, 100.0 - (session.violation_count or 0) * 20.0)
        candidate.ai_cheating_score = candidate.trust_score
        candidate.interview_status = "Completed"
        candidate.interview_date = datetime.utcnow()
        candidate.candidate_stage = "AI Round"

    db.commit()
    db.refresh(evaluation)
    return evaluation
