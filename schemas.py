"""
Database Schemas

Define your MongoDB collection schemas here using Pydantic models.
Each Pydantic model represents a collection in your database.
Class name lowercased is used as the collection name.
"""

from pydantic import BaseModel, Field, EmailStr
from typing import Optional, List, Dict, Any


class Profile(BaseModel):
    """User profile and preferences. Collection name: "profile"""
    full_name: str
    email: EmailStr
    phone: Optional[str] = None
    locations: List[str] = Field(default_factory=lambda: ["UAE", "Dubai"])  # preferred locations
    remote_ok: bool = True
    target_titles: List[str] = Field(default_factory=list)
    skills: List[str] = Field(default_factory=list)
    min_salary_aed: Optional[int] = None
    cv_text: str = ""
    linkedin: Optional[str] = None
    website: Optional[str] = None


class Job(BaseModel):
    """Normalized job posting. Collection name: "job"""
    source: str  # e.g., indeed, lever, greenhouse
    source_id: Optional[str] = None
    title: str
    company: Optional[str] = None
    location: Optional[str] = None
    url: str
    description: Optional[str] = None
    posted_at: Optional[str] = None
    tags: List[str] = Field(default_factory=list)
    matched_score: Optional[float] = None


class Application(BaseModel):
    """Application records. Collection name: "application"""
    job_id: str
    job_url: str
    job_title: str
    company: Optional[str] = None
    apply_channel: str  # e.g., lever, greenhouse, indeed-manual
    status: str = "queued"  # queued, submitted, failed, manual_required
    tailored_cv: Optional[str] = None
    cover_letter: Optional[str] = None
    notes: Optional[str] = None


# The Flames database viewer reads these schemas via /schema endpoint in main.py.
