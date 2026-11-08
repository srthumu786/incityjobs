"""Candidate-to-job matching rules for InCityJobs (no Streamlit; unit-testable).

The live pipeline matches candidates against a real posted job (JobCriteria
built from employer_jobs + beta_employers). The Manager Console's simulation
sandbox builds a JobCriteria from demo values and never writes anything.
"""
import math
import re
from dataclasses import dataclass, field

MIN_SEMANTIC_SCORE = 50.0  # percent; matches below this are gated


class Phase4SemanticEngine:
    """Pillars 4.1, 4.2 & 4.3: Local Natural Language Parsing & Cosine Math Matrix."""

    def __init__(self):
        # Local keyword vocabulary tracking
        self.skill_keywords = [
            "python", "javascript", "typescript", "react", "node", "sql",
            "aws", "docker", "kubernetes", "machine learning", "nlp"
        ]
        # Common structural noise words to exclude from mathematical matrix
        self.stopwords = {"a", "an", "the", "and", "or", "of", "at", "by", "for", "with", "in", "to", "is"}

    def clean_text(self, text: str) -> str:
        """Strips noise, lowercases everything, and collapses white spaces."""
        if not text:
            return ""
        cleaned = re.sub(r"[^\w\s\-\.]", " ", text)
        return " ".join(cleaned.lower().split())

    def parse_profile(self, text: str) -> dict:
        """Extracts text structures and cross-references explicitly targeted skill metrics."""
        cleaned = self.clean_text(text)
        detected_skills = [
            s for s in self.skill_keywords if re.search(rf"\b{re.escape(s)}\b", cleaned)
        ]
        return {"cleaned_text": cleaned, "skills": detected_skills}

    def calculate_match(self, resume_text: str, job_text: str) -> dict:
        """Runs Pillar 4.3 geometric cosine vector similarity entirely in system memory."""
        res = self.parse_profile(resume_text)
        job = self.parse_profile(job_text)

        res_tokens = [w for w in res["cleaned_text"].split() if w not in self.stopwords]
        job_tokens = [w for w in job["cleaned_text"].split() if w not in self.stopwords]

        vocabulary = sorted(set(res_tokens + job_tokens))
        if not vocabulary:
            return {"semantic_percentage": 0.0, "matched_skills": []}

        res_vec = [float(res_tokens.count(word)) for word in vocabulary]
        job_vec = [float(job_tokens.count(word)) for word in vocabulary]

        dot_product = sum(a * b for a, b in zip(res_vec, job_vec))
        mag_a = math.sqrt(sum(a * a for a in res_vec))
        mag_b = math.sqrt(sum(b * b for b in job_vec))
        semantic_score = (dot_product / (mag_a * mag_b)) if mag_a and mag_b else 0.0

        return {
            "semantic_percentage": round(semantic_score * 100, 2),
            "matched_skills": sorted(set(res["skills"]).intersection(job["skills"])),
        }


@dataclass(frozen=True)
class JobCriteria:
    """Structured matching parameters for one job."""
    job_id: object            # int for a real job, None for the simulation sandbox
    title: str
    requirements: str
    latitude: object
    longitude: object
    radius_miles: float
    department: str = ""
    employer_id: object = None
    employer_email: object = None
    company_name: object = None
    min_semantic_score: float = MIN_SEMANTIC_SCORE

    @property
    def match_text(self) -> str:
        return " ".join(part for part in (self.title, self.department, self.requirements) if part)

    @property
    def has_coordinates(self) -> bool:
        return self.latitude is not None and self.longitude is not None


@dataclass(frozen=True)
class CandidateRecord:
    profile_id: int
    name: str
    contact_email: str
    role: str
    resume: str
    latitude: object
    longitude: object


@dataclass
class MatchResult:
    candidate: CandidateRecord
    passed: bool
    reason: str
    semantic_percentage: float
    distance_miles: object = None
    matched_skills: list = field(default_factory=list)


def geodesic_miles(a, b) -> float:
    from geopy.distance import geodesic
    return geodesic(a, b).miles


def evaluate_match(job: JobCriteria, candidate: CandidateRecord, engine=None, distance_fn=geodesic_miles) -> MatchResult:
    """Apply the matching rules: contact email present, both locations known,
    within the job's radius, and semantic score at or above the job's minimum."""
    engine = engine or Phase4SemanticEngine()
    analysis = engine.calculate_match(candidate.resume or "", job.match_text)
    score = analysis["semantic_percentage"]
    skills = list(analysis["matched_skills"])

    def result(passed, reason, distance=None):
        return MatchResult(candidate, passed, reason, score, distance, skills)

    if not (candidate.contact_email or "").strip():
        return result(False, "Missing candidate contact email")
    if candidate.latitude is None or candidate.longitude is None:
        return result(False, "Candidate location unknown")
    if not job.has_coordinates:
        return result(False, "Job location unknown")

    distance = round(distance_fn((candidate.latitude, candidate.longitude), (job.latitude, job.longitude)), 1)
    if distance > job.radius_miles:
        return result(False, f"Out of range: {distance} mi (limit {job.radius_miles} mi)", distance)
    if score < job.min_semantic_score:
        return result(False, f"Semantic score {score}% below {job.min_semantic_score}%", distance)
    return result(True, "Passed radius and semantic thresholds", distance)


def build_push_payload(job: JobCriteria, result: MatchResult, logged_at: str) -> dict:
    """Everything the upcoming 3-Way Candidate Push Engine needs. Nothing is sent yet."""
    c = result.candidate
    return {
        "engine": "3-way-candidate-push",
        "delivery_status": "NOT_SENT_GATEWAY_PENDING",
        "push_targets": ["candidate", "employer", "platform_ledger"],
        "candidate": {"profile_id": c.profile_id, "name": c.name, "contact_email": c.contact_email, "target_role": c.role},
        "employer": {"employer_id": job.employer_id, "company_name": job.company_name, "contact_email": job.employer_email},
        "job": {"job_id": job.job_id, "title": job.title, "department": job.department,
                "worksite_coordinates": [job.latitude, job.longitude], "radius_miles": job.radius_miles},
        "scores": {"semantic_percentage": result.semantic_percentage, "min_semantic_score": job.min_semantic_score,
                   "matched_skills": sorted(result.matched_skills), "distance_miles": result.distance_miles},
        "logged_at": logged_at,
    }
