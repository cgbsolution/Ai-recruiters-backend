"""Fast (regex) parser: role lines with dates in brackets (found by the local
end-to-end run: "Acme Analytics Pvt Ltd (Jan 2021 - Present)" gave the company
"Acme Analytics Pvt Ltd (", shown to candidates on their refresh page)."""
from app.services.fast_resume_parser import extract_work_experience

CV = """EXPERIENCE
Lead Data Engineer, Acme Analytics Pvt Ltd (Jan 2021 - Present)
- Built pipelines
Software Engineer, Infosys Limited [Jun 2018 - Dec 2020]
- Delivered projects
(Mar 2016 - May 2018) Analyst at Wipro
"""


def test_brackets_around_dates_stay_off_the_title_and_company():
    roles = [(w["title"], w["company"]) for w in extract_work_experience(CV, CV)]
    assert roles == [("Lead Data Engineer", "Acme Analytics Pvt Ltd"), ("Software Engineer", "Infosys Limited"),
                     ("Analyst", "Wipro")]
