from fastapi import FastAPI, Depends, HTTPException, BackgroundTasks, UploadFile, File
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from openai import OpenAI
from sqlalchemy.orm import Session
import json
import os
import random
import string
import datetime
from dotenv import load_dotenv
import requests
from zoneinfo import ZoneInfo


from database import engine, get_db, Base, SessionLocal
from models import ChatHistory, Product, ProductSearchIndex
from product_upload_service import (
    ALL_PRODUCT_UPLOAD_COLUMNS,
    REQUIRED_PRODUCT_UPLOAD_COLUMNS,
    upsert_product_upload,
)


from tools import search_products, get_product_details, check_availability
from sync_service import sync_sap_data
from routers.products import router as products_router
from routers.categories import router as categories_router
from routers.brands import router as brands_router
from routers.subcategories import router as subcategories_router
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from pathlib import Path
from pypdf import PdfReader

import base64
import re
from io import BytesIO
from PIL import Image, ImageOps
from pillow_heif import register_heif_opener

register_heif_opener()

load_dotenv()

# ── Safe table creation ──────────────────────────────────────────────────────
# Base.metadata.create_all() tries to create the knowledge_chunks table with
# a VECTOR(1536) column, which requires the pgvector C extension.  If pgvector
# is not installed the whole app crashes at import time.  Create all tables
# EXCEPT knowledge_chunks in one shot, then attempt knowledge_chunks separately
# with a broad except so the rest of the app stays alive without RAG.

from sqlalchemy import MetaData

def safe_create_all():
    all_tables = Base.metadata.tables              # OrderedDict of every registered table
    core_tables = {k: v for k, v in all_tables.items() if k != "knowledge_chunks"}
    if core_tables:
        core_meta = MetaData()
        for tbl in core_tables.values():
            tbl.to_metadata(core_meta)
        core_meta.create_all(bind=engine)

    knowledge_table = all_tables.get("knowledge_chunks")
    if knowledge_table is not None:
        try:
            single_meta = MetaData()
            knowledge_table.to_metadata(single_meta)
            single_meta.create_all(bind=engine)
            print("[RAG] knowledge_chunks table created.")
        except Exception as exc:
            print(
                "[RAG] WARNING: pgvector extension not installed — "
                f"knowledge_chunks table NOT created. ({exc})\n"
                "  Install pgvector: pip install pgvector && CREATE EXTENSION vector;\n"
                "  RAG features will be unavailable but the app will function normally."
            )

safe_create_all()

app = FastAPI()
app.include_router(products_router, prefix="", tags=["Products"])
app.include_router(categories_router, prefix="", tags=["Categories"])
app.include_router(brands_router, prefix="", tags=["Brands"])
app.include_router(subcategories_router, prefix="", tags=["Subcategories"])

# Initialize Scheduler
IRAQ_TIMEZONE = ZoneInfo("Asia/Baghdad")

# Initialize Scheduler with Iraq timezone
scheduler = AsyncIOScheduler(timezone=IRAQ_TIMEZONE)


@app.on_event("startup")
async def start_scheduler():
    if scheduler.running:
        return

    # Run SAP sync every day at 6:00 AM and 6:00 PM Iraq time
    scheduler.add_job(
        sync_sap_data,
        trigger="cron",
        hour="6,18",
        minute=0,
        timezone=IRAQ_TIMEZONE,
        id="sap_data_sync_twice_daily",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )

    scheduler.start()

    print(
        "Background Scheduler Started: SAP Sync scheduled daily at "
        "06:00 and 18:00 Iraq time."
    )

static_dir = os.path.join(os.path.dirname(__file__), "static")
app.mount("/static", StaticFiles(directory=static_dir), name="static")
client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

@app.get("/health")
def health_check():
    """Health check endpoint for production monitoring."""
    return {"status": "healthy", "timestamp": datetime.datetime.now().isoformat()}

@app.post("/sap/sync-now")
async def sync_sap_now():
    await sync_sap_data()

    return {
        "success": True,
        "message": "SAP sync completed.",
        "synced_at": datetime.datetime.now(IRAQ_TIMEZONE).isoformat(),
        "timezone": "Asia/Baghdad"
    }
MAX_HISTORY = 70

LEADS_FILE = os.path.join(os.path.dirname(__file__), "leads.json")
LEADS_API_URL = "https://test11.fireai.agency/api/v1/leads/"

RATE_FILE = os.path.join(os.path.dirname(__file__), "rate.json")

BASE_DIR = Path(__file__).resolve().parent

SYSTEM_PROMPT_FILE = BASE_DIR / "system_prompt.txt"
LEGACY_COMPANY_KNOWLEDGE_FILE = BASE_DIR / "company_knowledge.txt"

KNOWLEDGE_BASE_DIR = BASE_DIR / "knowledge_base"
KNOWLEDGE_INDEX_FILE = KNOWLEDGE_BASE_DIR / "index.json"

ALLOWED_KNOWLEDGE_EXTENSIONS = {".pdf", ".txt"}
MAX_KNOWLEDGE_UPLOAD_MB = 20

KNOWLEDGE_BASE_DIR.mkdir(exist_ok=True)

def load_iqd_rate():
    if not os.path.exists(RATE_FILE):
        return 1310

    with open(RATE_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)

    return float(data.get("iqd_rate", 1310))


def save_iqd_rate(rate: float):
    with open(RATE_FILE, "w", encoding="utf-8") as f:
        json.dump({"iqd_rate": rate}, f, ensure_ascii=False, indent=2)
def load_leads():
    if not os.path.exists(LEADS_FILE):
        return []
    with open(LEADS_FILE, "r", encoding="utf-8") as f:
        return json.load(f)

def load_knowledge_index() -> list:
    if not KNOWLEDGE_INDEX_FILE.exists():
        return []

    with open(KNOWLEDGE_INDEX_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def save_knowledge_index(items: list):
    with open(KNOWLEDGE_INDEX_FILE, "w", encoding="utf-8") as f:
        json.dump(items, f, ensure_ascii=False, indent=2)


def extract_text_from_pdf(file_path: Path) -> str:
    reader = PdfReader(str(file_path))
    pages = []

    for page_number, page in enumerate(reader.pages, start=1):
        text = page.extract_text() or ""
        if text.strip():
            pages.append(f"\n--- Page {page_number} ---\n{text.strip()}")

    return "\n".join(pages).strip()


def extract_text_from_upload(file_path: Path) -> str:
    suffix = file_path.suffix.lower()

    if suffix == ".pdf":
        return extract_text_from_pdf(file_path)

    if suffix == ".txt":
        return file_path.read_text(encoding="utf-8", errors="ignore").strip()

    raise HTTPException(
        status_code=400,
        detail="Only PDF and TXT knowledge files are supported."
    )


def safe_upload_name(filename: str) -> str:
    safe = "".join(
        c if c.isalnum() or c in {"-", "_", "."} else "_"
        for c in filename
    )
    return safe or "knowledge_file"


def load_company_knowledge():
    parts = []


    # Load uploaded PDF/TXT knowledge files
    for item in load_knowledge_index():
        text_path = KNOWLEDGE_BASE_DIR / item.get("text_filename", "")

        if text_path.exists():
            text = text_path.read_text(
                encoding="utf-8",
                errors="ignore"
            ).strip()

            if text:
                parts.append(
                    f"SOURCE: {item.get('original_filename', text_path.name)}\n{text}"
                )

    return "\n\n".join(parts)
    

def save_lead(user_id: str, products: list):
    if not products:
        return

    lead_payload = {
        "user_id": user_id,
        "interested_products": products[0].get("name") if products else ""
    }

    try:
        response = requests.post(
            LEADS_API_URL,
            json=lead_payload,
            timeout=10
        )

        print("LEAD POST STATUS:", response.status_code)
        print("LEAD POST RESPONSE:", response.text)

    except Exception as e:
        print("LEAD POST ERROR:", str(e))

FIXED_WELCOME_EN = (
    "✨ Welcome to DhifafBot, your personal premium concierge. "
    "I'm here to help you discover the finest beauty, cosmetics, and personal care products from our catalog. "
    "How may I elevate your shopping experience today? 🛍️"
)

FIXED_WELCOME_AR = (
    "✨ أهلاً بك في ضفاف بوت، مساعدك الشخصي للتسوق. "
    "أنا هنا لمساعدتك في اكتشاف أفضل منتجات التجميل والعناية الشخصية من تشكيلتنا المميزة. "
    "كيف يمكنني مساعدتك اليوم؟ 🛍️"
)

FIXED_GOODBYE_EN = (
    "🌟 Thank you for visiting DhifafBot. It has been a pleasure assisting you. "
    "If you need further recommendations or help with your orders, I'm always here to help. "
    "Have a beautiful day! ✨"
)

FIXED_GOODBYE_AR = (
    "🌟 شكراً لزيارتك ضفاف بوت. سعدت بمساعدتك اليوم. "
    "إذا كنت بحاجة إلى أي توصيات أخرى أو مساعدة في طلباتك لاحقاً، فأنا متواجد دائماً لخدمتك. "
    "أتمنى لك يوماً جميلاً! ✨"
)

# Load system prompt from file
def render_prompt_template(template: str) -> str:
    return template.format(
        FIXED_WELCOME_EN=FIXED_WELCOME_EN,
        FIXED_WELCOME_AR=FIXED_WELCOME_AR,
        FIXED_GOODBYE_EN=FIXED_GOODBYE_EN,
        FIXED_GOODBYE_AR=FIXED_GOODBYE_AR
    )


def load_system_prompt():
    if not SYSTEM_PROMPT_FILE.exists():
        raise HTTPException(
            status_code=500,
            detail="system_prompt.txt was not found."
        )

    template = SYSTEM_PROMPT_FILE.read_text(encoding="utf-8")
    return render_prompt_template(template)


SUPPORTED_IMAGE_MIMES = {
    "image/png",
    "image/jpeg",
    "image/jpg",
    "image/webp",
    "image/gif",
}

HEIC_IMAGE_MIMES = {
    "image/heic",
    "image/heif",
    "image/heic-sequence",
    "image/heif-sequence",
    "image/x-heic",
    "image/x-heif",
}

GENERIC_IMAGE_MIMES = {
    "",
    "application/octet-stream",
    "binary/octet-stream",
}

HEIF_BRANDS = {
    b"heic",
    b"heix",
    b"hevc",
    b"hevx",
    b"heim",
    b"heis",
    b"hevm",
    b"hevs",
    b"mif1",
    b"msf1",
}


def looks_like_heif(image_bytes: bytes) -> bool:
    if len(image_bytes) < 12:
        return False

    return (
        image_bytes[4:8] == b"ftyp"
        and (
            image_bytes[8:12] in HEIF_BRANDS
            or any(brand in image_bytes[12:64] for brand in HEIF_BRANDS)
        )
    )

def normalize_image_for_openai(data_url: str) -> str:
    if not data_url or not data_url.startswith("data:"):
        return data_url

    match = re.match(r"data:(.*?);base64,(.*)$", data_url, re.DOTALL)
    if not match:
        raise HTTPException(status_code=400, detail="Invalid image format.")

    mime_type = match.group(1).lower()
    base64_data = re.sub(r"\s+", "", match.group(2))

    if mime_type in SUPPORTED_IMAGE_MIMES:
        return data_url

    try:
        image_bytes = base64.b64decode(base64_data)
    except Exception as e:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid image data. Please upload JPG, PNG, WEBP, GIF, or HEIC. Error: {str(e)}"
        )

    is_known_heic = mime_type in HEIC_IMAGE_MIMES
    is_generic_heic = mime_type in GENERIC_IMAGE_MIMES and looks_like_heif(image_bytes)

    if not is_known_heic and not is_generic_heic:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported image type: {mime_type or 'unknown'}. Please upload JPG, PNG, WEBP, GIF, or HEIC."
        )

    try:
        image = Image.open(BytesIO(image_bytes))
        image = ImageOps.exif_transpose(image)

        if image.mode in ("RGBA", "LA", "P"):
            image = image.convert("RGBA")
            background = Image.new("RGB", image.size, (255, 255, 255))
            background.paste(image, mask=image.split()[-1])
            image = background
        else:
            image = image.convert("RGB")

        output = BytesIO()
        image.save(output, format="JPEG", quality=90)

        jpeg_base64 = base64.b64encode(output.getvalue()).decode("utf-8")
        return f"data:image/jpeg;base64,{jpeg_base64}"

    except Exception as e:
        raise HTTPException(
            status_code=400,
            detail=f"Could not process HEIC image. Please upload JPG or PNG. Error: {str(e)}"
        )
    

TOOL_DEFINITIONS = [
    {
        "type": "function",
        "function": {
            "name": "search_products",
            "description": "Search products by keyword, ItemCode, or description with optional filters",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search keyword (ALWAYS use English keywords here for better matching, e.g. 'shampoo' instead of 'شامبو')"},
                    "max_price": {"type": "number", "description": "Maximum price"},
                    "min_price": {"type": "number", "description": "Minimum price"},
                    "in_stock": {"type": "boolean", "description": "If true, return only in-stock products"},
                    "category": {"type": "string", "description": "Optional category to filter by (e.g., 'Perfume', 'Skincare', 'Hair care')"},
                    "limit": {"type": "integer", "description": "Maximum number of products to return (default 10). Use 3 for initial recommendations."},
                    "skip": {"type": "integer", "description": "Number of products to skip for pagination (default 0)"},
                    "sort_by": {"type": "string", "description": "Sort order: 'name', 'price_asc', 'price_desc' (default 'name')"}
                },
                "required": ["query"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_product_details",
            "description": "Get full details of a specific product by its ItemCode",
            "parameters": {
                "type": "object",
                "properties": {
                    "product_id": {"type": "string", "description": "Product ItemCode from search results"}
                },
                "required": ["product_id"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "check_availability",
            "description": "Check if a brand, product type, or specific concern (like acne) is available in our catalog without displaying items yet. Use this for broad availability questions.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Keyword to check (e.g., 'NYX', 'acne', 'cleanser')"}
                },
                "required": ["query"]
            }
        }
    },
]


def run_tool(tool_name: str, args: dict) -> str:
    if tool_name == "search_products":
        result = search_products(
            query=args.get("query", ""),
            max_price=args.get("max_price"),
            min_price=args.get("min_price"),
            in_stock=args.get("in_stock"),
            category=args.get("category"),
            limit=args.get("limit", 10),
            skip=args.get("skip", 0),
            sort_by=args.get("sort_by", "name")
        )
    elif tool_name == "get_product_details":
        result = get_product_details(args["product_id"])
    elif tool_name == "check_availability":
        result = check_availability(args["query"])
    else:
        result = {"error": "Unknown tool"}
    return json.dumps(result, ensure_ascii=False)


def get_history(user_id: str, db: Session) -> list:
    rows = (
        db.query(ChatHistory)
        .filter(ChatHistory.user_id == user_id)
        .order_by(ChatHistory.created_at.desc())
        .limit(MAX_HISTORY)
        .all()
    )
    rows.reverse()

    history = []
    for r in rows:
        item = {"role": r.role, "content": r.content}
        if r.metadata_json:
            try:
                extra = json.loads(r.metadata_json)
                if extra.get("products"):
                    item["products"] = extra["products"]
                if extra.get("image_url"):
                    item["image_url"] = extra["image_url"]
                if extra.get("order_link"):
                    item["order_link"] = extra["order_link"]
            except Exception:
                pass
        history.append(item)
    return history


def save_message(user_id: str, role: str, content: str, db: Session, metadata: dict | None = None) -> int:
    history_item = ChatHistory(user_id=user_id, role=role, content=content)
    if metadata:
        history_item.metadata_json = json.dumps(metadata, ensure_ascii=False)
    db.add(history_item)
    db.commit()
    db.refresh(history_item)
    return history_item.id



# ============================================================
# Chat UI
# ============================================================

@app.get("/", response_class=FileResponse)
def chat_ui():
    index_path = os.path.join(os.path.dirname(__file__), "static/index.html")
    return FileResponse(index_path)

class RateRequest(BaseModel):
    iqd_rate: float
# API Endpoints
class PromptUpdateRequest(BaseModel):
    prompt: str


class PromptResponse(BaseModel):
    prompt: str
    rendered_prompt: str | None = None


class ChatRequest(BaseModel):
    user_id: str
    message: str
    image_url: str | None = None  # Base64 string from frontend


class ChatResponse(BaseModel):
    reply: str
    image_url: str | None = None
    products: list | None = None
    user_message_id: int | None = None
    assistant_message_id: int | None = None


@app.get("/history/{user_id}")
def get_chat_history(user_id: str, db: Session = Depends(get_db)):
    return get_history(user_id, db)


@app.delete("/history/{user_id}")
def delete_chat_history(user_id: str, db: Session = Depends(get_db)):
    deleted = db.query(ChatHistory).filter(ChatHistory.user_id == user_id).delete()
    db.commit()
    return {"deleted": deleted}

@app.get("/leads")
def get_leads():
    return load_leads()

@app.get("/rate")
def get_rate():
    return {
        "iqd_rate": load_iqd_rate()
    }


@app.post("/rate")
def update_rate(data: RateRequest):
    save_iqd_rate(data.iqd_rate)
    return {
        "success": True,
        "iqd_rate": data.iqd_rate
    }

@app.get("/prompt", response_model=PromptResponse)
def get_prompt(render: bool = False):
    if not SYSTEM_PROMPT_FILE.exists():
        raise HTTPException(
            status_code=404,
            detail="system_prompt.txt was not found."
        )

    prompt = SYSTEM_PROMPT_FILE.read_text(encoding="utf-8")

    return PromptResponse(
        prompt=prompt,
        rendered_prompt=render_prompt_template(prompt) if render else None
    )


@app.put("/prompt", response_model=PromptResponse)
def update_prompt(data: PromptUpdateRequest):
    if not data.prompt.strip():
        raise HTTPException(
            status_code=400,
            detail="Prompt cannot be empty."
        )

    try:
        rendered = render_prompt_template(data.prompt)
    except KeyError as e:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Unknown prompt variable: {e}. "
                "Allowed variables: FIXED_WELCOME_EN, FIXED_WELCOME_AR, "
                "FIXED_GOODBYE_EN, FIXED_GOODBYE_AR"
            )
        )

    SYSTEM_PROMPT_FILE.write_text(data.prompt, encoding="utf-8")

    return PromptResponse(
        prompt=data.prompt,
        rendered_prompt=rendered
    )


@app.get("/knowledge")
def list_knowledge_files():
    return {
        "legacy_company_knowledge_exists": LEGACY_COMPANY_KNOWLEDGE_FILE.exists(),
        "files": load_knowledge_index(),
    }


@app.post("/knowledge/upload")
async def upload_knowledge_file(file: UploadFile = File(...)):
    original_name = file.filename or "knowledge_file"
    suffix = Path(original_name).suffix.lower()

    if suffix not in ALLOWED_KNOWLEDGE_EXTENSIONS:
        raise HTTPException(
            status_code=400,
            detail="Only PDF and TXT files are supported."
        )

    content = await file.read()

    max_bytes = MAX_KNOWLEDGE_UPLOAD_MB * 1024 * 1024
    if len(content) > max_bytes:
        raise HTTPException(
            status_code=400,
            detail=f"File is too large. Max size is {MAX_KNOWLEDGE_UPLOAD_MB} MB."
        )

    timestamp = datetime.datetime.utcnow().strftime("%Y%m%d%H%M%S")
    random_part = "".join(
        random.choices(string.ascii_lowercase + string.digits, k=6)
    )

    knowledge_id = f"{timestamp}_{random_part}"
    safe_name = safe_upload_name(original_name)

    stored_filename = f"{knowledge_id}_{safe_name}"
    stored_path = KNOWLEDGE_BASE_DIR / stored_filename
    stored_path.write_bytes(content)

    extracted_text = extract_text_from_upload(stored_path)

    if not extracted_text:
        stored_path.unlink(missing_ok=True)
        raise HTTPException(
            status_code=400,
            detail="No readable text was found in the uploaded file."
        )

    text_filename = f"{stored_filename}.txt"
    text_path = KNOWLEDGE_BASE_DIR / text_filename
    text_path.write_text(extracted_text, encoding="utf-8")

    items = load_knowledge_index()

    record = {
        "id": knowledge_id,
        "original_filename": original_name,
        "stored_filename": stored_filename,
        "text_filename": text_filename,
        "content_type": file.content_type,
        "uploaded_at": datetime.datetime.utcnow().isoformat() + "Z",
        "characters": len(extracted_text),
    }

    items.append(record)
    save_knowledge_index(items)

    return {
        "success": True,
        "file": record
    }


@app.delete("/knowledge/{knowledge_id}")
def delete_knowledge_file(knowledge_id: str):
    items = load_knowledge_index()

    match = next(
        (item for item in items if item.get("id") == knowledge_id),
        None
    )

    if not match:
        raise HTTPException(
            status_code=404,
            detail="Knowledge file not found."
        )

    for key in ["stored_filename", "text_filename"]:
        filename = match.get(key)
        if filename:
            (KNOWLEDGE_BASE_DIR / filename).unlink(missing_ok=True)

    save_knowledge_index([
        item for item in items
        if item.get("id") != knowledge_id
    ])

    return {
        "success": True,
        "deleted": knowledge_id
    }


@app.get("/products/upload-template")
def get_product_upload_template():
    return {
        "required_columns": REQUIRED_PRODUCT_UPLOAD_COLUMNS,
        "all_supported_columns": ALL_PRODUCT_UPLOAD_COLUMNS,
        "accepted_file_types": [".xlsx", ".csv"],
        "notes": [
            "First sheet will be used for Excel files.",
            "item_code and item_name are required.",
            "barcode is optional. If barcode is empty, item_code will be used as product ID.",
            "concerns and tags should be comma separated.",
            "Existing products will be updated when barcode/product ID already exists.",
        ],
    }


@app.post("/products/upload")
async def upload_products(
    file: UploadFile = File(...),
    dry_run: bool = False,
    db: Session = Depends(get_db),
):
    filename = file.filename or ""

    if not (
        filename.lower().endswith(".xlsx")
        or filename.lower().endswith(".csv")
    ):
        raise HTTPException(
            status_code=400,
            detail="Only .xlsx and .csv files are supported."
        )

    content = await file.read()

    if not content:
        raise HTTPException(
            status_code=400,
            detail="Uploaded file is empty."
        )

    try:
        result = upsert_product_upload(
            db=db,
            filename=filename,
            content=content,
            dry_run=dry_run,
        )

        return {
            "success": True,
            "message": (
                "Product upload checked successfully."
                if dry_run
                else "Products uploaded successfully."
            ),
            "result": result,
        }

    except ValueError as e:
        raise HTTPException(
            status_code=400,
            detail=str(e)
        )

    except Exception as e:
        db.rollback()
        raise HTTPException(
            status_code=500,
            detail=f"Product upload failed: {str(e)}"
        )
    
    


@app.post("/reply", response_model=ChatResponse)
def generate_reply(data: ChatRequest, db: Session = Depends(get_db)):
    history = get_history(data.user_id, db)

    # ============================================================
    # BACKEND GREETING / FAREWELL INTERCEPTION
    # This bypasses the AI entirely for pure greetings to ensure
    # the fixed branded message is always shown.
    # ============================================================
    GREETING_WORDS = {
        "hello", "hi", "hey", "hii", "hiii", "salam", "salaam",
        "مرحبا", "أهلا", "أهلاً", "اهلا", "اهلاً", "هلا", "هلو",
        "হ্যালো", "হেলো", "হাই", "নমস্কার", "সালাম"
    }
    FAREWELL_WORDS = {
        "bye", "goodbye", "good bye", "see you", "take care",
        "وداعاً", "وداعا", "مع السلامة", "شكراً", "شكرا",
        "আলবিদা", "বিদায়", "ধন্যবাদ"
    }
    
    msg_clean = data.message.strip().lower().rstrip("!.,؟?")
    
    # Detect Arabic script to choose correct message
    has_arabic = any('\u0600' <= c <= '\u06FF' for c in data.message)
    
    # Pure greeting check (message is ONLY a greeting word)
    if msg_clean in GREETING_WORDS:
        fixed_reply = FIXED_WELCOME_AR if has_arabic else FIXED_WELCOME_EN
        u_id = save_message(data.user_id, "user", data.message, db)
        a_id = save_message(data.user_id, "assistant", fixed_reply, db)
        return ChatResponse(reply=fixed_reply, user_message_id=u_id, assistant_message_id=a_id)
    
    # Pure farewell check
    if msg_clean in FAREWELL_WORDS:
        fixed_reply = FIXED_GOODBYE_AR if has_arabic else FIXED_GOODBYE_EN
        u_id = save_message(data.user_id, "user", data.message, db)
        a_id = save_message(data.user_id, "assistant", fixed_reply, db)
        return ChatResponse(reply=fixed_reply, user_message_id=u_id, assistant_message_id=a_id)

    # NORMAL AI FLOW (for all other messages)
    system_prompt = load_system_prompt()

    company_knowledge = load_company_knowledge()

    system_prompt += f"""

    COMPANY KNOWLEDGE:
    Use this information when users ask about Dhifaf Baghdad, DBC, company profile, branches, brands, offices, app, partners, or company background.

    {company_knowledge}

    Rules:
    - Answer company questions using this company knowledge.
    - If the user asks in Arabic, answer in Arabic.
    - If the user asks in English, answer in English.
    - Do not invent company facts not listed here.
    """
    messages_for_ai = [{"role": "system", "content": system_prompt}]
    for msg in history:
        content = msg["content"]
        # If there are products in metadata, inject their IDs/Barcodes hiddenly for the AI's context
        if msg.get("products"):
            meta_strings = []
            for p in msg["products"]:
                meta_strings.append(f"[METADATA: Name: {p.get('name')}, ItemCode: {p.get('id')}, Barcode: {p.get('barcode')}]")
            content += "\n" + "\n".join(meta_strings)
            
        messages_for_ai.append({"role": msg["role"], "content": content})
    messages_for_ai.append({"role": "user", "content": data.message})

    # Save the user message now (so it appears in history even if something fails)
    user_msg_id = save_message(data.user_id, "user", data.message, db)

    image_url = None
    products = []


    # Prepare multimodal content if an image is present
    if data.image_url:
        image_for_ai = normalize_image_for_openai(data.image_url)

        user_content = [{"type": "text", "text": data.message}]
        user_content.append({
            "type": "image_url",
            "image_url": {"url": image_for_ai}
        })
        # The last message in messages_for_ai is currently the user message
        messages_for_ai[-1]["content"] = user_content

    while True:
        response = client.chat.completions.create(
            model="gpt-4o",  # Upgraded to GPT-4o for smarter reasoning and vision
            messages=messages_for_ai,
            tools=TOOL_DEFINITIONS,
            tool_choice="auto",
            temperature=0.7,
            max_tokens=1000,
        )

        ai_message = response.choices[0].message

        if ai_message.tool_calls:
            messages_for_ai.append(ai_message)   # append the assistant message with tool calls
            for tool_call in ai_message.tool_calls:
                tool_name = tool_call.function.name
                tool_args = json.loads(tool_call.function.arguments)

                tool_result_str = run_tool(tool_name, tool_args)

                tool_result = json.loads(tool_result_str)

                # Process results
                if tool_result.get("found"):
                    # ONLY search_products results go to the UI for card rendering
                    if tool_name == "search_products" and "products" in tool_result:
                        for p in tool_result["products"]:
                            price = str(p.get("price", "")).strip().lower()

                            if price in ["", "n/a", "na", "none", "null", "0", "0.0"]:
                                continue
                            products.append({
                                "id":          p.get("id", ""),
                                "name":        p.get("name", ""),
                                "price":       p.get("price", ""),
                                "barcode":     p.get("id", ""), # Barcode is stored in 'id' key from tools.py
                                "description": p.get("description", ""),
                                "image_url":   p.get("image_url", ""),
                                "stock":       p.get("available_qty", 0),
                            })
                        if products:
                            image_url = products[0]["image_url"]


                
                    
                    # Note: get_product_details results are NOT added to the 'products' list.
                    # They are passed to the AI via 'messages_for_ai' for internal info (price/stock).
                    if tool_name == "get_product_details":
                        if not image_url:
                            image_url = tool_result.get("image_url")

                        interested_product = tool_result.get("item_name") or tool_result.get("name")

                        if interested_product:
                            save_lead(data.user_id, [{"name": interested_product}])

                messages_for_ai.append({
                    "role": "tool",
                    "tool_call_id": tool_call.id,
                    "content": tool_result_str,
                })
            continue   # loop again to get the final answer after tools

        reply_text = ai_message.content
        break

    assistant_msg_id = save_message(
        data.user_id, "assistant", reply_text, db,
        metadata={
            "products":  products  if products  else None,
            "image_url": image_url,
        },
    )

    return ChatResponse(
        reply=reply_text,
        image_url=image_url,
        products=products if products else None,
        user_message_id=user_msg_id,
        assistant_message_id=assistant_msg_id
    )

@app.get("/conversations")
def get_conversations(db: Session = Depends(get_db)):
    from sqlalchemy import func, and_
    # Get first user message for each user_id
    subq = db.query(
        ChatHistory.user_id,
        func.min(ChatHistory.created_at).label('first_time')
    ).filter(ChatHistory.role == 'user').group_by(ChatHistory.user_id).subquery()
    first_msgs = db.query(ChatHistory).join(
        subq,
        and_(ChatHistory.user_id == subq.c.user_id, ChatHistory.created_at == subq.c.first_time)
    ).all()
    
    # Get latest timestamp per user_id
    latest_times = db.query(
        ChatHistory.user_id,
        func.max(ChatHistory.created_at).label('last_time')
    ).group_by(ChatHistory.user_id).all()
    time_map = {t.user_id: t.last_time for t in latest_times}
    
    conversations = []
    for msg in first_msgs:
        conversations.append({
            "user_id": msg.user_id,
            "title": msg.content[:50],
            "last_updated": time_map.get(msg.user_id, msg.created_at).isoformat()
        })
    conversations.sort(key=lambda x: x['last_updated'], reverse=True)
    return conversations
