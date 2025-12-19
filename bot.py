
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
    Sanitizes Argentina Text/Sandbox numbers to the LOCAL format required by this specific Meta account.
    Target format: 54 + AreaCode (11) + 15 + Number.
    Standard International (54911...) FAILS.
    
    Logic:
    1. Remove '9' after '54'.
    2. Insert '15' after '11' if missing.
    """
    # 0. Clean basic junk
    phone = phone_number.strip().replace("+", "").replace(" ", "")

    # 1. Check if Argentina (54)
    if phone.startswith("54"):
        # 2. REMOVE '9' if present (International Mobile Token)
        # e.g. 54911... -> 5411...
        if len(phone) > 2 and phone[2] == '9':
            phone = "54" + phone[3:]
        
        # Now phone is likely 5411... (assuming BA)
        
        # 3. ADD '15' (Local Mobile Prefix) for Buenos Aires (11)
        # We need the result to be 54 11 15 xxxxxxxx
        if phone.startswith("5411"):
            # Check if '15' is already there
            # 5411 says length 4. 
            # If next chars are 15, we leave it.
            if not phone.startswith("541115"):
                # Insert 15
                phone = "541115" + phone[4:]
                
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
                stop_words = ['hola', 'start', 'hi', 'hello', 'privet', 'menu', 'test']
                if text_body.lower() in stop_words:
                    welcome_text = (
                        "👋 **Hola! Soy FiltraBot.**\n"
                        "Tu buscador de filtros al instante. 🇦🇷\n\n"
                        "🚀 *Estamos en Beta: agregamos autos nuevos cada día.*\n\n"
                        "👇 **Escribí el modelo de tu auto:**\n"
                        "(ej: Gol Trend 1.6)"
                    )
                    send_whatsapp_message(chat_id, welcome_text)
                
                else:
                    # 2. Search Database
                    try:
                        # Updated columns
                        res = supabase.table("vehicle").select("vehicle_id, brand_car, model, series_suffix, body_type, fuel_type, year_from, year_to, engine_disp_l, power_hp, engine_valves")\
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
                                # Title: {engine_disp_l}L {power_hp}CV {engine_valves} {fuel_type}
                                parts_title = []
                                if v.get('engine_disp_l'):
                                    parts_title.append(f"{v['engine_disp_l']}L")
                                if v.get('power_hp'):
                                    parts_title.append(f"{v['power_hp']}CV")
                                if v.get('engine_valves'):
                                    parts_title.append(str(v['engine_valves']))
                                if v.get('fuel_type') == 'Diesel':
                                    parts_title.append("Diesel")
                                
                                title_str = " ".join(parts_title)
                                if not title_str:
                                    title_str = "Motor Desconocido" # Fallback
                                
                                # Description: {brand_car} {model} {series_suffix} {body_type} • {year_from}-{year_to}
                                desc_parts = [
                                    v.get('brand_car', ''),
                                    v.get('model', ''),
                                    v.get('series_suffix') or '',
                                    v.get('body_type') or ''
                                ]
                                # Clean empty strings
                                desc_parts = [s for s in desc_parts if s]
                                main_desc = " ".join(desc_parts)
                                
                                y_to = str(v['year_to']) if v['year_to'] else 'Pres'
                                years = f"• {v['year_from']}-{y_to}"
                                
                                full_desc = f"{main_desc} {years}"
                                
                                list_rows.append({
                                    "id": str(v['vehicle_id']),
                                    "title": title_str[:24],
                                    "description": full_desc[:72]
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
                
                log_to_db(chat_id, 'select_vehicle', vehicle_id)

                try:
                    # 1. Fetch Vehicle Details for Title/Footer
                    v_res = supabase.table("vehicle")\
                        .select("brand_car, model, series_suffix, engine_code, engine_series")\
                        .eq("vehicle_id", vehicle_id)\
                        .single().execute()
                    
                    vehicle = v_res.data
                    if not vehicle:
                         send_whatsapp_message(chat_id, "Error: Vehículo no encontrado.")
                         continue

                    # Construct Title
                    title_parts = [vehicle.get('brand_car'), vehicle.get('model'), vehicle.get('series_suffix')]
                    display_title = " ".join([p for p in title_parts if p])
                    
                    # 2. Fetch Parts
                    parts_res = supabase.table("vehicle_part")\
                        .select("role, part(brand_filter, part_code, part_type, notes)")\
                        .eq("vehicle_id", vehicle_id)\
                        .execute()
                    
                    # Process Parts
                    # Localization map
                    type_map = {
                        'oil': 'Aceite', 
                        'air': 'Aire', 
                        'cabin': 'Habitáculo', 
                        'fuel': 'Combustible'
                    }
                    icon_map = {'oil': '🛢️', 'air': '💨', 'cabin': '❄️', 'fuel': '⛽'}
                    
                    found_parts = {}
                    
                    for item in parts_res.data:
                        part_data = item.get('part')
                        if part_data:
                            ptype = part_data.get('part_type', 'other').lower()
                            # Format: • **BRAND:** PartNumber
                            brand = part_data.get('brand_filter', 'Gene.')
                            code = part_data.get('part_code', '?')
                            pstr = f"• **{brand}:** {code}"
                            
                            if ptype not in found_parts:
                                found_parts[ptype] = []
                            found_parts[ptype].append(pstr)
                    
                    msg_body = f"🚗 **{display_title}**\n\n"
                    
                    # Order: Aceite, Aire, Habitáculo, Combustible
                    order = ['oil', 'air', 'cabin', 'fuel']
                    
                    for k in order:
                        if k in found_parts:
                            label = type_map.get(k, k.capitalize())
                            icon = icon_map.get(k, '🔧')
                            
                            msg_body += f"{icon} **{label}**\n"
                            for line in found_parts[k]:
                                msg_body += f"{line}\n"
                            msg_body += "\n" # Spacing
                    
                    if not found_parts:
                        msg_body += "⚠️ No tenemos filtros cargados para este auto aún.\n"
                    
                    # Footer
                    eng_code = vehicle.get('engine_code')
                    eng_series = vehicle.get('engine_series')
                    if eng_code or eng_series:
                        code_str = f"{eng_code or ''} {eng_series or ''}".strip()
                        msg_body += f"🔧 Motor: {code_str}"

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
