import json
from typing import Any

from app.services.llm_provider import (
    ask_with_usage as _llm_ask_usage,
    ask_with_system_usage as _llm_ask_with_system_usage,
    ask as _llm_ask,
    ask_with_system as _llm_ask_with_system,
    get_model,
    LLMResult,
)
from app.services.credit_service import log_usage
from app.schemas.ai import (
    EvaluateCandidateRequest,
    EvaluateCandidateResponse,
    FitScoreRequest,
    FitScoreResponse,
    GenerateJDRequest,
    GenerateJDResponse,
    GenerateQuestionsRequest,
    GenerateQuestionsResponse,
    ParseResumeRequest,
    ParseResumeResponse,
    SuggestQuestionsRequest,
    SuggestQuestionsResponse,
)

# Backward-compatible constants — now tier names resolved by llm_provider
SONNET = "fast"
OPUS = "advanced"


# ── Backward-compatible wrappers (used by resume_service, avatar_interview_service) ─

async def _ask(prompt: str, model: str = SONNET, max_tokens: int = 1024, retries: int = 3) -> str:
    """Backward-compatible wrapper. `model` param is treated as tier name."""
    tier = "advanced" if model in ("advanced", "claude-opus-4-6", "gpt-4o", "gemini-2.5-pro") else "fast"
    return await _llm_ask(prompt, tier=tier, max_tokens=max_tokens, retries=retries)


async def _ask_with_usage(prompt: str, model: str = SONNET, max_tokens: int = 1024, retries: int = 3) -> LLMResult:
    """Like _ask but returns full LLMResult with token usage."""
    tier = "advanced" if model in ("advanced", "claude-opus-4-6", "gpt-4o", "gemini-2.5-pro") else "fast"
    return await _llm_ask_usage(prompt, tier=tier, max_tokens=max_tokens, retries=retries)


async def _ask_with_system(
    system: str,
    messages: list,
    model: str = SONNET,
    max_tokens: int = 512,
    timeout_seconds: int = 25,
    retries: int = 3,
) -> str:
    """Backward-compatible wrapper. `model` param is treated as tier name."""
    tier = "advanced" if model in ("advanced", "claude-opus-4-6", "gpt-4o", "gemini-2.5-pro") else "fast"
    return await _llm_ask_with_system(
        system, messages, tier=tier, max_tokens=max_tokens,
        timeout_seconds=timeout_seconds, retries=retries,
    )


async def _ask_with_system_usage(
    system: str,
    messages: list,
    model: str = SONNET,
    max_tokens: int = 512,
    timeout_seconds: int = 25,
    retries: int = 3,
) -> LLMResult:
    """Like _ask_with_system but returns full LLMResult with token usage."""
    tier = "advanced" if model in ("advanced", "claude-opus-4-6", "gpt-4o", "gemini-2.5-pro") else "fast"
    return await _llm_ask_with_system_usage(
        system, messages, tier=tier, max_tokens=max_tokens,
        timeout_seconds=timeout_seconds, retries=retries,
    )


def _parse_json(raw: str) -> Any:
    raw = (raw or "").strip()
    if raw.startswith("```"):
        lines = raw.split("\n")
        raw = "\n".join(lines[1:-1]) if lines[-1].strip() == "```" else "\n".join(lines[1:])
        raw = raw.strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass
    # Tolerant repair for common LLM JSON mistakes: surrounding prose and
    # unescaped newlines/tabs inside string values (which break strict parsing
    # and previously caused interview questions to be truncated). Valid JSON
    # never reaches this path. Unescaped inner QUOTES still can't be repaired
    # here — the caller's field-level salvage handles those.
    import re as _re
    m = _re.search(r'(\{.*\}|\[.*\])', raw, _re.DOTALL)
    if not m:
        raise json.JSONDecodeError("No JSON object found in model output", raw, 0)
    candidate = m.group(0)
    for variant in (
        candidate,
        candidate.replace("\r", " ").replace("\n", " ").replace("\t", " "),
    ):
        try:
            return json.loads(variant)
        except json.JSONDecodeError:
            continue
    raise json.JSONDecodeError("Unrepairable JSON in model output", candidate, 0)


async def generate_jd(req: GenerateJDRequest) -> GenerateJDResponse:
    exp_min = req.experience_min if req.experience_min is not None else 0
    exp_max = req.experience_max if req.experience_max is not None else 2

    prompt = f"""You are an expert HR professional and technical recruiter.
Analyse the following job input and generate a comprehensive, structured job description.
Return ONLY valid JSON — no markdown fences, no extra text.

--- INPUT ---
Job Title / Description: {req.title}
Department: {req.department or "infer from title"}
Experience Required: {exp_min}–{exp_max} years
Job Type: {req.job_type}
Location: {req.location or "not specified"}
--- END INPUT ---

Return JSON in EXACTLY this format (all fields required):
{{
  "title": "clean job title extracted or inferred from the input",
  "summary": "2-3 sentence role overview / purpose of the role",
  "experience_min": {exp_min},
  "experience_max": {exp_max},
  "responsibilities": [
    "responsibility 1",
    "responsibility 2",
    "responsibility 3",
    "responsibility 4",
    "responsibility 5"
  ],
  "skill_groups": [
    {{
      "name": "Descriptive group name (e.g. Core Frontend Skills)",
      "skills": ["Skill A", "Skill B", "Skill C"],
      "criteria": 1,
      "weight": 2.0
    }},
    {{
      "name": "Another group name",
      "skills": ["Skill D", "Skill E"],
      "criteria": 2,
      "weight": 1.5
    }},
    {{
      "name": "Tools & Utilities",
      "skills": ["Tool A", "Tool B"],
      "criteria": 3,
      "weight": 1.0
    }}
  ],
  "special_instructions": "Key things the interviewer should focus on or verify during the interview.",
  "coding_questions_count": 2,
  "business_context": "Why this role exists and its strategic importance, or 'No specific business context mentioned' if not clear."
}}

Criteria values:
  1 = All Mandatory (all skills in the group are required)
  2 = At Least One (candidate must have at least one skill from the group)
  3 = Good to have (nice to have, not mandatory)

Weight values:
  Use 2.0 for core/mandatory skill groups, 1.5 for important groups, 1.0 for optional groups.

Generate 2–4 skill groups that logically cluster the required skills.
Generate 4–6 responsibilities relevant to the role.
coding_questions_count must be at least 1 for technical roles."""

    result = await _ask_with_usage(prompt, model=SONNET, max_tokens=2048)
    log_usage(result, feature="jd_generation")
    data = _parse_json(result.text)
    return GenerateJDResponse(**data)


async def parse_jd_text(jd_text: str) -> GenerateJDResponse:
    """Parse raw JD text (extracted from an uploaded file) into structured data."""
    prompt = f"""You are an expert HR professional and technical recruiter.
Analyse the following job description text and extract structured data from it.
Return ONLY valid JSON — no markdown fences, no extra text.

--- JD TEXT ---
{jd_text[:8000]}
--- END JD TEXT ---

Return JSON in EXACTLY this format (all fields required):
{{
  "title": "the job title from the JD",
  "summary": "2-3 sentence role overview / purpose of the role",
  "experience_min": <integer, minimum years of experience required, default 0>,
  "experience_max": <integer, maximum years of experience, default 2>,
  "responsibilities": [
    "responsibility 1",
    "responsibility 2",
    "responsibility 3"
  ],
  "skill_groups": [
    {{
      "name": "Descriptive group name (e.g. Core Frontend Skills)",
      "skills": ["Skill A", "Skill B", "Skill C"],
      "criteria": 1,
      "weight": 2.0
    }},
    {{
      "name": "Another group name",
      "skills": ["Skill D", "Skill E"],
      "criteria": 2,
      "weight": 1.5
    }}
  ],
  "special_instructions": "Key things the interviewer should focus on or verify during the interview.",
  "coding_questions_count": 2,
  "business_context": "Why this role exists and its strategic importance, or 'No specific business context mentioned' if not clear."
}}

Criteria values:
  1 = All Mandatory (all skills in the group are required)
  2 = At Least One (candidate must have at least one skill from the group)
  3 = Good to have (nice to have, not mandatory)

Weight values:
  Use 2.0 for core/mandatory skill groups, 1.5 for important groups, 1.0 for optional groups.

Generate 2–4 skill groups that logically cluster the required skills.
Extract all responsibilities mentioned in the JD.
coding_questions_count must be at least 1 for technical roles."""

    result = await _ask_with_usage(prompt, model=SONNET, max_tokens=4096)
    log_usage(result, feature="jd_parsing")
    data = _parse_json(result.text)
    return GenerateJDResponse(**data)


async def generate_questions(req: GenerateQuestionsRequest) -> GenerateQuestionsResponse:
    skills_str = ", ".join(req.skills) if req.skills else "general"
    prompt = f"""You are a senior technical interviewer.
Generate {req.count} interview questions for a {req.job_title} ({req.department}) role.
Interview Mode: {req.mode}
Skills to cover: {skills_str}
Target difficulty: {req.difficulty}

Return ONLY a valid JSON array (no markdown fences):
[
  {{"text": "question text", "category": "skill category", "difficulty": "{req.difficulty}"}},
  ...
]"""
    result = await _ask_with_usage(prompt, model=SONNET, max_tokens=2048)
    log_usage(result, feature="question_generation")
    questions = _parse_json(result.text)
    return GenerateQuestionsResponse(questions=questions)


async def evaluate_answer(req: EvaluateCandidateRequest) -> EvaluateCandidateResponse:
    scoring_mode = req.scoring or "Standard Evaluation"
    scoring_instructions = {
        "Lenient": (
            "Scoring Mode: LENIENT — Be forgiving. Reward effort, intent, and partial correctness. "
            "Do not heavily penalize minor mistakes or incomplete answers. "
            "Scores should trend higher. Pass threshold: score >= 55."
        ),
        "Strict": (
            "Scoring Mode: STRICT — Be highly critical. Require precise, complete, and well-structured answers. "
            "Penalize missing details, vague explanations, and weak reasoning. "
            "Scores should be lower unless near-perfect. Pass threshold: score >= 75."
        ),
        "Standard Evaluation": (
            "Scoring Mode: STANDARD — Use balanced evaluation. Consider correctness, clarity, and understanding. "
            "Allow minor mistakes but apply reasonable deductions. Pass threshold: score >= 65."
        ),
    }.get(scoring_mode, "Scoring Mode: STANDARD — Use balanced evaluation. Pass threshold: score >= 65.")

    prompt = f"""You are an AI recruiter evaluating a candidate's interview answer.

Job: {req.job_title}
{scoring_instructions}
Question: {req.question}
Candidate Answer: {req.answer}

IMPORTANT: Strictly follow the scoring mode. The same answer must score higher in Lenient, moderate in Standard, and lower in Strict mode.

Evaluate the answer and return ONLY valid JSON (no markdown fences):
{{
  "score": <integer 0-100>,
  "feedback": "detailed evaluation feedback explaining strengths, weaknesses, and how the scoring mode influenced the score",
  "passed": <true|false based on the pass threshold above>
}}"""
    result = await _ask_with_usage(prompt, model=OPUS, max_tokens=512)
    log_usage(result, feature="answer_evaluation")
    data = _parse_json(result.text)
    return EvaluateCandidateResponse(
        score=data["score"],
        feedback=data["feedback"],
        passed=data["passed"],
    )


async def suggest_questions(req: SuggestQuestionsRequest) -> SuggestQuestionsResponse:
    prompt = f"""Suggest {req.count} interview questions for:
Category: {req.category}
Difficulty: {req.difficulty}

Return ONLY a valid JSON array of question strings (no markdown fences):
["question 1", "question 2", ...]"""
    result = await _ask_with_usage(prompt, model=SONNET, max_tokens=1024)
    log_usage(result, feature="question_suggestion")
    questions = _parse_json(result.text)
    return SuggestQuestionsResponse(questions=questions)


async def parse_resume(req: ParseResumeRequest) -> ParseResumeResponse:
    """Delegate to resume_service (keeps LLM helpers importable from ai_service)."""
    from app.services.resume_service import parse_resume as _parse
    return await _parse(req)


async def calculate_fit_score(req: FitScoreRequest) -> FitScoreResponse:
    """Delegate to resume_service (keeps LLM helpers importable from ai_service)."""
    from app.services.resume_service import calculate_fit_score as _calc
    return await _calc(req)
