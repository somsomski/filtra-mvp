import os
import asyncio
from datetime import datetime, timedelta, timezone
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request, HTTPException, Query, Response
from pydantic import BaseModel, Field
from typing import Optional, List, Dict, Any
from supabase import create_client, Client

# Services
from services.whatsapp import send_whatsapp_message, send_interactive_list, send_interactive_buttons, sanitize_argentina_number
import services.telegram_crm as telegram_crm

# Environment Variables
SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")
VERIFY_TOKEN = os.environ.get("VERIFY_TOKEN")

# Initialize Supabase Client
if not SUPABASE_URL or not SUPABASE_KEY:
    print("WARNING: Supabase credentials missing services will fail.")
    supabase = None
else:
    supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

WELCOME_TEXT = (
    "👋 *Hola! Soy FiltraBot.*\n"
    "Tu buscador de filtros al instante. 🇦🇷\n\n"
    "👇 *Escribí el modelo de tu auto:*\n"
    "(ej: Gol Trend 1.6)"
)

# --- Lifespan for Telegram Polling ---
@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    print("Starting Telegram Bot Polling...")
    bot, dp = await telegram_crm.start_telegram()
    
    # Run polling in background
    # We use asyncio.create_task to run it without blocking FastAPI
    polling_task = asyncio.create_task(dp.start_polling(bot))
    
    yield
    
    # Shutdown
    print("Stopping Telegram Bot Polling...")
    await bot.session.close()
    polling_task.cancel()
    try:
        await polling_task
    except asyncio.CancelledError:
        pass

# Initialize FastAPI
app = FastAPI(lifespan=lifespan)

# --- Analytics Helper (Updated) ---
def log_to_db(phone: str, action_type: str, content: str, payload: Optional[Dict] = None):
    if not supabase: return
    try:
        data = {
            "phone_number": phone, 
            "action_type": action_type, 
            "content": content,
            "raw_message": payload if payload else None
        }
        supabase.table("logs").insert(data).execute()
    except Exception as e:
        print(f"[Analytics Error] {e}")
# --- Search Engine V2 ---
STOP_WORDS = ['quiero', 'busco', 'necesito', 'para', 'el', 'la', 'un', 'una', 'auto', 'coche', 'camioneta', 'filtro', 'filtros', 'motor']

SYNONYMS = {
    'vw': 'volkswagen', 'volks': 'volkswagen',
    'chevy': 'chevrolet',
    'mb': 'mercedes-benz', 'mercedes': 'mercedes-benz',
    'citroen': 'citroën',
    's-10': 's10', 's 10': 's10'
}

NUMERIC_MODEL_WHITELIST = ['206', '207', '208', '306', '307', '308', '405', '408', '504', '505', '2008', '3008', '5008', '500', 'f100', 'f150', 'ram1500', 'ram2500']

def parse_search_query(text: str) -> dict:
    """
    Parses unstructured text into structured search data (Year, Engine, Text Tokens).
    Example: "Toyota Hilux 3.0 2010" -> year=2010, engine=3.0, tokens=['toyota', 'hilux']
    """
    if not text: return {}
    
    # 1. Sanitize & Normalize
    # Remove stand-alone input like " - " but verify if it acts as a separator
    # For simplicity, we assume comma removal was done in webhook, but we do it here too just in case.
    clean_text = text.lower().replace(',', '').replace('(', '').replace(')', '').replace("'", "")
    
    # Handle synonyms (Rough replace)
    for k, v in SYNONYMS.items():
        # Using simple replace might be dangerous for short words, but 'vw' -> 'volkswagen' is usually safe.
        # Ideally token-based replacement, but this suffices for MVP.
        clean_text = clean_text.replace(k, v)
        
    tokens = clean_text.split()
    
    parsed = {
        "text_tokens": [],
        "year_filter": None,
        "engine_filter": None
    }
    
    for token in tokens:
        # A. Stop Words
        if token in STOP_WORDS:
            continue
            
        # B. Numeric Model Whitelist (Protection)
        if token in NUMERIC_MODEL_WHITELIST:
            parsed["text_tokens"].append(token)
            continue
            
        # C. Year Parser (1950-2030)
        # Check if 4 digits
        if token.isdigit() and len(token) == 4:
            val = int(token)
            if 1950 <= val <= 2030:
                parsed["year_filter"] = val
                continue
                
        # D. Displacement Parser (1.6, 2.0 etc)
        # Regex-like check: digit + [.,] + digit
        # We handle "1.6" "2,0"
        if len(token) <= 4 and ('.' in token or ',' in token):
            try:
                # Normalize 2,0 -> 2.0
                norm = token.replace(',', '.')
                # Check if it casts to float
                float(norm) 
                # Also verify it looks like an engine size (e.g. 0.8 to 8.0)
                # Avoid matching weird version numbers if any
                parsed["engine_filter"] = norm # Keep as string for DB matching if column is text, or float if numeric.
                # DB 'engine_disp_l' is numeric or string? Assuming match exact value or string.
                # Usually engine_disp_l in DB might be "1.6", let's assume direct match.
                continue
            except:
                pass
                
        # E. Fallback: Text Token
        parsed["text_tokens"].append(token)
        
    return parsed

async def search_vehicle(query_data: dict, limit: int = 12):
    """
    Executes the dynamic Supabase query.
    """
    if not supabase: return []
    
    query = supabase.table("vehicle").select("vehicle_id, brand_car, model, series_suffix, body_type, fuel_type, year_from, year_to, engine_disp_l, power_hp, engine_valves")
    
    # 1. Technical Filters
    if query_data.get("year_filter"):
        y = query_data["year_filter"]
        # Logic: year_from <= y AND (year_to >= y OR year_to IS NULL)
        # Supabase Python SDK chaining:
        query = query.lte('year_from', y).or_(f"year_to.gte.{y},year_to.is.null")
        
    if query_data.get("engine_filter"):
        # Assuming engine_disp_l is the column. 
        # Note: input might be "1.6" text, db might be numeric 1.6. 
        # .eq() usually handles auto-casting if flexible.
        query = query.eq('engine_disp_l', query_data["engine_filter"])
        
    # 2. Text Search Everywhere
    # For EACH token, it must match AT LEAST ONE of the target columns (.or_ logic per token)
    # But we want AND logic between tokens (Toyota AND Hilux).
    # So we chain .or_() filters for each token? 
    # Wait. Supabase .or_() applies to the whole query scope if not careful.
    # To do (ColA ilike %T1% OR ColB ilike %T1%) AND (ColA ilike %T2% OR ColB ilike %T2%):
    # We just chain multiple .or_() clauses. Supabase treats chained filters as AND.
    
    target_cols = [
        "brand_car", "model", "series_suffix", 
        "engine_code", "engine_series", 
        "body_type", "fuel_type", "engine_valves"
    ]
    
    for token in query_data.get("text_tokens", []):
        # Build the OR string: "col1.ilike.%tok%,col2.ilike.%tok%,..."
        or_conditions = ",".join([f"{col}.ilike.%{token}%" for col in target_cols])
        query = query.or_(or_conditions)
        
    res = query.limit(limit).execute()
    return res.data

# --- Helper for Context-Aware Navigation ---
async def send_car_actions(phone: str, vehicle_id: str):
    """
    Restores the 3 main actions for a specific vehicle without resending the full card.
    """
    try:
        # Validate ID
        if not vehicle_id or not vehicle_id.isdigit():
            # If invalid ID (e.g. legacy button), maybe just send a generic menu or "Search Again"
            await reply_and_mirror(phone, "⚠️ No pude recuperar el contexto. Por favor buscá de nuevo.")
            return

        # Fetch minimal vehicle data for context
        v_res = supabase.table("vehicle").select("brand_car, model").eq("vehicle_id", vehicle_id).single().execute()
        v = v_res.data
        if not v:
            send_whatsapp_message(phone, "⚠️ Vehículo no encontrado.")
            return

        text = f"🔙 Opciones para *{v.get('brand_car')} {v.get('model')}*:"
        
        buttons = [
            {"id": f"btn_buy_loc_{vehicle_id}", "title": "📍 Dónde comprar"},
            {"id": f"btn_menu_mech_{vehicle_id}", "title": "⚙️ Menú / Taller"},
            {"id": "btn_search_error", "title": "🔍 Buscar otro"}
        ]
        
        await reply_and_mirror(phone, text, buttons=buttons)
    except Exception as e:
        print(f"Send Car Actions Error: {e}")
        await reply_and_mirror(phone, "⚠️ Error interno recuperando menú.")

# --- Unified Response Wrapper ---
async def reply_and_mirror(phone: str, text: str, buttons: list = None, list_rows: list = None, list_title: str = None):
    """
    Sends to WhatsApp AND mirrors the exact content to Telegram.
    """
    try:
        # 1. Send to WhatsApp via services
        if buttons:
            send_interactive_buttons(phone, text, buttons)
        elif list_rows:
            send_interactive_list(phone, text, "Ver Opciones", list_title or "Resultados", list_rows)
        else:
            send_whatsapp_message(phone, text)
    except Exception as e:
        print(f"WhatsApp Send Error: {e}")

    # 2. Construct Mirror Text for Telegram
    try:
        # Use the EXACT 'text' variable passed above
        mirror_msg = f"🤖 Bot: {text}"
        
        # Append visual cues for interactive elements
        if buttons:
            btn_titles = " | ".join([f"[{b['title']}]" for b in buttons])
            mirror_msg += f"\n🔘 *Opciones:* {btn_titles}"
        
        if list_rows:
            mirror_msg += f"\n📋 *Mostró Lista:* {len(list_rows)} ítems"

        # 3. Send to Telegram
        await telegram_crm.send_log_to_admin(phone, mirror_msg, is_alert=False)
    except Exception as e:
        print(f"Telegram Mirror Error: {e}")


# --- Pydantic Models ---
class MetaWebhookPayload(BaseModel):
    object: str
    entry: List[Dict[str, Any]]

# --- Webhook Verification ---
@app.get("/webhook")
async def verify_webhook(
    mode: str = Query(..., alias="hub.mode"),
    verify_token: str = Query(..., alias="hub.verify_token"),
    challenge: str = Query(..., alias="hub.challenge")
):
    if mode == "subscribe" and verify_token == VERIFY_TOKEN:
        return Response(content=challenge, media_type="text/plain")
    raise HTTPException(status_code=403, detail="Verification failed")

# --- Main Logic ---
@app.post("/webhook")
async def webhook(payload: MetaWebhookPayload):
    """
    Main Hybrid Flow Logic
    """
    for entry in payload.entry:
        for change in entry.get('changes', []):
            value = change.get('value', {})
            messages = value.get('messages', [])
            
            if not messages:
                continue

            msg = messages[0]
            chat_id = msg['from'] # Phone number
            user_name = value.get('contacts', [{}])[0].get('profile', {}).get('name', 'Unknown')
            msg_type = msg.get('type')

            # 1. Get/Create User & Session Management
            if not supabase: continue

            user_res = supabase.table("users").select("*").eq("phone", chat_id).maybe_single().execute()
            user = user_res.data if user_res else None
            
            now = datetime.now(timezone.utc)
            
            if not user:
                # Create new user
                user = {
                    "phone": chat_id,
                    "name": user_name,
                    "status": "bot",
                    "user_type": "unknown",
                    "last_active_at": now.isoformat()
                }
                supabase.table("users").insert(user).execute()
                # Also ensure topic exists just in case
                await telegram_crm.get_or_create_topic(chat_id, user_name)
            else:
                # Session Timeout Logic
                last_active_str = user.get("last_active_at")
                current_status = user.get("status", "bot")
                
                # Check timeout for Humans
                if current_status == 'human' and last_active_str:
                    try:
                        # Handle varied timestamp formats if needed, usually ISO from Supabase
                        last_active = datetime.fromisoformat(last_active_str.replace("Z", "+00:00"))
                        if (now - last_active) > timedelta(minutes=60):
                            # Reset to bot
                            supabase.table("users").update({"status": "bot", "last_active_at": now.isoformat()}).eq("phone", chat_id).execute()
                            user['status'] = 'bot' # Update local var
                            # Log to CRM
                            await telegram_crm.send_log_to_admin(chat_id, "ℹ️ Sesión expirada. Bot reactivado.", is_alert=False)
                    except Exception as e:
                        print(f"Time check error: {e}")

                # Update Last Active
                supabase.table("users").update({"last_active_at": now.isoformat()}).eq("phone", chat_id).execute()

            # Refresh local status
            status = user.get('status', 'bot')

            # 2. Hybrid Routing
            
            # --- HUMAN MODE ---
            if status == 'human':
                text_body = ""
                if msg_type == 'text':
                    text_body = msg['text']['body']
                elif msg_type == 'interactive':
                    # Even buttons might be sent in human mode if they click old ones?
                    # Or maybe "Return to Bot" button
                    if msg['interactive']['type'] == 'button_reply':
                        if msg['interactive']['button_reply']['id'] == 'btn_return_bot':
                            # SWITCH TO BOT
                            supabase.table("users").update({"status": "bot"}).eq("phone", chat_id).execute()
                            await reply_and_mirror(chat_id, WELCOME_TEXT)
                            await telegram_crm.send_log_to_admin(chat_id, "🔄 User returned to Bot.", is_alert=False)
                            continue
                
                
                # Check keywords to break out
                keywords = ["menu", "start", "bot", "volver", "inicio"]
                if text_body and text_body.lower().strip() in keywords:
                    # Switch back to bot
                    supabase.table("users").update({"status": "bot"}).eq("phone", chat_id).execute()
                    await reply_and_mirror(chat_id, WELCOME_TEXT)
                    await telegram_crm.send_log_to_admin(chat_id, f"🔄 User detected keyword '{text_body}'. Bot Active.", is_alert=False)
                    # Stop processing
                    continue
                else:
                    # Just forward to Telegram
                    if text_body:
                        await telegram_crm.send_log_to_admin(chat_id, f"📩 {text_body}")
                    else:
                        await telegram_crm.send_log_to_admin(chat_id, f"📩 [Media/Other Message Type]")
                    # STOP here
                    continue

            # --- BOT MODE (Standard & Menu) ---
            if status in ['bot', 'menu_mode']:
                
                # A. Search Logic (Text)
                if msg_type == 'text':
                    text_body = msg['text']['body'].strip()
                    log_to_db(chat_id, 'search_text', text_body, payload=msg)
                    
                    # Sanitize for SQL/Supabase filter to prevent syntax errors
                    search_term = text_body.replace(',', '').replace('(', '').replace(')', '').replace("'", "")
                    
                    LOG_TAG = f"🔍 Buscó: {text_body}"
                    # Silent Mirroring to Telegram
                    await telegram_crm.send_log_to_admin(chat_id, LOG_TAG, is_alert=False)
                    
                    # Check "Hola" logic or Stop Words if desired, but "Search Logic" is main focus
                    stop_words_greetings = ['hola', 'start', 'hi', 'hello', 'menú', 'menu']
                    if text_body.lower() in stop_words_greetings:
                        await reply_and_mirror(chat_id, WELCOME_TEXT)
                    else:
                        # --- SEARCH ENGINE V2 ---
                        try:
                            # 1. Parse
                            q_data = parse_search_query(text_body)
                            
                            # 2. Execute
                            vehicles = await search_vehicle(q_data, limit=15) # Slightly higher limit to check count
                            
                            # 3. Handle Results
                            
                            # A. 0 Results
                            if not vehicles:
                                if status == 'menu_mode':
                                    # Treat as Feedback
                                    await telegram_crm.send_log_to_admin(chat_id, f"📝 **Feedback:** {text_body}", is_alert=True)
                                    await reply_and_mirror(chat_id, "✅ Gracias. Mensaje recibido, lo revisaremos.", buttons=[{"id": "btn_search_error", "title": "🔍 Buscar otro"}])
                                    supabase.table("users").update({"status": "bot"}).eq("phone", chat_id).execute()
                                else:
                                    reply = f"🤔 No encontré '{text_body}'.\n💡 Consejo: Probá 'Gol 1.6' o 'Hilux 2015'."
                                    buttons = [
                                        {"id": "btn_human_help", "title": "🙋‍♂️ Ayuda / Error"},
                                        {"id": "btn_search_error", "title": "🔙 Probar de nuevo"}
                                    ]
                                    await reply_and_mirror(chat_id, reply, buttons=buttons)
                            
                            # B. Too Many Results (>10)
                            elif len(vehicles) > 10:
                                # Check if single brand
                                unique_brands = list(set([v['brand_car'] for v in vehicles]))
                                
                                if len(unique_brands) == 1:
                                    # List unique models
                                    unique_models = list(set([v['model'] for v in vehicles]))
                                    models_str = "\n".join([f"• {m}" for m in unique_models[:8]]) # Limit list
                                    reply = f"🖐 Encontré muchos **{unique_brands[0]}**. Por favor escribí el modelo:\n\n{models_str}\n\n..."
                                else:
                                    reply = f"🖐 Encontré {len(vehicles)}+ vehículos. Por favor agregá el año o motor (ej: 1.6)."
                                
                                await reply_and_mirror(chat_id, reply)

                            # C. Good Range (1-10)
                            else:
                                list_rows = []
                                for v in vehicles:
                                    # Create Title
                                    parts_title = []
                                    if v.get('engine_disp_l'): parts_title.append(f"{v['engine_disp_l']}L")
                                    if v.get('power_hp'): parts_title.append(f"{v['power_hp']}CV")
                                    if v.get('engine_valves'): parts_title.append(str(v['engine_valves']))
                                    if v.get('fuel_type') == 'Diesel': parts_title.append("Diesel")
                                    
                                    title_str = " ".join(parts_title) or "Motor Desconocido"
                                    
                                    # Create Desc
                                    desc_parts = [
                                        v.get('brand_car'), v.get('model'), 
                                        v.get('series_suffix'), v.get('body_type')
                                    ]
                                    main_desc = " ".join([s for s in desc_parts if s])
                                    y_to = str(v['year_to']) if v['year_to'] else 'Pres'
                                    full_desc = f"{main_desc} • {v['year_from']}-{y_to}"
                                    
                                    list_rows.append({
                                        "id": str(v['vehicle_id']),
                                        "title": title_str[:24],
                                        "description": full_desc[:72]
                                    })
                                
                                await reply_and_mirror(
                                    chat_id,
                                    f"Encontré {len(vehicles)} opciones. Seleccioná motor:",
                                    list_rows=list_rows,
                                    list_title="Motores"
                                )

                        except Exception as e:
                            print(f"Search V2 Error: {e}")
                            if status == 'menu_mode':
                                # Failed search in menu mode -> Likely complex feedback
                                await telegram_crm.send_log_to_admin(chat_id, f"📝 **Feedback (Error Trigger):** {text_body}", is_alert=True)
                                await reply_and_mirror(chat_id, "✅ Mensaje recibido.", buttons=[{"id": "btn_search_error", "title": "🔍 Buscar otro"}])
                                supabase.table("users").update({"status": "bot"}).eq("phone", chat_id).execute()
                            else:
                                send_whatsapp_message(chat_id, "⚠️ Error en búsqueda.")

                # B. Vehicle Card (List Selection)
                elif msg_type == 'interactive' and msg['interactive']['type'] == 'list_reply':
                    vid = msg['interactive']['list_reply']['id']
                    
                    # Fetch Vehicle
                    v_res = supabase.table("vehicle").select("*").eq("vehicle_id", vid).single().execute()
                    vehicle = v_res.data
                    if not vehicle: return

                    # Log selection
                    sel_brand = vehicle.get('brand_car', '')
                    sel_model = vehicle.get('model', '')
                    sel_year = f"{vehicle.get('year_from', '?')}-{vehicle.get('year_to') or 'Pres'}"
                    await telegram_crm.send_log_to_admin(chat_id, f"👆 Seleccionó: {sel_brand} {sel_model} ({sel_year})", is_alert=False)

                    # Fetch Parts
                    parts_res = supabase.table("vehicle_part").select("role, part(brand_filter, part_code, part_type)").eq("vehicle_id", vid).execute()
                    
                    # Build Message
                    display_title = f"{vehicle.get('brand_car')} {vehicle.get('model')}"
                    msg_body = f"🚗 **{display_title}**\n\n"
                    
                    found_parts = {}
                    for item in parts_res.data:
                        part = item.get('part')
                        if part:
                            ptype = part.get('part_type', 'other').lower()
                            code = part.get('part_code', '').replace('*', '')
                            line = f"• {part.get('brand_filter')}: {code}"
                            found_parts.setdefault(ptype, []).append(line)
                    
                    type_dic = {'oil': '🛢️ Aceite', 'air': '💨 Aire', 'cabin': '❄️ Habitáculo', 'fuel': '⛽ Combustible'}
                    for k, label in type_dic.items():
                        if k in found_parts:
                            msg_body += f"{label}\n" + "\n".join(found_parts[k]) + "\n\n"
                    
                    if not found_parts: msg_body += "⚠️ Sin filtros cargados.\n"

                    # 3 Action Buttons
                    buttons = [
                        {"id": f"btn_buy_loc_{vid}", "title": "📍 Dónde comprar"},
                        {"id": f"btn_menu_mech_{vid}", "title": "⚙️ Menú / Taller"},
                        {"id": "btn_search_error", "title": "🔍 Buscar otro"} 
                    ]
                    await reply_and_mirror(chat_id, msg_body, buttons=buttons)

                # C. Button Handlers
                elif msg_type == 'interactive' and msg['interactive']['type'] == 'button_reply':
                    btn_id = msg['interactive']['button_reply']['id']
                    btn_title = msg['interactive']['button_reply']['title']
                    await telegram_crm.send_log_to_admin(chat_id, f"👆 Click: {btn_title}", is_alert=False)
                    
                    # 1. Human Help Request (No Result)
                    if btn_id == 'btn_human_help':
                        # Set human, alert admin
                        supabase.table("users").update({"status": "human"}).eq("phone", chat_id).execute()
                        await telegram_crm.send_log_to_admin(chat_id, "🚨 **Help Request**: User requested assistance.", is_alert=True)
                        await reply_and_mirror(chat_id, "✅ Ticket creado. Te contestaré en breve.", buttons=[{"id": "btn_return_bot", "title": "🤖 Volver al Bot"}])
                    
                    # 1b. Return to Bot (Exit Human Mode)
                    elif btn_id == 'btn_return_bot':
                         supabase.table("users").update({"status": "bot"}).eq("phone", chat_id).execute()
                         await reply_and_mirror(chat_id, WELCOME_TEXT)
                         await telegram_crm.send_log_to_admin(chat_id, "🔄 User returned to Bot via Button.", is_alert=False)

                    # 2. Search Error / Back
                    elif btn_id == 'btn_search_error':
                        # Reset status to bot just in case
                        supabase.table("users").update({"status": "bot"}).eq("phone", chat_id).execute()
                        await reply_and_mirror(chat_id, WELCOME_TEXT)

                    # 3. Dónde comprar
                    elif btn_id.startswith('btn_buy_loc'):
                        # Ask for location
                        supabase.table("users").update({"status": "waiting_location"}).eq("phone", chat_id).execute()
                        await reply_and_mirror(chat_id, "📍 ¿De qué Barrio o Ciudad sos?")
                    
                    # 4. Menú / Taller
                    elif btn_id.startswith('btn_menu_mech'):
                        # Set to menu_mode to capturing feedback
                        supabase.table("users").update({"status": "menu_mode"}).eq("phone", chat_id).execute()

                        # Extract VID if needed, but we pass it forward in the Back button
                        try:
                            parts = btn_id.split('_')
                            vid = parts[-1] # "btn_menu_mech_123" -> "123"
                        except:
                            vid = "0"

                        reply = "¿Eres colega? Seleccioná una opción.\n\n⚠️ ¿Encontraste un error? Simplemente escribe los detalles aquí y te responderemos."
                        sub_btns = [
                            {"id": "btn_is_mechanic", "title": "🔧 Soy Mecánico"},
                            {"id": "btn_is_seller", "title": "🏪 Soy Vendedor"},
                            {"id": f"btn_back_actions_{vid}", "title": "🔙 Volver"}
                        ]
                        await reply_and_mirror(chat_id, reply, buttons=sub_btns)

                    # 4b. Back to Actions (Soft Back)
                    elif btn_id.startswith('btn_back_actions'):
                        try:
                            vid = btn_id.split('_')[-1]
                            await send_car_actions(chat_id, vid)
                        except Exception as e:
                            await reply_and_mirror(chat_id, "⚠️ Error recuperando menú.")

                    # 5. Handler 🔧 Soy Mecánico
                    elif btn_id == 'btn_is_mechanic':
                        reply = "Ofrecemos catálogos PRO para talleres. ¿Te anoto en la lista?"
                        # Button [✅ Sí, quiero PRO]
                        # API limits 3 buttons.
                        await reply_and_mirror(chat_id, reply, buttons=[{"id": "btn_mech_confirm", "title": "✅ Sí, quiero PRO"}])

                    elif btn_id == 'btn_mech_confirm':
                        supabase.table("users").update({"user_type": "mechanic"}).eq("phone", chat_id).execute()
                        await telegram_crm.send_log_to_admin(chat_id, "👨‍🔧 **User is Mechanic** (Requested PRO)", is_alert=True)
                        await telegram_crm.send_log_to_admin(chat_id, "👨‍🔧 **User is Mechanic** (Requested PRO)", is_alert=True)
                        await reply_and_mirror(chat_id, "¡Anotado! Te contactaremos.", buttons=[{"id": "btn_search_error", "title": "🔍 Buscar repuestos"}])

                    # 6. Handler 🏪 Soy Vendedor
                    elif btn_id == 'btn_is_seller':
                        reply = "¿Quieres recibir clientes de tu zona?"
                        await reply_and_mirror(chat_id, reply, buttons=[{"id": "btn_seller_confirm", "title": "👋 Contactar"}])

                    elif btn_id == 'btn_seller_confirm':
                        supabase.table("users").update({"user_type": "seller"}).eq("phone", chat_id).execute()
                        await telegram_crm.send_log_to_admin(chat_id, "🏪 **User is Seller** (Wants Leads)", is_alert=True)
                        await telegram_crm.send_log_to_admin(chat_id, "🏪 **User is Seller** (Wants Leads)", is_alert=True)
                        await reply_and_mirror(chat_id, "¡Genial! Hablamos pronto.", buttons=[{"id": "btn_search_error", "title": "🔍 Buscar repuestos"}])

            # --- SPECIAL STATUS: Waiting Location ---
            elif status == 'waiting_location':
                if msg_type == 'text':
                    location = msg['text']['body']
                    # Save location
                    supabase.table("users").update({"location": location, "status": "bot"}).eq("phone", chat_id).execute()
                    
                    msg = f"Gracias. Te avisaremos cuando agreguemos tiendas en {location}."
                    # [Buscar otro] button is requested but can be just text instruction or button if possible.
                    # Log
                    await telegram_crm.send_log_to_admin(chat_id, f"📍 Lead Location: {location}", is_alert=False)
                    # Send button message?
                    await reply_and_mirror(chat_id, msg, buttons=[{"id": "btn_search_error", "title": "🔍 Buscar repuestos"}])

    return {"status": "ok"}
