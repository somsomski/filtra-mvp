
import os
import requests
import json
from datetime import datetime
from fastapi import FastAPI, Request, HTTPException, Query, Response
from pydantic import BaseModel, Field
from typing import Optional, List, Dict, Any
from supabase import create_client, Client

# Initialize FastAPI
app = FastAPI()

# Environment Variables
SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")
META_TOKEN = os.environ.get("META_TOKEN")
PHONE_NUMBER_ID = os.environ.get("PHONE_NUMBER_ID")
VERIFY_TOKEN = os.environ.get("VERIFY_TOKEN")

# Initialize Supabase Client
if not SUPABASE_URL or not SUPABASE_KEY:
    print("WARNING: Supabase credentials missing services will fail.")
    supabase = None
else:
    supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# --- Analytics Helper ---
def log_to_db(phone: str, action_type: str, content: str):
    """
    Logs user interaction to Supabase 'logs' table.
    Schema: phone_number, action_type, content, raw_message (optional but good for debugging)
    """
    if not supabase:
        return
    try:
        data = {
            "phone_number": phone,  # User asked for 'phone_number' in payload
            "action_type": action_type, # User asked for 'action_type'
            "content": content,
            "raw_message": "Logged from bot.py" # Placeholder or could pass actual json
        }
        # Assuming table is 'logs'
        supabase.table("logs").insert(data).execute()
        print(f"[Analytics] {action_type}: {content}")
    except Exception as e:
        print(f"[Analytics Error] {e}")

# --- Helper Functions ---
def sanitize_argentina_number(phone_number: str) -> str:
    """
    Sanitizes Argentina Text/Sandbox numbers to Meta API format.
    Input format expected: 5411... or 54911...
    Target format: 549 + Area Code + Number (without 15 prefix).
    """
    # 0. Clean basic junk just in case (though usually clean from Meta)
    phone = phone_number.strip().replace("+", "").replace(" ", "")

    # 1. Check if Argentina
    if phone.startswith("54"):
        # 2. Check for '9' after '54'. If missing, insert it.
        # e.g. 5411... -> 54911...
        if len(phone) > 2 and phone[2] != '9':
            phone = "549" + phone[2:]
        
        # Now phone starts with 549...
        
        # 3. Check for '15' removal.
        # '15' is the mobile prefix usually found after area code when dialing locally.
        # But in international format (549...), it should NOT exist.
        # Meta Sandbox/User inputs might carry it.
        # Example structure: 54 9 [AreaCode] [15?] [Number]
        # Area codes: 
        #   11 (BA) -> index 3,4. Next is 5.
        #   2xx/3xx -> index 3,4,5. Next is 6.
        #   2xxx -> index 3,4,5,6. Next is 7.
        
        # Heuristic: If we find "15" at likely positions (index 5, 6, or 7), remove it.
        # CAUTION: '15' could be part of the actual number.
        # SAFE BET: Only fix known big cases or just the logic requested "If after area code is 15".
        # Given "Example: 541115... -> 54911...", let's handle the specific 11 case safely,
        # and maybe a generic "sequence 15" removal if length suggests it's extra?
        # A standard mobile number in Arg including area code is 10 digits (without 54 9).
        # e.g. 11 1234 5678 (10 digits).
        # Total Length of 54 9 XX XXXX XXXX = 13 digits.
        # If we have 15 digits (extra 15), we remove it?
        
        # Let's try matching the 5491115 pattern explicitly first (Buenos Aires)
        if phone.startswith("5491115") and len(phone) > 11:
            # Remove the '15' at index 5,6
            phone = phone[:5] + phone[7:]
        
        # TODO: Add other area codes if strictly needed, but 11 is 40% of country.
        # For now we stick to the requested logic examples.

    return phone

def send_whatsapp_message(to_number: str, text: str):
    """Sends a standard text message."""
    if not META_TOKEN or not PHONE_NUMBER_ID:
        print("Error: Meta credentials not set.")
        return

    normalized_to = sanitize_argentina_number(to_number)
    
    url = f"https://graph.facebook.com/v17.0/{PHONE_NUMBER_ID}/messages"
    headers = {"Authorization": f"Bearer {META_TOKEN}", "Content-Type": "application/json"}
    payload = {
        "messaging_product": "whatsapp",
        "to": normalized_to,
        "type": "text",
        "text": {"body": text}
    }
    
    try:
        requests.post(url, json=payload, headers=headers).raise_for_status()
    except requests.exceptions.RequestException as e:
        print(f"Error sending Text to {normalized_to}: {e}")
        if e.response is not None:
             print(f"Meta details: {e.response.text}")

def send_interactive_list(to_number: str, body_text: str, button_text: str, section_title: str, rows: List[Dict]):
    """
    Sends an Interactive List Message.
    """
    if not META_TOKEN or not PHONE_NUMBER_ID:
        return

    normalized_to = sanitize_argentina_number(to_number)
    url = f"https://graph.facebook.com/v17.0/{PHONE_NUMBER_ID}/messages"
    headers = {"Authorization": f"Bearer {META_TOKEN}", "Content-Type": "application/json"}
    
    payload = {
        "messaging_product": "whatsapp",
        "to": normalized_to,
        "type": "interactive",
        "interactive": {
            "type": "list",
            "body": {"text": body_text},
            "action": {
                "button": button_text,
                "sections": [
                    {
                        "title": section_title,
                        "rows": rows[:10]
                    }
                ]
            }
        }
    }
    
    try:
        requests.post(url, json=payload, headers=headers).raise_for_status()
    except requests.exceptions.RequestException as e:
        print(f"Error sending List to {normalized_to}: {e}")

def send_interactive_buttons(to_number: str, body_text: str, buttons: List[Dict]):
    """
    Sends an Interactive Button Message.
    """
    if not META_TOKEN or not PHONE_NUMBER_ID:
        return

    normalized_to = sanitize_argentina_number(to_number)
    url = f"https://graph.facebook.com/v17.0/{PHONE_NUMBER_ID}/messages"
    headers = {"Authorization": f"Bearer {META_TOKEN}", "Content-Type": "application/json"}
    
    formatted_buttons = []
    for btn in buttons[:3]:
        formatted_buttons.append({
            "type": "reply",
            "reply": {
                "id": btn['id'],
                "title": btn['title']
            }
        })

    payload = {
        "messaging_product": "whatsapp",
        "to": normalized_to,
        "type": "interactive",
        "interactive": {
            "type": "button",
            "body": {"text": body_text},
            "action": {
                "buttons": formatted_buttons
            }
        }
    }
    
    try:
        requests.post(url, json=payload, headers=headers).raise_for_status()
    except requests.exceptions.RequestException as e:
        print(f"Error sending Buttons to {normalized_to}: {e}")

# --- Pydantic Models ---
class MetaWebhookPayload(BaseModel):
    object: str
    entry: List[Dict[str, Any]]

# --- Logic ---

@app.get("/webhook")
async def verify_webhook(
    mode: str = Query(..., alias="hub.mode"),
    verify_token: str = Query(..., alias="hub.verify_token"),
    challenge: str = Query(..., alias="hub.challenge")
):
    if mode == "subscribe" and verify_token == VERIFY_TOKEN:
        return Response(content=challenge, media_type="text/plain")
    raise HTTPException(status_code=403, detail="Verification failed")


@app.post("/webhook")
async def webhook(payload: MetaWebhookPayload):
    """
    Main Logic Flow with fixes.
    """
    # print(f"Payload received") # Optional logging
    
    for entry in payload.entry:
        for change in entry.get('changes', []):
            value = change.get('value', {})
            messages = value.get('messages', [])
            
            if not messages:
                continue

            msg = messages[0]
            chat_id = msg['from']
            msg_type = msg.get('type')
            
            # --- SCENARIO A: Text Message (Search) ---
            if msg_type == 'text':
                text_body = msg['text']['body'].strip()
                log_to_db(chat_id, 'search_text', text_body)

                # 1. Check Keywords
                if text_body.lower() in ['hola', 'start', 'menu', 'inicio', 'hi']:
                    welcome_text = (
                        "👋 **Hola! Soy FiltraBot (Beta).**\n"
                        "Herramienta gratuita para buscar filtros en Argentina. 🇦🇷\n\n"
                        "⚠️ *Nuestra base de datos crece todos los días.*\n\n"
                        "👇 **Escribí el modelo de tu auto (ej: Gol Trend):**"
                    )
                    send_whatsapp_message(chat_id, welcome_text)
                
                else:
                    # 2. Search Database
                    try:
                        res = supabase.table("vehicle").select("vehicle_id, brand_car, model, year_from, year_to, engine_disp_l, power_hp")\
                            .or_(f"brand_car.ilike.%{text_body}%,model.ilike.%{text_body}%")\
                            .limit(12).execute()
                        
                        vehicles = res.data
                        
                        if not vehicles:
                            reply = "😕 No encontré ese modelo.\n(Recuerda que es una Beta). ¿Quieres que lo agreguemos prioridad?"
                            buttons = [{"id": "btn_support", "title": "📩 Avisar soporte"}]
                            # Check if interactive messages are failing, fallback logic isn't here but we strictly use interactive as requested
                            send_interactive_buttons(chat_id, reply, buttons)
                        
                        elif len(vehicles) > 10:
                            send_whatsapp_message(chat_id, f"🖐 Encontré demasiados autos ({len(vehicles)}+). Por favor, sé más específico (ej: agregar motor o año).")
                        
                        else:
                            # 1-10 Results -> Interactive List
                            list_rows = []
                            for v in vehicles:
                                title = f"{v['brand_car']} {v['model']}"[:24]
                                y_to = str(v['year_to']) if v['year_to'] else 'Pres'
                                desc = f"{v['year_from']}-{y_to} {v['engine_disp_l'] or ''}L {v.get('power_hp') or ''}HP"
                                list_rows.append({
                                    "id": str(v['vehicle_id']),
                                    "title": title,
                                    "description": desc[:72]
                                })
                            
                            send_interactive_list(
                                chat_id, 
                                "Seleccioná tu versión exacta:", 
                                "Ver Modelos", 
                                "Resultados", 
                                list_rows
                            )

                    except Exception as e:
                        print(f"Search Error: {e}")
                        send_whatsapp_message(chat_id, "Lo siento, hubo un error buscando. Intenta más tarde.")

            # --- SCENARIO B: Interactive List Reply (Vehicle Selected) ---
            elif msg_type == 'interactive' and msg['interactive']['type'] == 'list_reply':
                selection = msg['interactive']['list_reply']
                vehicle_id = selection['id']
                vehicle_title = selection['title']
                
                log_to_db(chat_id, 'select_vehicle', f"{vehicle_id} - {vehicle_title}")

                try:
                    parts_res = supabase.table("vehicle_part")\
                        .select("role, part(brand_filter, part_code, part_type, notes)")\
                        .eq("vehicle_id", vehicle_id)\
                        .execute()
                    
                    # Process Parts
                    type_icons = {'oil': '🛢', 'air': '💨', 'fuel': '⛽', 'cabin': '❄️'}
                    found_parts = {}
                    
                    for item in parts_res.data:
                        part_data = item.get('part')
                        if part_data:
                            ptype = part_data.get('part_type', 'other').lower()
                            pstr = f"{part_data.get('brand_filter')} {part_data.get('part_code')}"
                            if ptype not in found_parts:
                                found_parts[ptype] = []
                            found_parts[ptype].append(pstr)
                    
                    msg_body = f"🚗 **{vehicle_title}**\n\n"
                    
                    for k in ['oil', 'air', 'fuel', 'cabin']:
                        if k in found_parts:
                            icon = type_icons.get(k, '🔧')
                            filters_str = ", ".join(found_parts[k])
                            msg_body += f"{icon} {k.capitalize()}: {filters_str}\n"
                    
                    if not found_parts:
                        msg_body += "⚠️ No tenemos filtros cargados para este auto aún.\n"

                    buttons = [
                        {"id": "btn_buy", "title": "📍 Dónde comprar?"},
                        {"id": "btn_b2b", "title": "🔧 Soy Taller"},
                        {"id": "btn_error", "title": "📝 Reportar Error"}
                    ]
                    send_interactive_buttons(chat_id, msg_body, buttons)

                except Exception as e:
                    print(f"Details Error: {e}")
                    send_whatsapp_message(chat_id, "Error recuperando datos del vehículo.")

            # --- SCENARIO C: Interactive Button Reply (Actions) ---
            elif msg_type == 'interactive' and msg['interactive']['type'] == 'button_reply':
                btn_id = msg['interactive']['button_reply']['id']
                log_to_db(chat_id, 'click_button', btn_id)

                support_number = "5491132273621"

                if btn_id == 'btn_buy':
                    link = f"https://wa.me/{support_number}?text=Busco_vendedor_zona_para_mi_auto"
                    send_whatsapp_message(chat_id, f"🗺 Para buscar vendedores en tu zona, avísanos aquí: {link}")
                
                elif btn_id == 'btn_b2b':
                    link = f"https://wa.me/{support_number}?text=Soy_taller_y_quiero_sumar_mi_catalogo"
                    send_whatsapp_message(chat_id, f"🤝 Para sumar tu catálogo, escribinos aquí: {link}")
                
                elif btn_id in ['btn_error', 'btn_support']:
                    link = f"https://wa.me/{support_number}?text=Error_en_auto_o_dato_faltante"
                    send_whatsapp_message(chat_id, f"🙏 Reportar error aquí: {link}")

    return {"status": "ok"}
