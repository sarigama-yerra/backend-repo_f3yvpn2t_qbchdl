import os
from typing import List, Optional, Dict, Any
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from bson.objectid import ObjectId
import re
import requests

from database import db, create_document, get_documents
from schemas import Profile, Job, Application

app = FastAPI(title="Job Auto Apply API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------- Utilities ----------

def to_object_id(id_str: str) -> ObjectId:
    try:
        return ObjectId(id_str)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid ID format")


def tokenize(text: str) -> List[str]:
    if not text:
        return []
    return re.findall(r"[a-zA-Z0-9+#.]+", text.lower())


# ---------- Health ----------

@app.get("/")
def read_root():
    return {"message": "Job Auto Apply Backend running"}


@app.get("/test")
def test_database():
    response = {
        "backend": "✅ Running",
        "database": "❌ Not Available",
        "database_url": "❌ Not Set",
        "database_name": "❌ Not Set",
        "connection_status": "Not Connected",
        "collections": [],
    }
    try:
        if db is not None:
            response["database"] = "✅ Available"
            response["database_url"] = "✅ Set" if os.getenv("DATABASE_URL") else "❌ Not Set"
            response["database_name"] = db.name
            response["connection_status"] = "Connected"
            collections = db.list_collection_names()
            response["collections"] = collections
        else:
            response["database"] = "⚠️ Available but not initialized"
    except Exception as e:
        response["database"] = f"❌ Error: {str(e)[:120]}"
    return response


# ---------- Schemas endpoint (viewer support) ----------

@app.get("/schema")
def get_schema_definitions():
    from schemas import Profile, Job, Application
    return {
        "profile": Profile.model_json_schema(),
        "job": Job.model_json_schema(),
        "application": Application.model_json_schema(),
    }


# ---------- Profile ----------

class ProfileIn(BaseModel):
    full_name: str
    email: str
    phone: Optional[str] = None
    locations: Optional[List[str]] = None
    remote_ok: Optional[bool] = True
    target_titles: Optional[List[str]] = None
    skills: Optional[List[str]] = None
    min_salary_aed: Optional[int] = None
    cv_text: str
    linkedin: Optional[str] = None
    website: Optional[str] = None


@app.post("/profile")
def upsert_profile(payload: ProfileIn):
    if db is None:
        raise HTTPException(500, "Database not configured")
    data = payload.model_dump()
    existing = db["profile"].find_one({"email": data["email"]})
    if existing:
        db["profile"].update_one({"_id": existing["_id"]}, {"$set": data})
        _id = existing["_id"]
    else:
        inserted_id = create_document("profile", data)
        _id = ObjectId(inserted_id)
    doc = db["profile"].find_one({"_id": _id})
    doc["_id"] = str(doc["_id"])
    return doc


@app.get("/profile")
def get_profile(email: Optional[str] = None):
    if db is None:
        raise HTTPException(500, "Database not configured")
    q = {"email": email} if email else {}
    doc = db["profile"].find_one(q) if email else db["profile"].find_one()
    if not doc:
        raise HTTPException(404, "Profile not found")
    doc["_id"] = str(doc["_id"])
    return doc


# ---------- Ingestion: Indeed RSS (UAE) ----------

def build_indeed_rss_urls(profile: dict) -> List[str]:
    base = "https://ae.indeed.com/rss"
    urls = []
    titles = profile.get("target_titles") or []
    locs = profile.get("locations") or ["United Arab Emirates"]
    if not titles:
        titles = ["Digital Health", "Healthcare AI", "Medical Director", "Clinical" ]
    for t in titles:
        q = requests.utils.quote(t)
        for l in locs:
            ll = requests.utils.quote(l)
            urls.append(f"{base}?q={q}&l={ll}")
    return list(dict.fromkeys(urls))


def parse_indeed_rss(url: str) -> List[Dict[str, Any]]:
    # RSS is XML; we avoid heavy deps: simple regex-based minimal parse for item blocks
    try:
        resp = requests.get(url, timeout=15)
        resp.raise_for_status()
        text = resp.text
    except Exception as e:
        return []
    items = []
    for block in re.findall(r"<item>(.*?)</item>", text, flags=re.S | re.I):
        def tag(name):
            m = re.search(rf"<{name}>(.*?)</{name}>", block, flags=re.S | re.I)
            return re.sub(r"<.*?>", "", m.group(1)).strip() if m else None
        title = tag("title") or ""
        link = tag("link") or ""
        desc = tag("description") or ""
        pub = tag("pubDate") or None
        company = None
        # Indeed "title" often like: Title - Company - Location
        parts = [p.strip() for p in title.split(" - ")]
        if len(parts) >= 2:
            title_clean = parts[0]
            company = parts[1]
        else:
            title_clean = title
        items.append({
            "source": "indeed",
            "source_id": None,
            "title": title_clean,
            "company": company,
            "location": None,
            "url": link,
            "description": desc,
            "posted_at": pub,
            "tags": [],
        })
    return items


@app.post("/ingest/indeed")
def ingest_indeed(email: Optional[str] = None):
    if db is None:
        raise HTTPException(500, "Database not configured")
    profile = db["profile"].find_one({"email": email}) if email else db["profile"].find_one()
    if not profile:
        raise HTTPException(400, "Profile not found. Create it first.")
    urls = build_indeed_rss_urls(profile)
    all_items: List[Dict[str, Any]] = []
    for u in urls:
        items = parse_indeed_rss(u)
        all_items.extend(items)
    # upsert jobs by url
    inserted = 0
    for job in all_items:
        existing = db["job"].find_one({"url": job["url"]})
        if existing:
            db["job"].update_one({"_id": existing["_id"]}, {"$set": job})
        else:
            create_document("job", job)
            inserted += 1
    return {"sources": urls, "found": len(all_items), "inserted": inserted}


# ---------- Matching ----------

class MatchRequest(BaseModel):
    email: Optional[str] = None
    top_n: int = 50


@app.post("/match")
def match_jobs(payload: MatchRequest):
    if db is None:
        raise HTTPException(500, "Database not configured")
    profile = db["profile"].find_one({"email": payload.email}) if payload.email else db["profile"].find_one()
    if not profile:
        raise HTTPException(400, "Profile not found")
    skills = set(tokenize(" ".join(profile.get("skills") or [])))
    titles = set(tokenize(" ".join(profile.get("target_titles") or [])))
    cv_tokens = set(tokenize(profile.get("cv_text", "")))

    jobs = list(db["job"].find())
    for j in jobs:
        text = " ".join([
            j.get("title") or "",
            j.get("company") or "",
            j.get("description") or "",
        ])
        jt = set(tokenize(text))
        score = 0.0
        if titles:
            score += len(jt & titles) * 2.0
        if skills:
            score += len(jt & skills) * 1.5
        score += len(jt & cv_tokens) * 0.2
        j["matched_score"] = round(score, 2)
        db["job"].update_one({"_id": j["_id"]}, {"$set": {"matched_score": j["matched_score"]}})
    jobs_sorted = sorted(jobs, key=lambda x: x.get("matched_score", 0), reverse=True)[: payload.top_n]
    # serialize ids
    for j in jobs_sorted:
        j["_id"] = str(j["_id"])
    return {"count": len(jobs_sorted), "jobs": jobs_sorted}


# ---------- Applications ----------

class ApplyRequest(BaseModel):
    job_id: str


def detect_channel(url: str) -> str:
    if "greenhouse.io" in url:
        return "greenhouse"
    if "jobs.lever.co" in url:
        return "lever"
    if "workable.com" in url:
        return "workable"
    if "indeed" in url:
        return "indeed"
    return "other"


@app.post("/apply")
def queue_application(payload: ApplyRequest):
    if db is None:
        raise HTTPException(500, "Database not configured")
    job = db["job"].find_one({"_id": to_object_id(payload.job_id)})
    if not job:
        raise HTTPException(404, "Job not found")
    channel = detect_channel(job.get("url", ""))
    status = "queued" if channel in {"lever", "greenhouse", "workable"} else "manual_required"
    app_doc = {
        "job_id": str(job["_id"]),
        "job_url": job.get("url"),
        "job_title": job.get("title"),
        "company": job.get("company"),
        "apply_channel": channel,
        "status": status,
        "notes": None,
    }
    create_document("application", app_doc)
    return {"message": "Application queued", "channel": channel, "status": status}


@app.get("/jobs")
def list_jobs(min_score: Optional[float] = 0.0, limit: int = 50):
    if db is None:
        raise HTTPException(500, "Database not configured")
    cur = db["job"].find({"matched_score": {"$gte": float(min_score)}}).sort("matched_score", -1).limit(limit)
    jobs = list(cur)
    for j in jobs:
        j["_id"] = str(j["_id"])
    return {"count": len(jobs), "jobs": jobs}


@app.get("/applications")
def list_applications():
    if db is None:
        raise HTTPException(500, "Database not configured")
    apps = list(db["application"].find().sort("created_at", -1))
    for a in apps:
        a["_id"] = str(a["_id"])
    return {"count": len(apps), "applications": apps}


if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
