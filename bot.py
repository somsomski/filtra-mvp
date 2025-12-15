import os
import requests
from fastapi import FastAPI, Request, HTTPException
from pydantic import BaseModel, Field
from typing import Optional, Dict, Any
from supabase import create_client, Client

# Initialize FastAPI
app = FastAPI()

# Environment Variables
SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")
GREEN_API_ID = os.environ.get("GREEN_API_ID")
GREEN_API_TOKEN = os.environ.get("GREEN_API_TOKEN")

# Initialize Supabase Client
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# --- Helper Function ---
def send_message(chat_id: str, text: str):
    """
    Sends a text message via GreenAPI.
    """
    if not GREEN_API_ID or not GREEN_API_TOKEN:
        print("Error: GreenAPI credentials not set.")
        return

    url = f"https://api.green-api.com/waInstance{GREEN_API_ID}/SendMessage/{GREEN_API_TOKEN}"
    payload = {"chatId": chat_id, "message": text}
    headers = {'Content-Type': 'application/json'}
    
    try:
        response = requests.post(url, json=payload, headers=headers)
        response.raise_for_status()
    except requests.exceptions.RequestException as e:
        print(f"Error sending message to {chat_id}: {e}")

# --- Pydantic Models ---
class TextMessageData(BaseModel):
    textMessage: str

class MessageData(BaseModel):
    textMessageData: Optional[TextMessageData] = None

class SenderData(BaseModel):
    chatId: str

class WebhookPayload(BaseModel):
    typeWebhook: str
    senderData: Optional[SenderData] = None
    messageData: Optional[MessageData] = None

# --- Routes ---

@app.post("/webhook")
async def webhook(payload: WebhookPayload):
    """
    Main entry point for GreenAPI webhooks.
    """
    # 2. Check if typeWebhook == 'incomingMessageReceived' and if it's a text message
    if payload.typeWebhook != 'incomingMessageReceived':
        return {"status": "ignored", "reason": "not_incoming_message"}
    
    if not payload.messageData or not payload.messageData.textMessageData:
         return {"status": "ignored", "reason": "not_text_message"}
    
    # 3. Extract chatId and user_query
    chat_id = payload.senderData.chatId
    user_query = payload.messageData.textMessageData.textMessage
    
    if not user_query:
        return {"status": "ignored", "reason": "empty_text"}

    print(f"Received query: {user_query} from {chat_id}")

    # 4. Search Logic (Supabase)
    try:
        # Perform search: brand_car ILIKE %query% OR model ILIKE %query%
        response = supabase.table("vehicle").select(
            "brand_car, model, year_from, year_to, engine_disp_l, power_hp"
        ).or_(
            f"brand_car.ilike.%{user_query}%,model.ilike.%{user_query}%"
        ).limit(5).execute()
        
        vehicles = response.data
        
        # 5. Formatting Response
        if vehicles:
            reply_text = "🚗 Encontré estos vehículos:\n\n"
            for i, v in enumerate(vehicles, 1):
                # Handle potential None values for year_to (e.g. "Present") if applicable, 
                # but based on previous schema it's an int. 
                # Simple formatting:
                reply_text += f"{i}. {v['brand_car']} {v['model']} ({v['year_from']}-{v['year_to']}) {v['engine_disp_l']}L"
                if v.get('power_hp'):
                    reply_text += f" {v['power_hp']}HP"
                reply_text += "\n"
        else:
            reply_text = f"No encontré vehículos que coincidan con '{user_query}'."

        # 6. Send Reply
        send_message(chat_id, reply_text)
        
    except Exception as e:
        print(f"Error processing webhook: {e}")
        send_message(chat_id, "Lo siento, hubo un error procesando tu búsqueda.")
    
    # 7. Return status ok
    return {"status": "ok"}
