# Deployment Guide: Telegram CRM & WhatsApp Bot

## Environment Variables
Add the following environment variables to your Railway project (or `.env` locally):

| Variable | Description | Example |
| :--- | :--- | :--- |
| `SUPABASE_URL` | Your Supabase Project URL | `https://xyz.supabase.co` |
| `SUPABASE_KEY` | Your Supabase Service Role Key (or Anon if RLS permits) | `eyJ...` |
| `META_TOKEN` | Meta WhatsApp Cloud API Token | `EAAB...` |
| `PHONE_NUMBER_ID` | WhatsApp Phone Number ID | `100...` |
| `VERIFY_TOKEN` | Custom string for Webhook Verification | `my_secret_token` |
| `TELEGRAM_BOT_TOKEN` | Telegram Bot API Token | `123456:ABC-DEF...` |

> **Note**: `ADMIN_GROUP_ID` is currently hardcoded in `services/telegram_crm.py` as `-1003686781828`. If this changes, update the code.

## Running the Application
The application uses **FastAPI**. The Telegram Bot runs strictly as a background task within the FastAPI lifespan.

### On Railway (Procfile)
Your `Procfile` already contains a `bot` worker. Ensure it points to the correct app object:

```text
web: streamlit run app.py
bot: uvicorn bot:app --host 0.0.0.0 --port $PORT
```
*Note: If port is set by Railway, uvicorn picks it up. If running successfully before, no change needed.*

### Local Development
1. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```
2. Run the bot:
   ```bash
   uvicorn bot:app --reload
   ```
   *The Telegram Bot will start automatically when the server starts.*

## Features Overview
- **WhatsApp Webhook**: Listens on `POST /webhook`.
- **Telegram CRM**:
    - Admin Group: `-1003686781828`.
    - Creates a Forum Topic for each user (Phone + Name).
    - Forwards user messages to the specific topic.
    - Admin replies in the topic are sent back to the user on WhatsApp.
- **Hybrid Flow**:
    - **Bot Mode**: Searches database.
    - **Human Mode**: Forwards messages to Telegram.
    - **Session Timeout**: 60 minutes inactivity resets to Bot Mode.
