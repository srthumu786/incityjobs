"""Labelled evaluation set for the CURRENT matching engine (matching.py).

Seven occupations, each with a job posting (title + department + requirements,
exactly as the app builds JobCriteria.match_text) and three resumes:
  short      - a 1-2 line qualified summary
  realistic  - a qualified, realistic one-page-style resume (~100-150 words)
  unqualified- a realistic resume from a different occupation
Used by tests/test_units.py (to pin current behaviour) and to produce
docs/MATCHING_ENGINE_ASSESSMENT.md. Not used by the app.
"""
from matching import MIN_SEMANTIC_SCORE, Phase4SemanticEngine

OCCUPATIONS = {
    "Software developer": {
        "job": ("Python Developer", "Technical Services",
                "Python, SQL databases, Docker containers, REST APIs, AWS cloud. 3+ years building backend services."),
        "short": "Python developer: SQL, Docker, REST APIs, AWS, 4 years backend services.",
        "realistic": (
            "Summary: Backend engineer with five years of experience designing and maintaining web services for a regional "
            "logistics company. Experience: Built and operated customer-facing APIs in Python using Flask and FastAPI; "
            "designed relational schemas in PostgreSQL and wrote complex SQL for reporting; containerized services with "
            "Docker and deployed them to AWS (ECS, RDS, S3); introduced automated testing and CI pipelines that cut release "
            "time in half; mentored two junior developers; participated in on-call rotation and incident reviews. "
            "Education: B.S. Computer Science. Skills: Python, SQL, PostgreSQL, Docker, AWS, REST, Git, Linux, pytest. "
            "Interests: open source contributions, chess, hiking."),
    },
    "Warehouse / forklift": {
        "job": ("Forklift Operator", "Logistics",
                "Forklift certification, 2+ years warehouse experience, inventory scanning, pallet loading, OSHA safety."),
        "short": "Certified forklift operator, 3 years warehouse experience, inventory scanning, pallet loading, OSHA trained.",
        "realistic": (
            "Professional summary: dependable warehouse associate with strong attendance and safety record. Experience: "
            "Distribution center, 2021 to present. Operate sit-down and stand-up forklifts and reach trucks; load and unload "
            "trailers; stage outbound pallets; cycle counts with RF scanners; maintain clean, organized aisles; train new "
            "associates on equipment safety; meet daily productivity targets. Previous: grocery stock associate, 2018 to 2021, "
            "shelf replenishment, receiving, customer service. Certifications: forklift operator certification, OSHA 10, CPR and "
            "First Aid. Education: high school diploma. References available on request."),
    },
    "CDL truck driver": {
        "job": ("CDL Class A Driver", "Logistics",
                "Class A commercial driver's license, clean driving record, DOT medical card, 1+ year tractor-trailer experience, ELD logs."),
        "short": "Class A CDL driver, clean record, DOT medical card, 3 years tractor-trailer, ELD logs.",
        "realistic": (
            "Over-the-road and regional driver with four years of accident-free experience. Hold Class A license with tanker "
            "and doubles endorsements and a current DOT medical certificate. Hauled dry van and refrigerated freight across "
            "eight southeastern states; average 2,300 miles per week; complete pre-trip and post-trip inspections; maintain "
            "hours-of-service compliance using electronic logging devices; coordinate appointments with dispatch and "
            "receivers; secure loads and document bills of lading. Safety award 2023. Comfortable with team and solo runs."),
    },
    "Registered nurse": {
        "job": ("Registered Nurse", "Administrative",
                "Active RN license, BLS and ACLS certification, 2+ years acute care, EHR charting, patient assessment."),
        "short": "Registered nurse, active RN license, BLS and ACLS, 3 years acute care, EHR charting, patient assessment.",
        "realistic": (
            "Compassionate nurse with three years on a busy medical-surgical floor. Responsibilities included head-to-toe "
            "assessments for five to six patients per shift, medication administration, wound care, IV starts, and "
            "coordinating discharges with case management. Documented care in Epic. Precepted new graduates and served on the "
            "unit practice council. Licensure: registered nurse, state board, active and unencumbered. Certifications: Basic "
            "Life Support and Advanced Cardiovascular Life Support. Education: Bachelor of Science in Nursing."),
    },
    "Retail cashier": {
        "job": ("Retail Cashier", "Retail/Fulfillment",
                "Cash handling, point of sale (POS) systems, customer service, stocking shelves, weekend availability."),
        "short": "Retail cashier: cash handling, POS systems, customer service, stocking shelves, available weekends.",
        "realistic": (
            "Friendly, reliable team member with two years of front-end retail experience at a busy pharmacy and a grocery "
            "store. Balanced registers at open and close with no discrepancies; processed returns and exchanges; greeted "
            "customers and resolved complaints; restocked and faced aisles; built seasonal displays; trained on the store's "
            "point-of-sale software and loyalty program. Flexible schedule including evenings, weekends and holidays. "
            "Bilingual English and Spanish. High school diploma."),
    },
    "Welder": {
        "job": ("Structural Welder", "Manufacturing",
                "MIG and TIG welding, blueprint reading, AWS D1.1 certification, fabrication, measuring tools, safety."),
        "short": "Welder: MIG, TIG, blueprint reading, AWS D1.1 certified, fabrication, measuring tools, shop safety.",
        "realistic": (
            "Fabrication welder with six years of experience in structural steel and heavy equipment repair. Perform GMAW, "
            "GTAW and stick welding in all positions on carbon and stainless steel; read and interpret shop drawings and "
            "weld symbols; set up fit-ups using squares, levels and tape measures; operate plasma cutters, grinders and "
            "overhead cranes; inspect welds for defects and rework as needed. Certified to AWS D1.1 structural code. "
            "Maintain a clean, safe work area and follow lockout/tagout procedures."),
    },
    "Administrative assistant": {
        "job": ("Administrative Assistant", "Administrative",
                "Microsoft Office, scheduling, data entry, phone etiquette, filing, invoicing, 2+ years office experience."),
        "short": "Administrative assistant: Microsoft Office, scheduling, data entry, phone etiquette, filing, invoicing.",
        "realistic": (
            "Organized office professional with four years supporting a busy dental practice and a small construction firm. "
            "Managed calendars for three providers, confirmed appointments and handled a multi-line phone system; prepared "
            "invoices and tracked payments in QuickBooks; maintained patient and vendor records; drafted correspondence in "
            "Word and built tracking sheets in Excel; ordered supplies and coordinated with vendors; greeted visitors. "
            "Known for accuracy, discretion and a calm, professional manner."),
    },
}

# For each occupation, the "unqualified" resume is the realistic resume of another occupation.
UNQUALIFIED_FROM = {
    "Software developer": "Warehouse / forklift",
    "Warehouse / forklift": "Registered nurse",
    "CDL truck driver": "Retail cashier",
    "Registered nurse": "Welder",
    "Retail cashier": "Software developer",
    "Welder": "Administrative assistant",
    "Administrative assistant": "CDL truck driver",
}


def job_text(occupation):
    title, department, requirements = OCCUPATIONS[occupation]["job"]
    return " ".join((title, department, requirements))  # same as JobCriteria.match_text


def evaluate():
    """Score every (occupation, resume kind) pair with the unchanged engine."""
    engine = Phase4SemanticEngine()
    rows = []
    for occupation, data in OCCUPATIONS.items():
        cases = {
            "short (qualified)": data["short"],
            "realistic (qualified)": data["realistic"],
            "unqualified": OCCUPATIONS[UNQUALIFIED_FROM[occupation]]["realistic"],
        }
        for kind, resume in cases.items():
            result = engine.calculate_match(resume, job_text(occupation))
            rows.append({
                "occupation": occupation,
                "resume": kind,
                "words": len(resume.split()),
                "score": result["semantic_percentage"],
                "passes": result["semantic_percentage"] >= MIN_SEMANTIC_SCORE,
                "should_pass": kind != "unqualified",
                "skills": result["matched_skills"],
            })
    return rows
