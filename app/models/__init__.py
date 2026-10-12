from app.models.organization import Organization
from app.models.user import User, UserRole
from app.models.job import (
    Job,
    JobResponsibility,
    JobSkill,
    InterviewSettings,
    JobQuestion,
)
from app.models.question import Question
from app.models.candidate import Candidate
from app.models.interview import (
    InterviewToken,
    InterviewSession,
    InterviewMessage,
    Violation,
    Evaluation,
    CandidateFeedback,
    MeetingSlot,
    MeetingSchedule,
)
from app.models.resume_metadata import ResumeMetadata
from app.models.job_metadata import JobMetadata
from app.models.ai_credit_usage import AiCreditUsage
from app.models.secondary_cam import SecondaryCamSession, SecondaryCamSnapshot
from app.models.observer import ObserverToken
from app.models.client_event import ClientEvent
from app.models.talent import (
    AccessToken,
    AgentHeartbeat,
    AuditLog,
    Campaign,
    CampaignMember,
    ContactConsent,
    CvVersion,
    DeliverabilityEvent,
    InterviewBooking,
    JobMatch,
    SendCounter,
    SenderDomain,
    SuppressionEntry,
    TalentApproval,
    TalentBatchRun,
    TalentInterview,
    TalentJobSettings,
    TalentSettings,
    Touch,
)

# File cleanup when a talent-pool profile is deleted by any path (needs the models above).
import app.talent.retention  # noqa: E402,F401
# Job candidates follow their profile's current resume when it is replaced.
import app.services.resume_links  # noqa: E402,F401
