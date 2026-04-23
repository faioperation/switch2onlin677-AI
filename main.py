from fastapi import FastAPI, Depends, HTTPException, BackgroundTasks
from fastapi.responses import HTMLResponse, FileResponse
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

from database import engine, get_db, Base, SessionLocal
from models import ChatHistory, Order, Product
from tools import search_products, get_product_details, check_availability
from sync_sap import sync_products

load_dotenv()

Base.metadata.create_all(bind=engine)

app = FastAPI()
static_dir = os.path.join(os.path.dirname(__file__), "static")
app.mount("/static", StaticFiles(directory=static_dir), name="static")
client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

@app.on_event("startup")
def startup_event():
    db = SessionLocal()
    try:
        # Prioritize local JSON sync if getItems.json exists
        json_path = os.path.join(os.path.dirname(__file__), "getItems.json")
        if os.path.exists(json_path):
            print("Detected getItems.json. Triggering JSON sync for 100% accuracy...")
            sync_products()
        else:
            print("No getItems.json found. System running in standby.")
    finally:
        db.close()

@app.post("/sync")
def trigger_sync(background_tasks: BackgroundTasks):
    """Manually trigger a full product sync from getItems.json."""
    background_tasks.add_task(sync_products)
    return {"message": "JSON Synchronization started in background."}

MAX_HISTORY = 70

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
def load_system_prompt():
    prompt_path = os.path.join(os.path.dirname(__file__), "system_prompt.txt")
    with open(prompt_path, "r", encoding="utf-8") as f:
        template = f.read()
    # Format with bilingual messages
    return template.format(
        FIXED_WELCOME_EN=FIXED_WELCOME_EN,
        FIXED_WELCOME_AR=FIXED_WELCOME_AR,
        FIXED_GOODBYE_EN=FIXED_GOODBYE_EN,
        FIXED_GOODBYE_AR=FIXED_GOODBYE_AR
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
    {
        "type": "function",
        "function": {
            "name": "place_order",
            "description": "Place a final order for items currently in the cart.",
            "parameters": {
                "type": "object",
                "properties": {
                    "items": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "product_id": {"type": "string", "description": "Product ItemCode"},
                                "product_name": {"type": "string", "description": "Name of the product"},
                                "quantity": {"type": "integer", "description": "Quantity to order"}
                            },
                            "required": ["product_id", "product_name", "quantity"]
                        }
                    },
                    "customer_name": {"type": "string", "description": "Full name of the customer"},
                    "customer_email": {"type": "string", "description": "Email address"},
                    "address": {"type": "string", "description": "Full shipping address"},
                    "phone": {"type": "string", "description": "Phone number (optional)"}
                },
                "required": ["items", "customer_name", "customer_email", "address"]
            }
        }
    }
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

def save_order(user_id: str, order_args: dict, db: Session) -> dict:
    # Generate a SINGLE unique Order ID for all items in this checkout
    random_id = ''.join(random.choices(string.ascii_uppercase + string.digits, k=6))
    order_id = f"ORD-{random_id}"
    
    items = order_args.get("items", [])
    if not items:
        return {"success": False, "message": "No items found to order."}

    for item in items:
        new_item = Order(
            user_id=user_id,
            order_id=order_id,
            customer_name=order_args["customer_name"],
            customer_email=order_args["customer_email"],
            product_id=item["product_id"],
            product_name=item["product_name"],
            quantity=item.get("quantity", 1),
            address=order_args["address"],
            phone=order_args.get("phone", "")
        )
        db.add(new_item)
    
    db.commit()
    return {
        "success": True, 
        "orderID": order_id,
        "message": f"Your order for {len(items)} items has been placed successfully! Your Order ID is: {order_id}"
    }


# ============================================================
# Chat UI
# ============================================================

@app.get("/", response_class=FileResponse)
def chat_ui():
    index_path = os.path.join(os.path.dirname(__file__), "static/index.html")
    return FileResponse(index_path)

# API Endpoints
class ChatRequest(BaseModel):
    user_id: str
    message: str
    image_url: str | None = None  # Base64 string from frontend


class ChatResponse(BaseModel):
    reply: str
    image_url: str | None = None
    order_link: str | None = None
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
    order_link = None
    products = []


    # Prepare multimodal content if an image is present
    if data.image_url:
        user_content = [{"type": "text", "text": data.message}]
        user_content.append({
            "type": "image_url",
            "image_url": {"url": data.image_url}
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

                if tool_name == "place_order":
                    # Save order directly using our function
                    result = save_order(data.user_id, tool_args, db)
                    tool_result_str = json.dumps(result, ensure_ascii=False)
                else:
                    # For search_products and get_product_details
                    tool_result_str = run_tool(tool_name, tool_args)

                tool_result = json.loads(tool_result_str)

                # Process results
                if tool_result.get("found"):
                    # ONLY search_products results go to the UI for card rendering
                    if tool_name == "search_products" and "products" in tool_result:
                        for p in tool_result["products"]:
                            products.append({
                                "id":          p.get("id", ""),
                                "name":        p.get("name", ""),
                                "price":       p.get("price", ""),
                                "barcode":     p.get("barcode", ""),
                                "description": p.get("description", ""),
                                "image_url":   p.get("image_url", ""),
                                "stock":       p.get("stock", 0),
                            })
                        if products:
                            image_url = products[0]["image_url"]
                    
                    # Note: get_product_details results are NOT added to the 'products' list.
                    # They are passed to the AI via 'messages_for_ai' for internal info (price/stock).
                    if tool_name == "get_product_details":
                        # We still want to capture the image for the response if relevant
                        if not image_url:
                            image_url = tool_result.get("image_url")

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
            "products":   products   if products   else None,
            "image_url":  image_url,
            "order_link": order_link,
        },
    )

    return ChatResponse(
        reply=reply_text,
        image_url=image_url,
        order_link=order_link,
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