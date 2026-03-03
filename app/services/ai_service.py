import json

import anthropic

from app.config import settings
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

SONNET = "claude-sonnet-4-6"
OPUS = "claude-opus-4-6"


def _client() -> anthropic.Anthropic:
    return anthropic.Anthropic(api_key=settings.ANTHROPIC_API_KEY)


def _ask(prompt: str, model: str = SONNET, max_tokens: int = 1024) -> str:
    client = _client()
    message = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        messages=[{"role": "user", "content": prompt}],
    )
    return message.content[0].text


def _parse_json(raw: str) -> any:
    raw = raw.strip()
    if raw.startswith("```"):
        lines = raw.split("\n")
        raw = "\n".join(lines[1:-1]) if lines[-1].strip() == "```" else "\n".join(lines[1:])
    return json.loads(raw)


def generate_jd(req: GenerateJDRequest) -> GenerateJDResponse:
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

    raw = _ask(prompt, model=SONNET, max_tokens=2048)
    data = _parse_json(raw)
    return GenerateJDResponse(**data)


def generate_questions(req: GenerateQuestionsRequest) -> GenerateQuestionsResponse:
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
    raw = _ask(prompt, model=SONNET, max_tokens=2048)
    questions = _parse_json(raw)
    return GenerateQuestionsResponse(questions=questions)


def evaluate_answer(req: EvaluateCandidateRequest) -> EvaluateCandidateResponse:
    prompt = f"""You are an AI recruiter evaluating a candidate's interview answer.

Job: {req.job_title}
Scoring Mode: {req.scoring}
Question: {req.question}
Candidate Answer: {req.answer}

Evaluate the answer and return ONLY valid JSON (no markdown fences):
{{
  "score": <integer 0-100>,
  "feedback": "detailed evaluation feedback",
  "passed": <true|false>
}}"""
    raw = _ask(prompt, model=OPUS, max_tokens=512)
    data = _parse_json(raw)
    return EvaluateCandidateResponse(
        score=data["score"],
        feedback=data["feedback"],
        passed=data["passed"],
    )


def suggest_questions(req: SuggestQuestionsRequest) -> SuggestQuestionsResponse:
    prompt = f"""Suggest {req.count} interview questions for:
Category: {req.category}
Difficulty: {req.difficulty}

Return ONLY a valid JSON array of question strings (no markdown fences):
["question 1", "question 2", ...]"""
    raw = _ask(prompt, model=SONNET, max_tokens=1024)
    questions = _parse_json(raw)
    return SuggestQuestionsResponse(questions=questions)


def parse_resume(req: ParseResumeRequest) -> ParseResumeResponse:
    """Delegate to resume_service (keeps LLM helpers importable from ai_service)."""
    from app.services.resume_service import parse_resume as _parse
    return _parse(req)


def calculate_fit_score(req: FitScoreRequest) -> FitScoreResponse:
    """Delegate to resume_service (keeps LLM helpers importable from ai_service)."""
    from app.services.resume_service import calculate_fit_score as _calc
    return _calc(req)
