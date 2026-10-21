"""
Test interview flow and system prompt generation.
Verifies that:
1. Opening is 1-2 lines with immediate first question
2. Question modes (ONLY_PREDEFINED, MIX, ALL_AI) are handled correctly
3. Total questions match configured limit
"""

from app.models.job import Job, InterviewSettings, MixOfQuestions
from app.services.avatar_interview_service import _build_system_prompt, _build_question_plan
from unittest.mock import MagicMock


def test_build_question_plan_only_predefined():
    """Test ONLY_PREDEFINED mode with 3 predefined questions."""
    job = MagicMock()
    job.mix_of_questions = MixOfQuestions.only
    job.questions = [
        MagicMock(text="Q1: Tell me about your experience"),
        MagicMock(text="Q2: What is your strongest skill?"),
        MagicMock(text="Q3: Where do you see yourself in 5 years?"),
    ]

    settings = MagicMock()
    settings.max_questions = 7  # This should be overridden

    predefined, ai_count, max_q, mode = _build_question_plan(job, settings)

    assert mode == "ONLY_PREDEFINED"
    assert len(predefined) == 3
    assert ai_count == 0
    assert max_q == 3  # Should match predefined count, not max_questions
    print("✓ ONLY_PREDEFINED mode: Predefined count overrides max_questions")


def test_build_question_plan_mix():
    """Test MIX mode with 2 predefined and 5 AI-generated for total of 7."""
    job = MagicMock()
    job.mix_of_questions = MixOfQuestions.mix
    job.questions = [
        MagicMock(text="Q1: What is your experience?"),
        MagicMock(text="Q2: What's your strongest skill?"),
    ]

    settings = MagicMock()
    settings.max_questions = 7

    predefined, ai_count, max_q, mode = _build_question_plan(job, settings)

    assert mode == "MIX"
    assert len(predefined) == 2
    assert ai_count == 5  # 7 - 2
    assert max_q == 7  # Total
    print("✓ MIX mode: 2 predefined + 5 AI = 7 total questions")


def test_build_question_plan_all_ai():
    """Test ALL_AI mode with no predefined questions."""
    job = MagicMock()
    job.mix_of_questions = None  # Default mode
    job.questions = None  # No predefined questions

    settings = MagicMock()
    settings.max_questions = 8

    predefined, ai_count, max_q, mode = _build_question_plan(job, settings)

    assert mode == "ALL_AI"
    assert len(predefined) == 0
    assert ai_count == 8  # All generated
    assert max_q == 8
    print("✓ ALL_AI mode: All 8 questions generated dynamically")


def test_system_prompt_includes_opening_instruction():
    """Verify system prompt has clear opening instructions."""
    job = MagicMock()
    job.title = "Backend Developer"
    job.department = "Engineering"
    job.location = "Remote"
    job.experience_min = 3
    job.experience_max = 5
    job.skills = [MagicMock(skill="Python"), MagicMock(skill="FastAPI")]
    job.mix_of_questions = None  # Default = ALL_AI
    job.questions = None
    job.interview_settings = None

    candidate = MagicMock()
    candidate.name = "John"

    settings = MagicMock()
    settings.mode = MagicMock(value="TRADITIONAL")
    settings.special_instructions = None
    settings.max_questions = 7
    settings.resume_based = True
    settings.code_editor_required = False  # verbal interview; coding has its own tests
    settings.manual_coding_questions = False

    prompt = _build_system_prompt(job, candidate, settings)

    # The opening is two steps: a short welcome that asks the candidate to
    # introduce themselves (question 0), then the first real question (question 1).
    assert "OPENING" in prompt
    assert "introduce themselves" in prompt
    assert "Set question_index to 0" in prompt
    assert "Set question_index to 1" in prompt
    assert "Do NOT ask any interview question" in prompt
    assert "Hi John! I'm Nira" in prompt
    print("✓ System prompt includes clear opening instructions")

    # Verify mode instructions are present
    assert "ALL_AI" in prompt or "Generate" in prompt
    print("✓ System prompt includes mode-specific instructions")


def test_system_prompt_mix_mode_sections():
    """Verify MIX mode has clear predefined and AI-generated sections."""
    job = MagicMock()
    job.title = "Backend Developer"
    job.department = "Engineering"
    job.location = "Remote"
    job.experience_min = 3
    job.experience_max = 5
    job.skills = [MagicMock(skill="Python")]
    job.mix_of_questions = MixOfQuestions.mix
    job.questions = [
        MagicMock(text="Tell me about your experience"),
        MagicMock(text="What's your strongest skill?"),
    ]
    job.interview_settings = None

    candidate = MagicMock()
    candidate.name = "Jane"

    settings = MagicMock()
    settings.mode = MagicMock(value="TRADITIONAL")
    settings.special_instructions = None
    settings.max_questions = 7
    settings.resume_based = True
    settings.code_editor_required = False  # verbal interview; coding has its own tests
    settings.manual_coding_questions = False

    prompt = _build_system_prompt(job, candidate, settings)

    # Verify MIX mode sections
    assert "MODE: MIX" in prompt or "PHASE 1" in prompt or "PHASE 2" in prompt
    assert "PREDEFINED" in prompt
    assert "AI-GENERATED" in prompt
    assert "Predefined: 2" in prompt or "predefined: 2" in prompt or "2 questions" in prompt
    assert "5" in prompt  # 7 - 2 = 5 AI questions
    print("✓ MIX mode prompt has clear predefined and AI-generated sections")


def test_system_prompt_question_counting_rules():
    """Verify system prompt has detailed question counting rules."""
    job = MagicMock()
    job.title = "Engineer"
    job.department = "Tech"
    job.location = "Remote"
    job.experience_min = 2
    job.experience_max = 4
    job.skills = []
    job.mix_of_questions = None
    job.questions = None
    job.interview_settings = None

    candidate = None

    settings = MagicMock()
    settings.mode = MagicMock(value="TRADITIONAL")
    settings.special_instructions = None
    settings.max_questions = 7
    settings.resume_based = True
    settings.code_editor_required = False  # verbal interview; coding has its own tests
    settings.manual_coding_questions = False

    prompt = _build_system_prompt(job, candidate, settings)

    # Verify question counting rules
    assert "Set question_index to 0 (this is the intro" in prompt  # the intro is not counted
    assert "NEVER exceeds" in prompt or "NEVER exceed" in prompt
    assert "Increment by 1" in prompt or "Increment" in prompt
    assert "FLOW EXAMPLE" in prompt
    print("✓ System prompt includes detailed question counting rules")


if __name__ == "__main__":
    test_build_question_plan_only_predefined()
    test_build_question_plan_mix()
    test_build_question_plan_all_ai()
    test_system_prompt_includes_opening_instruction()
    test_system_prompt_mix_mode_sections()
    test_system_prompt_question_counting_rules()
    print("\n✓ All interview flow tests passed!")
