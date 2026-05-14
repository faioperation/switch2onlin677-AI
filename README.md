# 🛍️ Dhifaf AI - Sales & Shopping Advisor Chatbot

Dhifaf AI is a premium, high-performance AI-powered sales assistant designed for beauty, skincare, and personal care brands. It features advanced product discovery, visual search (multimodal), and real-time inventory synchronization with SAP systems.

---

## 🚀 Key Features

- **Visual Search (Vision AI):** Customers can upload images of products, and the bot identifies the brand/product name to search the inventory automatically.
- **Multilingual Support:** Seamlessly converses in **English, Arabic, and Bengali**, mirroring the user's preferred language.
- **SAP Inventory Sync:** Automatically updates product prices and stock levels from SAP Service Layer every 24 hours.
- **Advanced Fuzzy Search:** Uses PostgreSQL `pg_trgm` for high-accuracy product matching even with typos.
- **Branded Experience:** Customized welcome and farewell messages for a premium shopping feel.
- **Responsive UI:** Modern, dark-themed chat interface with glassmorphism aesthetics.

---

## 🛠️ Tech Stack

- **Backend:** FastAPI (Python)
- **Database:** PostgreSQL (with `pg_trgm` extension)
- **AI Engine:** OpenAI GPT-4o (Vision enabled)
- **ORM:** SQLAlchemy
- **Frontend:** Vanilla JS, HTML5, CSS3 (Modern Glassmorphism)
- **Production Server:** Gunicorn + Uvicorn + Nginx
- **Scheduler:** APScheduler (for automated sync)

---

## 💻 Local Installation

1. **Clone the repository:**
   ```bash
   git clone <repo-url>
   cd AI
   ```

2. **Create a virtual environment:**
   ```bash
   python -m venv venv
   source venv/bin/activate  # On Windows: venv\Scripts\activate
   ```

3. **Install dependencies:**
   ```bash
   pip install -r requirements.txt
   ```

4. **Configure Environment Variables:**
   Create a `.env` file in the root directory:
   ```env
   OPENAI_API_KEY=your_openai_key
   DATABASE_URL=postgresql://user:password@localhost:5432/db_name
   SAP_API_URL=https://your-sap-server.com
   IQD_RATE=1310
   ```

5. **Initialize Database:**
   Ensure PostgreSQL is running and the `pg_trgm` extension is enabled.
   ```bash
   python import_catalog.py --file products.xlsx
   ```

6. **Run the app:**
   ```bash
   uvicorn main:app --reload
   ```

---

## 🌐 VPS Deployment (Production)

### 1. Database Setup
Ensure extensions are enabled:
```sql
CREATE EXTENSION IF NOT EXISTS pg_trgm;
CREATE EXTENSION IF NOT EXISTS fuzzystrmatch;
```

### 2. Systemd Service Configuration
Create `/etc/systemd/system/salesbot.service`:
```ini
[Unit]
Description=Gunicorn instance to serve DhifafBot
After=network.target

[Service]
User=root
Group=www-data
WorkingDirectory=/var/www/sales-ai-chatbot/ai-codebase
Environment="PATH=/var/www/sales-ai-chatbot/ai-codebase/venv/bin"
ExecStart=/var/www/sales-ai-chatbot/ai-codebase/venv/bin/gunicorn -w 4 -k uvicorn.workers.UvicornWorker main:app --bind 0.0.0.0:8000

[Install]
WantedBy=multi-user.target
```

### 3. Nginx Reverse Proxy
Create `/etc/nginx/sites-available/salesbot`:
```nginx
server {
    listen 80;
    server_name your-server-ip;

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
    }
}
```

---

## 🔌 API Documentation

FastAPI automatically generates interactive documentation at `/docs` (Swagger UI) and `/redoc`. Below is a summary of the core endpoints:

### 1. Chat Interface
- **Endpoint:** `GET /`
- **Description:** Serves the main chat interface (HTML/JS/CSS).

### 2. Generate AI Reply
- **Endpoint:** `POST /reply`
- **Description:** Sends a message (and optional image) to the AI and receives a response with product recommendations.
- **Request Body:**
  ```json
  {
    "user_id": "unique-id",
    "message": "Do you have lipstick?",
    "image_url": "data:image/png;base64,..." (Optional)
  }
  ```
- **Response:** Returns `reply` (text), `products` (array), and `image_url`.

### 3. Chat History
- **Endpoint:** `GET /history/{user_id}`
- **Description:** Retrieves all previous messages for a specific user to maintain conversation state.

### 4. Clear History
- **Endpoint:** `DELETE /history/{user_id}`
- **Description:** Deletes all chat records for a specific user ID.

### 5. Admin Conversations
- **Endpoint:** `GET /conversations`
- **Description:** Returns a list of all active user IDs and their first message, used for the sidebar chat list.

---

## 🔄 SAP Synchronization

The system is configured to sync every 24 hours. To trigger a manual sync:
```bash
python sync_service.py
```
This updates `price` and `available_qty` for all products based on the `ItemBarcode` match.

---

## 📄 License
Internal proprietary software for Dhifaf Bot. 
© 2026 Dhifaf Team.
