
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
def log_event(phone: str, action: str, content: str):
    """
    Logs user interaction to Supabase 'logs' table.
    """
    if not supabase:
        return
    try:
        data = {
            "phone": phone,
            "action": action,
            "content": content,
            # created_at is automatic in DB usually, but we can rely on DB defaults
        }
        supabase.table("logs").insert(data).execute()
        print(f"[Analytics] {action}: {content}")
    except Exception as e:
        print(f"[Analytics Error] {e}")

# --- Helper Functions ---
def fix_argentina_number(phone_number: str) -> str:
    """
    Fixes Argentina Sandbox numbers:
    5491144445555 -> 541144445555 (Removes the 9 after 54)
    Only applies if starts with 549.
    """
    if phone_number.startswith("549") and len(phone_number) > 10:
        # Remove the '9' at index 2 (3rd char)
        return "54" + phone_number[3:]
    return phone_number

def send_whatsapp_message(to_number: str, text: str):
    """Sends a standard text message."""
    if not META_TOKEN or not PHONE_NUMBER_ID:
        print("Error: Meta credentials not set.")
        return

    normalized_to = fix_argentina_number(to_number)
    
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

def send_interactive_list(to_number: str, body_text: str, button_text: str, section_title: str, rows: List[Dict]):
    """
    Sends an Interactive List Message.
    rows should be [{'id': '...', 'title': '...', 'description': '...'}, ...]
    Max 10 rows.
    """
    if not META_TOKEN or not PHONE_NUMBER_ID:
        return

    normalized_to = fix_argentina_number(to_number)
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
                        "rows": rows[:10]  # Ensure max 10
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
    buttons should be [{'id': '...', 'title': '...'}, ...]
    Max 3 buttons.
    """
    if not META_TOKEN or not PHONE_NUMBER_ID:
        return

    normalized_to = fix_argentina_number(to_number)
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
    Main Logic Flow:
    Scenario A: Text (Search) -> Welcome OR List/NotFound
    Scenario B: List Reply (Select) -> Details + Buttons
    Scenario C: Button Reply (Action) -> Text with WA Link
    """
    print(f"Payload received")
    
    for entry in payload.entry:
        for change in entry.get('changes', []):
            value = change.get('value', {})
            messages = value.get('messages', [])
            
            if not messages:
                continue

            msg = messages[0]
            chat_id = msg['from']  # The user's phone number
            msg_type = msg.get('type')
            
            # --- SCENARIO A: Text Message (Search) ---
            if msg_type == 'text':
                text_body = msg['text']['body'].strip()
                log_event(chat_id, 'search_text', text_body)

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
                    # We limit to 12 to know if >10
                    try:
                        res = supabase.table("vehicle").select("vehicle_id, brand_car, model, year_from, year_to, engine_disp_l, power_hp")\
                            .or_(f"brand_car.ilike.%{text_body}%,model.ilike.%{text_body}%")\
                            .limit(12).execute()
                        
                        vehicles = res.data
                        
                        if not vehicles:
                            # 0 Results
                            reply = "😕 No encontré ese modelo.\n(Recuerda que es una Beta). ¿Quieres que lo agreguemos prioridad?"
                            buttons = [{"id": "btn_support", "title": "📩 Avisar soporte"}]
                            send_interactive_buttons(chat_id, reply, buttons)
                        
                        elif len(vehicles) > 10:
                            # > 10 Results
                            send_whatsapp_message(chat_id, f"🖐 Encontré demasiados autos ({len(vehicles)}+). Por favor, sé más específico (ej: agregar motor o año).")
                        
                        else:
                            # 1-10 Results -> Interactive List
                            list_rows = []
                            for v in vehicles:
                                # Format: Toyota Hilux (2015-Pres) 2.8L
                                title = f"{v['brand_car']} {v['model']}"[:24] # Limit title length
                                y_to = str(v['year_to']) if v['year_to'] else 'Pres'
                                desc = f"{v['year_from']}-{y_to} {v['engine_disp_l'] or ''}L {v.get('power_hp') or ''}HP"
                                list_rows.append({
                                    "id": str(v['vehicle_id']),
                                    "title": title,
                                    "description": desc[:72] # Limit desc length
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
                
                log_event(chat_id, 'select_vehicle', f"{vehicle_id} - {vehicle_title}")

                try:
                    # 1. Get Vehicle Details
                    # 2. Get Parts (Filters)
                    # We can do two queries or one join if supported.
                    # Getting parts with join:
                    parts_res = supabase.table("vehicle_part")\
                        .select("role, part(brand_filter, part_code, part_type, notes)")\
                        .eq("vehicle_id", vehicle_id)\
                        .execute()
                    
                    # Process Parts
                    parts_msg = ""
                    type_icons = {'oil': '🛢', 'air': '💨', 'fuel': '⛽', 'cabin': '❄️'}
                    
                    found_parts = {} # type -> []
                    
                    for item in parts_res.data:
                        part_data = item.get('part')
                        if part_data:
                            ptype = part_data.get('part_type', 'other').lower()
                            pstr = f"{part_data.get('brand_filter')} {part_data.get('part_code')}"
                            if ptype not in found_parts:
                                found_parts[ptype] = []
                            found_parts[ptype].append(pstr)
                    
                    # Construct Message
                    msg_body = f"🚗 **{vehicle_title}**\n\n"
                    
                    # Order: Oil, Air, Fuel, Cabin
                    for k in ['oil', 'air', 'fuel', 'cabin']:
                        if k in found_parts:
                            icon = type_icons.get(k, '🔧')
                            # Join multiple filters with comma
                            filters_str = ", ".join(found_parts[k])
                            msg_body += f"{icon} {k.capitalize()}: {filters_str}\n"
                    
                    if not found_parts:
                        msg_body += "⚠️ No tenemos filtros cargados para este auto aún.\n"

                    # Send Result with Buttons
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
                log_event(chat_id, 'click_button', btn_id)

                support_number = "5491132273621" # Target for links

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
