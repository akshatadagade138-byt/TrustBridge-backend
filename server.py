from fastapi import FastAPI, APIRouter, HTTPException
from dotenv import load_dotenv
from starlette.middleware.cors import CORSMiddleware
from motor.motor_asyncio import AsyncIOMotorClient
import os
import logging
from pathlib import Path
from pydantic import BaseModel, Field, ConfigDict, EmailStr
from typing import List, Optional
import uuid
from datetime import datetime, timezone
import httpx


ROOT_DIR = Path(__file__).parent
load_dotenv(ROOT_DIR / '.env')

# MongoDB connection
mongo_url = os.environ['MONGO_URL']
client = AsyncIOMotorClient(mongo_url)
db = client[os.environ['DB_NAME']]

# Optional Google Apps Script Web App URL.
# When set, every consultation request will also be forwarded there
# (which appends a row to the configured Google Sheet).
GOOGLE_SHEETS_WEBHOOK_URL = os.environ.get('GOOGLE_SHEETS_WEBHOOK_URL', '').strip()

# Create the main app without a prefix
app = FastAPI()

# Add CORS middleware first (before routes)
app.add_middleware(
    CORSMiddleware,
    allow_credentials=True,
    allow_origins=os.environ.get('CORS_ORIGINS', 'http://localhost:3001').split(','),
    allow_methods=["*"],
    allow_headers=["*"],
)

# Create a router with the /api prefix
api_router = APIRouter(prefix="/api")


# ---------- Models ----------
class StatusCheck(BaseModel):
    model_config = ConfigDict(extra="ignore")
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    client_name: str
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class StatusCheckCreate(BaseModel):
    client_name: str


class ConsultationRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=200)
    email: EmailStr
    phone: str = Field(..., pattern=r"^\d{10}$")
    subject: str = Field(default="", max_length=120)
    message: str = Field(..., min_length=1, max_length=4000)


class ConsultationStored(BaseModel):
    model_config = ConfigDict(extra="ignore")
    id: str
    name: str
    email: str
    phone: str
    subject: str
    message: str
    timestamp: datetime
    forwarded_to_sheet: bool


# ---------- Routes ----------
@api_router.get("/")
async def root():
    return {"message": "Hello World"}


@api_router.post("/status", response_model=StatusCheck)
async def create_status_check(input: StatusCheckCreate):
    status_obj = StatusCheck(**input.model_dump())
    doc = status_obj.model_dump()
    doc['timestamp'] = doc['timestamp'].isoformat()
    await db.status_checks.insert_one(doc)
    return status_obj


@api_router.get("/status", response_model=List[StatusCheck])
async def get_status_checks():
    status_checks = await db.status_checks.find({}, {"_id": 0}).to_list(1000)
    for check in status_checks:
        if isinstance(check['timestamp'], str):
            check['timestamp'] = datetime.fromisoformat(check['timestamp'])
    return status_checks


@api_router.post("/contact", response_model=ConsultationStored)
async def submit_consultation(payload: ConsultationRequest):
    """Stores a consultation request in MongoDB and (if configured) forwards
    the same payload — with a server-generated UTC timestamp — to a Google
    Apps Script Web App that appends a row to the consultation Google Sheet.
    """
    record_id = str(uuid.uuid4())
    timestamp = datetime.now(timezone.utc)

    doc = {
        "id": record_id,
        "name": payload.name.strip(),
        "email": str(payload.email).strip().lower(),
        "phone": payload.phone.strip(),
        "subject": payload.subject.strip(),
        "message": payload.message.strip(),
        "timestamp": timestamp.isoformat(),
        "forwarded_to_sheet": False,
    }

    # Always persist locally first so no submission is ever lost.
    await db.consultation_requests.insert_one(dict(doc))

    # Best-effort forward to Google Sheets via Apps Script Web App.
    if GOOGLE_SHEETS_WEBHOOK_URL:
        try:
            async with httpx.AsyncClient(timeout=10.0, follow_redirects=True) as http:
                resp = await http.post(
                    GOOGLE_SHEETS_WEBHOOK_URL,
                    json={
                        "id": doc["id"],
                        "date": timestamp.strftime("%Y-%m-%d %H:%M:%S UTC"),
                        "name": doc["name"],
                        "email": doc["email"],
                        "phone": doc["phone"],
                        "subject": doc["subject"],
                        "message": doc["message"],
                    },
                )
                if resp.status_code in (200, 201, 204):
                    doc["forwarded_to_sheet"] = True
                    await db.consultation_requests.update_one(
                        {"id": record_id},
                        {"$set": {"forwarded_to_sheet": True}},
                    )
                else:
                    logger.warning(
                        "Sheets webhook returned status %s: %s",
                        resp.status_code, resp.text[:300],
                    )
        except Exception as e:  # noqa: BLE001
            logger.exception("Failed to forward consultation to Google Sheets: %s", e)

    return ConsultationStored(
        id=doc["id"],
        name=doc["name"],
        email=doc["email"],
        phone=doc["phone"],
        subject=doc["subject"],
        message=doc["message"],
        timestamp=timestamp,
        forwarded_to_sheet=doc["forwarded_to_sheet"],
    )


@api_router.get("/contact", response_model=List[ConsultationStored])
async def list_consultations():
    rows = await db.consultation_requests.find({}, {"_id": 0}).sort("timestamp", -1).to_list(1000)
    out: List[ConsultationStored] = []
    for r in rows:
        ts = r.get("timestamp")
        if isinstance(ts, str):
            try:
                ts = datetime.fromisoformat(ts)
            except ValueError:
                ts = datetime.now(timezone.utc)
        out.append(ConsultationStored(
            id=r.get("id", ""),
            name=r.get("name", ""),
            email=r.get("email", ""),
            phone=r.get("phone", ""),
            subject=r.get("subject", ""),
            message=r.get("message", ""),
            timestamp=ts,
            forwarded_to_sheet=bool(r.get("forwarded_to_sheet", False)),
        ))
    return out


# Include the router in the main app
app.include_router(api_router)

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


@app.on_event("shutdown")
async def shutdown_db_client():
    client.close()
