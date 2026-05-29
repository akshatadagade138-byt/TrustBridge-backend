from fastapi import FastAPI, APIRouter, HTTPException
from dotenv import load_dotenv
from starlette.middleware.cors import CORSMiddleware
import os
import logging
from pathlib import Path
from pydantic import BaseModel, Field, ConfigDict, EmailStr
import uuid
from datetime import datetime, timezone
import httpx


ROOT_DIR = Path(__file__).parent
load_dotenv(ROOT_DIR / '.env')

# Google Apps Script Web App URL that appends consultation requests to Google Sheets.
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


@api_router.post("/contact", response_model=ConsultationStored)
async def submit_consultation(payload: ConsultationRequest):
    """Forward a consultation request to Google Sheets."""
    if not GOOGLE_SHEETS_WEBHOOK_URL:
        raise HTTPException(
            status_code=500,
            detail="GOOGLE_SHEETS_WEBHOOK_URL is not configured.",
        )

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
        "forwarded_to_sheet": True,
    }

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
            if resp.status_code not in (200, 201, 204):
                logger.warning(
                    "Sheets webhook returned status %s: %s",
                    resp.status_code, resp.text[:300],
                )
                raise HTTPException(
                    status_code=502,
                    detail="Google Sheets webhook rejected the request.",
                )
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001
        logger.exception("Failed to forward consultation to Google Sheets: %s", e)
        raise HTTPException(
            status_code=502,
            detail="Failed to forward consultation to Google Sheets.",
        ) from e

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


# Include the router in the main app
app.include_router(api_router)

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)
