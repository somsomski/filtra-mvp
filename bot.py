import os
import requests
from fastapi import FastAPI, Request, HTTPException, Query, Response
from pydantic import BaseModel, Field
from typing import Optional, List, Dict, Any
from supabase import create_client, Client

# Initialize FastAPI
app = FastAPI()

# Environment Variables
SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")

# Meta / WhatsApp Cloud API Credentials
META_TOKEN = os.environ.get("META_TOKEN")
PHONE_NUMBER_ID = os.environ.get("PHONE_NUMBER_ID")
VERIFY_TOKEN = os.environ.get("VERIFY_TOKEN")

# Initialize Supabase Client
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# --- Helper Function ---
def fix_argentina_number(phone_number: str) -> str:
    """
    Fixes Argentina number format for Meta Sandbox.
    Replaces 54911 with 541115 if applicable.
    """
    if phone_number.startswith("54911"):
        return phone_number.replace("54911", "541115", 1)
    return phone_number

def send_whatsapp_message(to_number: str, text: str):
    """
    Sends a text message via WhatsApp Cloud API.
    """
    if not META_TOKEN or not PHONE_NUMBER_ID:
        print("Error: Meta credentials not set.")
        return

    url = f"https://graph.facebook.com/v17.0/{PHONE_NUMBER_ID}/messages"
    headers = {
        "Authorization": f"Bearer {META_TOKEN}",
        "Content-Type": "application/json"
    }
    payload = {
        "messaging_product": "whatsapp",
        "to": to_number,
        "text": {"body": text}
    }
    
    try:
        response = requests.post(url, json=payload, headers=headers)
        response.raise_for_status()
    except requests.exceptions.RequestException as e:
        print(f"Error sending message to {to_number}: {e}")
        if hasattr(e, 'response') and e.response is not None:
            print(f"Meta API Response: {e.response.text}")

# --- Pydantic Models for Meta Webhooks ---
# These models help validat the structure, but we can also use Dict/Any for flexibility
class TextObject(BaseModel):
    body: str

class Message(BaseModel):
    from_: str = Field(..., alias="from")
    id: str
    timestamp: str
    text: Optional[TextObject] = None
    type: str

class Value(BaseModel):
    messaging_product: str
    metadata: Dict[str, Any]
    messages: Optional[List[Message]] = None
    # We might receive statuses etc, so messages is optional

class Change(BaseModel):
    value: Value
    field: str

class Entry(BaseModel):
    id: str
    changes: List[Change]

class MetaWebhookPayload(BaseModel):
    object: str
    entry: List[Entry]

# --- Routes ---

@app.get("/webhook")
async def verify_webhook(
    mode: str = Query(..., alias="hub.mode"),
    verify_token: str = Query(..., alias="hub.verify_token"),
    challenge: str = Query(..., alias="hub.challenge")
):
    """
    Verification challenge from Meta.
    """
    if mode == "subscribe" and verify_token == VERIFY_TOKEN:
        print("Webhook verified successfully!")
        return Response(content=challenge, media_type="text/plain")
    
    raise HTTPException(status_code=403, detail="Verification failed")


@app.post("/webhook")
async def webhook(payload: MetaWebhookPayload):
    """
    Handle incoming messages from WhatsApp Cloud API.
    """
    print("Received webhook payload")
    
    # Iterate through entries
    for entry in payload.entry:
        for change in entry.changes:
            value = change.value
            
            # Check if there are messages
            if not value.messages:
                continue
                
            for message in value.messages:
                # We only care about text messages for now
                if message.type != "text" or not message.text:
                    continue
                
                chat_id = message.from_  # Sender's phone number
                user_query = message.text.body
                
                print(f"Received query: {user_query} from {chat_id}")
                
                # --- Search Logic (Supabase) ---
                try:
                    # Perform search: brand_car ILIKE %query% OR model ILIKE %query%
                    response = supabase.table("vehicle").select(
                        "brand_car, model, year_from, year_to, engine_disp_l, power_hp"
                    ).or_(
                        f"brand_car.ilike.%{user_query}%,model.ilike.%{user_query}%"
                    ).limit(5).execute()
                    
                    vehicles = response.data
                    
                    # Formatting Response
                    if vehicles:
                        reply_text = "🚗 Encontré estos vehículos:\n\n"
                        for i, v in enumerate(vehicles, 1):
                            reply_text += f"{i}. {v['brand_car']} {v['model']} ({v['year_from']}-{v['year_to']}) {v['engine_disp_l']}L"
                            if v.get('power_hp'):
                                reply_text += f" {v['power_hp']}HP"
                            reply_text += "\n"
                    else:
                        reply_text = f"No encontré vehículos que coincidan con '{user_query}'."

                    # Send Reply
                    target_number = fix_argentina_number(chat_id)
                    send_whatsapp_message(target_number, reply_text)

                except Exception as e:
                    print(f"Error processing search: {e}")
                    target_number = fix_argentina_number(chat_id)
                    send_whatsapp_message(target_number, "Lo siento, hubo un error procesando tu búsqueda.")

    return {"status": "ok"}
