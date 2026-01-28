import os
import asyncio
import re
import json
from datetime import datetime, timedelta, timezone
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request, HTTPException, Query, Response
from fastapi.responses import JSONResponse
from collections import deque
from pydantic import BaseModel, Field
from typing import Optional, List, Dict, Any
from supabase import AsyncClient, create_async_client

# Services
from services.whatsapp import send_whatsapp_message, send_interactive_list, send_interactive_buttons, sanitize_argentina_number
import services.telegram_crm as telegram_crm

# Environment Variables
SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")
VERIFY_TOKEN = os.environ.get("VERIFY_TOKEN")

# Initialize Supabase Client
supabase: AsyncClient = None

if not SUPABASE_URL or not SUPABASE_KEY:
    print("WARNING: Supabase credentials missing services will fail.")

WELCOME_TEXT = (
    "👋 **¡Hola! Soy FiltraBot (Beta).** 🇦🇷\n\n"
    "🔍 Buscador de filtros y repuestos.\n"
    "🚧 **Estamos construyendo la base:** Cargamos catálogos nuevos todos los días. Si no encontrás algo, ¡avisanos!\n\n"
    "👇 **Escribí el modelo para probar:**\n"
    "_(ej: Gol Trend 1.6 o Amarok 2015)_"
)

SHORT_WELCOME = "✅ Listo. Escribí el modelo (ej: *Gol 1.6* o *Hilux 2015*) para buscar."

# Cache processed message IDs to prevent retry loops
PROCESSED_MSG_IDS = deque(maxlen=1000)

# --- Lifespan for Telegram Polling ---
@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    print("Starting Telegram Bot Polling...")
    
    # Init Supabase
    global supabase
    if SUPABASE_URL and SUPABASE_KEY:
        supabase = await create_async_client(SUPABASE_URL, SUPABASE_KEY)
    
    bot, dp = await telegram_crm.start_telegram()
    # Share Supabase client with services
    telegram_crm.supabase = supabase

    
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
async def log_to_db(phone: str, action_type: str, content: str, payload: Optional[Dict] = None):
    """
    Unified logging function for analytics.
    Maps everything to the strict 'logs' table schema.
    """
    if not supabase: return
    try:
        data = {
            "phone_number": phone, 
            "action_type": action_type, 
            "content": content[:200] if content else "", # Truncate for safety
            "raw_message": payload if payload else None,
            "direction": "analytics",
            "status": "saved"
        }
        await supabase.table("logs").insert(data).execute()
    except Exception as e:
        print(f"[Analytics Error] {e}")

async def log_user_event(phone: str, action: str, details: str):
    """Wrapper for backward compatibility."""
    await log_to_db(phone, action, details)

async def update_user_metadata(phone: str, updates: dict):
    if not supabase: return
    try:
        res = await supabase.table("users").select("metadata").eq("phone", phone).maybe_single().execute()
        current = res.data.get("metadata") or {}
        # Ensure dict
        if isinstance(current, str):
             try: current = json.loads(current)
             except: current = {}
        
        current.update(updates)
        await supabase.table("users").update({"metadata": current}).eq("phone", phone).execute()
    except Exception as e:
        print(f"[Metadata Error] {e}")

def get_message_content(msg: dict) -> str:
    """Extract content from Text or Button Reply"""
    mtype = msg.get('type')
    if mtype == 'text':
        return msg['text']['body']
    elif mtype == 'interactive':
        itype = msg['interactive']['type']
        if itype == 'button_reply':
            # We treat the TITLE as the user input for surveys
            return msg['interactive']['button_reply']['title']
        elif itype == 'list_reply':
            return msg['interactive']['list_reply']['title']
    return ""
# --- Search Engine V2 ---
STOP_WORDS = ['quiero', 'busco', 'necesito', 'para', 'el', 'la', 'un', 'una', 'auto', 'coche', 'camioneta', 'filtro', 'filtros', 'motor']

SYNONYMS = {
    'vw': 'volkswagen', 'volks': 'volkswagen',
    'chevy': 'chevrolet',
    'mb': 'mercedes-benz', 'mercedes': 'mercedes-benz',
    'citroen': 'citroën',
    's-10': 's10', 's 10': 's10'
}

NUMERIC_MODEL_WHITELIST = ['206', '207', '208', '306', '307', '308', '405', '408', '504', '505', '3008', '5008', '500', 'f100', 'f150', 'ram1500', 'ram2500']

def parse_search_query(text: str) -> dict:
    """
    Parses unstructured text into structured search data (Year, Engine, Text Tokens).
    Example: "Toyota Hilux 3.0 2010" -> year=2010, engine=3.0, tokens=['toyota', 'hilux']
    """
    if not text: return {}
    
    # 1. Sanitize & Normalize
    # Pre-process: Converts "1,6" to "1.6" via regex so the sanitizer doesn't destroy it.
    text_pre = re.sub(r'(\d+),(\d+)', r'\1.\2', text.lower())
    
    # Remove stand-alone input like " - " but verify if it acts as a separator
    clean_text = text_pre.replace(',', '').replace('(', '').replace(')', '').replace("'", "")
    
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
                
        # D. Displacement Parser (1.6, 2.0, 2l, 2.0l etc)
        # Regex-like check: digit + [.,] + digit OR digit + 'l'
        # We handle "1.6", "2,0", "2l", "2.0l"
        token_lower = token.lower()
        if len(token) <= 5: # Small enough
             # Strip 'l' if present at end
             is_liter = False
             if token_lower.endswith('l'):
                 token_clean = token_lower[:-1]
                 is_liter = True
             else:
                 token_clean = token_lower

             # Check format
             if any(c in token_clean for c in ['.', ',']) or is_liter or token_clean.isdigit():
                 try:
                     norm = token_clean.replace(',', '.')
                     # Check if float
                     val_float = float(norm)
                     
                     # If it was just an integer "2" or "2l", make it "2.0"
                     # Logic: if no dot in norm, append .0
                     if '.' not in norm:
                         norm += ".0"
                     
                     # Verify reasonable engine range (0.5 to 16.0)
                     if 0.5 <= val_float <= 16.0:
                         parsed["engine_filter"] = norm
                         continue
                 except:
                     pass
                
        # E. Fallback: Text Token
        parsed["text_tokens"].append(token)
        
    return parsed

def to_accent_regex(text: str) -> str:
    """
    Converts text to accent-insensitive regex pattern.
    Example: "mio" -> "m[ií]o"
    """
    mapping = {
        'a': '[aá]', 'e': '[eé]', 'i': '[ií]', 'o': '[oó]', 'u': '[uúü]', 
        'n': '[nñ]'
    }
    return "".join([mapping.get(c, c) for c in text])

async def search_vehicle(query_data: dict, limit: int = 12):
    """
    Executes the dynamic Supabase query.
    """
    if not supabase: return []
    
    query = supabase.table("vehicle").select("vehicle_id, brand_car, model, series_suffix, body_type, fuel_type, year_from, year_to, engine_disp_l, power_hp, engine_valves")
    
    # 1. Technical Filters
    if query_data.get("year_filter"):
        y = query_data["year_filter"]
        # Logic: (year_from <= y AND (year_to >= y OR year_to IS NULL)) OR model ilike %y%
        # We construct a raw OR filter for the entire year logic block
        # Supabase syntax: "and(year_from.lte.y,or(year_to.gte.y,year_to.is.null)),model.ilike.%y%"
        # Actually, to combine complex AND/OR groups in PostgREST is tricky with the py wrapper's simple methods.
        # But we can use the `or_` method on the top level with the raw string syntax.
        # The condition we want is: condition_year_range OR condition_model_name
        
        # condition_year_range = and(year_from.lte.Y,or(year_to.gte.Y,year_to.is.null))
        # condition_model_name = model.ilike.*Y*
        
        raw_filter = f"and(year_from.lte.{y},or(year_to.gte.{y},year_to.is.null)),model.ilike.*{y}*"
        query = query.or_(raw_filter)
        
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
        # Sanitize token for SQL/Regex safety
        safe_token = token.replace("'", "").replace("%", "")
        
        # Calculate accent-insensitive regex for ALL tokens first
        # Example: "mio" -> "m[ií]o"
        fuzzy_token = to_accent_regex(safe_token)
        
        if len(safe_token) > 3:
            # LONG TOKENS: Accent-Insensitive Regex Search
            # Example: "Megane" -> matches "Megane", "Mégane"
            or_conditions = ",".join([f"{col}.imatch.{fuzzy_token}" for col in target_cols])
            query = query.or_(or_conditions)
        else:
            # SHORT TOKENS: Strict Search (Word Boundary) -> \ytoken\y
            # Example: "Mio" -> "\ym[ií]o\y" matches "Clio Mío" but NOT "Kamion"
            regex_pattern = f"\\y{fuzzy_token}\\y"
            
            # PostgREST syntax: col.imatch.pattern
            or_conditions = ",".join([f"{col}.imatch.{regex_pattern}" for col in target_cols])
            query = query.or_(or_conditions)
        
    res = await query.limit(limit).execute()
    return res.data

async def process_search_request(chat_id: str, text_body: str, status: str):
    """
    Centralized Search Handler used by Text inputs and List selections.
    """
    try:
        # 1. Parse
        q_data = parse_search_query(text_body)
        
        # 2. Execute
        vehicles = await search_vehicle(q_data, limit=15)
        
        # 3. Handle Results
        
        # A. 0 Results
        if not vehicles:
            if status == 'menu_mode':
                await telegram_crm.send_log_to_admin(chat_id, f"📝 **Feedback:** {text_body}", priority='high')
                await reply_and_mirror(chat_id, "✅ Gracias. Mensaje recibido, lo revisaremos.", buttons=[{"id": "btn_search_error", "title": "🔍 Buscar otro"}])
                await supabase.table("users").update({"status": "bot"}).eq("phone", chat_id).execute()
            else:
                # Log Empty
                await log_user_event(chat_id, "search_empty", text_body)

                reply = f"🤔 No encontré '{text_body}' en la base.\n\nComo estamos en Beta, es posible que falte ese modelo. ¿Querés que lo agregue a la lista de prioridades?"
                buttons = [
                    {"id": f"btn_add_missing_{text_body[:20]}", "title": "➕ Sumar a la base"},
                    {"id": "btn_human_help", "title": "💬 Hablar con alguien"},
                    {"id": "btn_search_retry", "title": "🔙 Probar de nuevo"}
                ]
                await reply_and_mirror(chat_id, reply, buttons=buttons)
        
        # B. Too Many Results (>10)
        elif len(vehicles) > 10:
            unique_brands = list(set([v['brand_car'] for v in vehicles]))
            unique_models = sorted(list(set([v['model'] for v in vehicles])))
            
            # CASE A: Single Brand, Multi Model (Intermediate Selector)
            if len(unique_brands) == 1 and len(unique_models) > 1:
                
                # If small list (2-10 items), send INTERACTIVE LIST
                if 2 <= len(unique_models) <= 10:
                    list_rows = []
                    brand = unique_brands[0]
                    for m in unique_models:
                        # ID format: cmd_search_Brand Model
                        # Limit title to 24 chars
                        list_rows.append({
                            "id": f"cmd_search_{brand} {m}",
                            "title": m[:24],
                            "description": "Ver versiones"
                        })
                    
                    await reply_and_mirror(
                        chat_id, 
                        f"Encontré modelos de {brand}. Seleccioná uno:", 
                        list_rows=list_rows, 
                        list_title="Modelos"
                    )
                else:
                    # Too many models (>10), fall back to text list
                    models_str = "\n".join([f"• {m}" for m in unique_models[:8]])
                    reply = f"🖐 Encontré muchos **{unique_brands[0]}**. Por favor escribí el modelo:\n\n{models_str}\n\n..."
                    await reply_and_mirror(chat_id, reply)

            # CASE B: Single Brand, Single Model (Refinement Loop check)
            # CASE C: Mixed Brands
            else:
                reply = f"🖐 Encontré muchos vehículos. Por favor escribí **Modelo + Año** (ej: *Hilux 2015*)."
                await reply_and_mirror(chat_id, reply)

        # C. Good Range (1-10)
        else:
            list_rows = []
            for v in vehicles:
                # 1. Fuel Badge Logic
                f_raw = (v.get('fuel_type') or '').lower()
                if 'diesel' in f_raw:
                    fuel_badge = "🛢️ Diesel"
                elif 'gnc' in f_raw or 'gas' in f_raw:
                    fuel_badge = "🔥 GNC"
                elif 'nafta' in f_raw or 'benz' in f_raw:
                    fuel_badge = "⛽"
                else:
                    fuel_badge = ""

                # 2. Build Title (Engine + HP + Valves + Fuel)
                title_parts = []
                if v.get('engine_disp_l'): 
                    title_parts.append(f"{v['engine_disp_l']}L")
                if v.get('power_hp'): 
                    title_parts.append(f"{v['power_hp']}CV")
                if v.get('engine_valves'):
                    title_parts.append(str(v['engine_valves']))
                
                # Append Fuel priority (after engine specs)
                if fuel_badge:
                    title_parts.append(fuel_badge)

                title_str = " ".join(title_parts) 
                if not title_str.strip():
                    title_str = "Ver Detalles" # Fallback
                
                # 3. Description (Brand Model Suffix • Year)
                # Body type explicitly excluded per requirements
                y_to = str(v['year_to']) if v.get('year_to') else 'Pres'
                year_str = f"{v.get('year_from')}-{y_to}" if v.get('year_from') else ""
                
                # Merge Model + Suffix
                model_full = v.get('model', '')
                if v.get('series_suffix'):
                    model_full += f" {v['series_suffix']}"
                
                desc_parts = [
                    v.get('brand_car'), 
                    model_full,
                    year_str
                ]
                # Filter empty and join
                full_desc = " • ".join([str(p) for p in desc_parts if p])
                
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
            
            # Log Success
            await log_user_event(chat_id, "search_found", text_body)

    except Exception as e:
        print(f"Process Search Error: {e}")
        await send_whatsapp_message(chat_id, "⚠️ Error en motor de búsqueda.")

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
        v_res = await supabase.table("vehicle").select("brand_car, model").eq("vehicle_id", vehicle_id).single().execute()
        v = v_res.data
        if not v:
            await send_whatsapp_message(phone, "⚠️ Vehículo no encontrado.")
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
            await send_interactive_buttons(phone, text, buttons)
        elif list_rows:
            await send_interactive_list(phone, text, "Ver Opciones", list_title or "Resultados", list_rows)
        else:
            await send_whatsapp_message(phone, text)
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
        await telegram_crm.send_log_to_admin(phone, mirror_msg, priority='log')
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
            
            # --- NEW: STALE FILTER ---
            try:
                if 'messages' in value:
                    msg = value['messages'][0]
                    raw_ts = msg.get('timestamp')
                    if raw_ts:
                        msg_dt = datetime.fromtimestamp(int(raw_ts), tz=timezone.utc)
                        if (datetime.now(timezone.utc) - msg_dt).total_seconds() > 300:
                            print(f"⌛ Ignoring STALE message from {msg_dt}")
                            return JSONResponse(content={"status": "ignored_stale"}, status_code=200)
            except Exception as e:
                print(f"Time check error: {e}")
            # -------------------------
            
            # --- DEDUPLICATION ---
            try:
                if 'messages' in value:
                    msg_id = value['messages'][0].get('id')
                    
                    # If ID was already processed, stop immediately (return 200 OK)
                    if msg_id and msg_id in PROCESSED_MSG_IDS:
                        print(f"🔁 Ignoring retry: {msg_id}")
                        return JSONResponse(content={"status": "ignored_duplicate"}, status_code=200)
                    
                    if msg_id:
                        PROCESSED_MSG_IDS.append(msg_id)
            except Exception as e:
                print(f"Dedup error: {e}")
            # ---------------------

            messages = value.get('messages', [])
            
            if not messages:
                continue

            msg = messages[0]
            chat_id = msg['from'] # Phone number
            user_name = value.get('contacts', [{}])[0].get('profile', {}).get('name', 'Unknown')
            msg_type = msg.get('type')

            # 1. Get/Create User & Session Management
            if not supabase: continue

            user_res = await supabase.table("users").select("*").eq("phone", chat_id).maybe_single().execute()
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
                await supabase.table("users").insert(user).execute()
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
                            await supabase.table("users").update({"status": "bot", "last_active_at": now.isoformat()}).eq("phone", chat_id).execute()
                            user['status'] = 'bot' # Update local var
                            # Log to CRM (Silent/Log priority, no user alert needed)
                            await telegram_crm.send_log_to_admin(chat_id, "ℹ️ Sesión expirada. Bot reactivado.", priority='log')
                    except Exception as e:
                        print(f"Time check error: {e}")

                # Update Last Active
                await supabase.table("users").update({"last_active_at": now.isoformat()}).eq("phone", chat_id).execute()

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
                            await supabase.table("users").update({"status": "bot"}).eq("phone", chat_id).execute()
                            await reply_and_mirror(chat_id, WELCOME_TEXT)
                            await telegram_crm.send_log_to_admin(chat_id, "🔄 User returned to Bot.", priority='log')
                            continue
                
                
                # Check keywords to break out
                keywords = ["menu", "start", "bot", "volver", "inicio"]
                if text_body and text_body.lower().strip() in keywords:
                    # Switch back to bot
                    await supabase.table("users").update({"status": "bot"}).eq("phone", chat_id).execute()
                    await reply_and_mirror(chat_id, WELCOME_TEXT)
                    await telegram_crm.send_log_to_admin(chat_id, f"🔄 User detected keyword '{text_body}'. Bot Active.", priority='log')
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

            # --- SMART SURVEYS (Refined Logic) ---
            
            input_val = get_message_content(msg).strip()
            
            # 1. Global Cancel Check
            # Check keywords or explicit cancel button
            is_cancel_btn = (msg_type == 'interactive' and 
                             msg.get('interactive', {}).get('button_reply', {}).get('id') == 'btn_cancel_survey')
            
            cancel_keywords = ['cancelar', 'salir', 'menu', 'basta', 'chau', 'volver']
            
            if (input_val.lower() in cancel_keywords) or is_cancel_btn:
                # Reset to bot
                await supabase.table("users").update({"status": "bot"}).eq("phone", chat_id).execute()
                await telegram_crm.send_log_to_admin(chat_id, "🚫 User cancelled survey.", priority='log')
                await reply_and_mirror(chat_id, SHORT_WELCOME, buttons=[{"id": "btn_search_error", "title": "🔍 Buscar repuesto"}])
                continue

            # A. Mechanic Flow
            if status == 'waiting_mechanic_priority':
                if input_val:
                    priority_val = 'speed' if 'velocidad' in input_val.lower() or 'rocket' in input_val.lower() else 'price'
                    await update_user_metadata(chat_id, {"priority": priority_val})
                    
                    await supabase.table("users").update({"status": "waiting_mechanic_name"}).eq("phone", chat_id).execute()
                    
                    # Ask Name WITH CANCEL
                    btns = [{"id": "btn_cancel_survey", "title": "🔙 Cancelar"}]
                    await reply_and_mirror(chat_id, "📝 ¿Cuál es el nombre de tu Taller?", buttons=btns)
                    continue

            elif status == 'waiting_mechanic_name':
                if input_val:
                    await update_user_metadata(chat_id, {"shop_name": input_val})
                    
                    # Finalize & Update SQL Column 'name'
                    await supabase.table("users").update({
                        "status": "bot", 
                        "user_type": "mechanic",
                        "name": input_val
                    }).eq("phone", chat_id).execute()
                    
                    # Log Event
                    await log_user_event(chat_id, "lead_mechanic", f"Shop: {input_val}")

                    await telegram_crm.update_topic_title(chat_id, 'bot', 'mechanic')
                    await telegram_crm.send_log_to_admin(chat_id, f"👨‍🔧 Mechanic Registered: {input_val}", priority='high')
                    
                    await reply_and_mirror(chat_id, "✅ **¡Perfil Guardado!**\n\nGracias por sumarte a la Beta. Estamos conectando los primeros talleres con proveedores. Te avisaremos apenas activemos tu cuenta PRO.", buttons=[{"id": "btn_search_error", "title": "🔍 Buscar repuesto"}])
                    continue

            # B. Seller Flow
            elif status == 'waiting_seller_name':
                if input_val:
                    # Save Name
                    await update_user_metadata(chat_id, {"shop_name": input_val})
                    # Update SQL Column 'name', move to Location
                    await supabase.table("users").update({
                        "status": "waiting_seller_location",
                        "name": input_val
                    }).eq("phone", chat_id).execute()
                    
                    # Ask Location
                    btns = [{"id": "btn_cancel_survey", "title": "🔙 Cancelar"}]
                    await reply_and_mirror(chat_id, "🏪 Alta Vendedor: ¿En qué Ciudad o Zona está tu depósito?\n_(Escribí tu ubicación)_", buttons=btns)
                    continue

            elif status == 'waiting_seller_location':
                if input_val:
                    await update_user_metadata(chat_id, {"location": input_val})
                    
                    # Update Location Column
                    await supabase.table("users").update({
                        "status": "waiting_seller_logistics",
                        "location": input_val
                    }).eq("phone", chat_id).execute()
                    
                    # Ask Logistics (Buttons)
                    btns = [
                        {"id": "btn_logistics_ship", "title": "📦 Hago Envíos"},
                        {"id": "btn_logistics_pickup", "title": "🏪 Solo Retiro"},
                        {"id": "btn_cancel_survey", "title": "🔙 Cancelar"}
                    ]
                    await reply_and_mirror(chat_id, "🚚 ¿Hacés envíos?", buttons=btns)
                    continue
            
            elif status == 'waiting_seller_logistics':
                if input_val:
                    # Input is button title
                    logistics_val = 'envios' if 'envíos' in input_val.lower() else 'retiro'
                    
                    await update_user_metadata(chat_id, {"logistics": logistics_val})
                    # Finalize
                    await supabase.table("users").update({"status": "bot", "user_type": "seller"}).eq("phone", chat_id).execute()
                    
                    # Log Event
                    await log_user_event(chat_id, "lead_seller", f"Logistics: {logistics_val}")

                    await telegram_crm.update_topic_title(chat_id, 'bot', 'seller')
                    await telegram_crm.send_log_to_admin(chat_id, f"🏪 Seller Registered: {logistics_val}", priority='high')
                    
                    await reply_and_mirror(chat_id, "✅ **¡Datos Recibidos!**\n\nEstamos armando la red de distribución. Te contactaremos personalmente para validar tu zona y empezar a derivarte pedidos.", buttons=[{"id": "btn_search_error", "title": "🔍 Buscar repuesto"}])
                    continue

            # C. Buyer Flow
            elif status == 'waiting_buyer_location':
                if input_val:
                    # Save location to column AND metadata
                    await update_user_metadata(chat_id, {"location": input_val})
                    
                    await supabase.table("users").update({
                        "location": input_val, 
                        "status": "waiting_buyer_urgency"
                    }).eq("phone", chat_id).execute()
                    
                    # Ask Urgency (Refined Copy & Buttons)
                    btns = [
                        {"id": "btn_urgency_high", "title": "🔥 Lo necesito YA"},
                        {"id": "btn_urgency_normal", "title": "💰 Busco Precio"},
                        {"id": "btn_cancel_survey", "title": "🔙 Cancelar"}
                    ]
                    await reply_and_mirror(chat_id, "⏳ Para filtrar opciones: ¿Buscás el mejor PRECIO o necesitás el repuesto YA (Cerca)?", buttons=btns)
                    continue

            elif status == 'waiting_buyer_urgency':
                if input_val:
                    # "Lo necesito YA" vs "Busco Precio"
                    is_urgent = 'ya' in input_val.lower() or 'fuego' in input_val.lower() or '🔥' in input_val
                    
                    await update_user_metadata(chat_id, {"urgency": input_val})
                    await supabase.table("users").update({"status": "bot"}).eq("phone", chat_id).execute()
                    
                    tag = "🔥" if is_urgent else "💸"
                    
                    # Alert actions
                    if is_urgent:
                        await telegram_crm.send_log_to_admin(chat_id, f"🔥 Buyer Urgency: {input_val}", priority='high')
                    else:
                        await telegram_crm.send_log_to_admin(chat_id, f"💸 Buyer Inquiry: {input_val}", priority='normal')
                    
                    # Log Event
                    await log_user_event(chat_id, "lead_buyer", f"Urgency: {input_val}")

                    await reply_and_mirror(chat_id, f"{tag} **¡Pedido Recibido!**\n\nComo estamos en **Fase Beta**, un especialista de nuestra red revisará tu pedido manualmente y te contactará con opciones reales en breve.\n\n🏎️ ¡Gracias por ayudarnos a mejorar!", buttons=[{"id": "btn_search_error", "title": "🔍 Buscar otro"}])
                    continue

            # --- BOT MODE (Standard & Menu) ---
            if status in ['bot', 'menu_mode']:
                
                # A. Search Logic (Text)
                if msg_type == 'text':
                    text_body = msg['text']['body'].strip()
                    
                    # LOGGING LOGIC
                    if status == 'menu_mode':
                        # Feedback/Error Reporting
                        await log_to_db(chat_id, 'user_feedback', text_body, payload=msg)
                    else:
                        # Standard Search
                        await log_to_db(chat_id, 'search_text', text_body, payload=msg)
                    
                    # Sanitize for SQL/Supabase filter to prevent syntax errors
                    search_term = text_body.replace(',', '').replace('(', '').replace(')', '').replace("'", "")
                    
                    LOG_TAG = f"🔍 Buscó: {text_body}"
                    # Silent Mirroring to Telegram
                    await telegram_crm.send_log_to_admin(chat_id, LOG_TAG, priority='log')
                    
                    # Check "Hola" logic or Stop Words if desired, but "Search Logic" is main focus
                    stop_words_greetings = ['hola', 'start', 'hi', 'hello', 'menú', 'menu']
                    if text_body.lower() in stop_words_greetings:
                        await reply_and_mirror(chat_id, WELCOME_TEXT)
                    else:
                        # --- SEARCH ENGINE V2 (Refactored) ---
                        await process_search_request(chat_id, text_body, status)

                # B. Vehicle Card (List Selection)
                elif msg_type == 'interactive' and msg['interactive']['type'] == 'list_reply':
                    vid = msg['interactive']['list_reply']['id']
                    
                    # Check if it's a "Search Command" (Model Selector)
                    if vid.startswith("cmd_search_"):
                        # Extract query (e.g., "Toyota Hilux")
                        # Format: "cmd_search_Brand Model"
                        new_query = vid.replace("cmd_search_", "")
                        
                        # Log click
                        await telegram_crm.send_log_to_admin(chat_id, f"👆 List Selection: {new_query}", priority='log')
                        
                        # Treat as text search
                        await process_search_request(chat_id, new_query, status)
                        continue
                    
                    # --- VEHICLE DETAILS ---
                    # Fetch Vehicle
                    v_res = await supabase.table("vehicle").select("*").eq("vehicle_id", vid).single().execute()
                    vehicle = v_res.data
                    if not vehicle: continue

                    # Log selection
                    sel_brand = vehicle.get('brand_car', '')
                    sel_model = vehicle.get('model', '')
                    sel_year = f"{vehicle.get('year_from', '?')}-{vehicle.get('year_to') or 'Pres'}"
                    await telegram_crm.send_log_to_admin(chat_id, f"👆 Seleccionó: {sel_brand} {sel_model} ({sel_year})", priority='log')

                    # Fetch Parts
                    parts_res = await supabase.table("vehicle_part").select("role, part(brand_filter, part_code, part_type)").eq("vehicle_id", vid).execute()
                    
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
                    
                    # Add Mechanic/Pro Tech Info (Engine Series/Code)
                    # UX: Subtle footer
                    eng_code = vehicle.get('engine_code')
                    eng_series = vehicle.get('engine_series')
                    
                    if eng_code or eng_series:
                        tech_info = []
                        if eng_series: tech_info.append(f"Serie: {eng_series}")
                        if eng_code: tech_info.append(f"Motor: {eng_code}")
                        
                        msg_body += f"\n🔧 {' | '.join(tech_info)}"

                    # 3 Action Buttons
                    buttons = [
                        {"id": f"btn_buy_loc_{vid}", "title": "📍 Dónde comprar"},
                        {"id": f"btn_menu_mech_{vid}", "title": "⚙️ Menú / Taller"},
                        {"id": "btn_search_error", "title": "🔍 Buscar otro"} 
                    ]
                    await reply_and_mirror(chat_id, msg_body, buttons=buttons)

                # C. General Button Handlers
                elif msg_type == 'interactive' and msg['interactive']['type'] == 'button_reply':
                    btn_id = msg['interactive']['button_reply']['id']
                    btn_title = msg['interactive']['button_reply']['title']
                    
                    await telegram_crm.send_log_to_admin(chat_id, f"👆 Click: {btn_title}", priority='log')

                    # 1. Add Missing
                    if btn_id.startswith("btn_add_missing_"):
                        model_name = btn_id.split("btn_add_missing_")[1]
                        
                        await log_user_event(chat_id, "request_missing", model_name)
                        await telegram_crm.send_log_to_admin(chat_id, f"📝 Request to ADD: {model_name}", priority='normal')
                        
                        await reply_and_mirror(chat_id, f"📝 ¡Anotado!\n\nYa le avisé al equipo. Voy a buscar los filtros de {model_name} y los cargo lo antes posible. ¡Gracias! 🚀")
                        await send_whatsapp_message(chat_id, SHORT_WELCOME)
                        continue

                    # 2. Human Help (Global & Fallback)
                    elif btn_id == "btn_human_help":
                        await supabase.table("users").update({"status": "human"}).eq("phone", chat_id).execute()
                        await telegram_crm.update_topic_title(chat_id, 'human', user.get('user_type', 'unknown'))
                        
                        await log_user_event(chat_id, "human_mode_req", "User requested support")
                        await telegram_crm.send_log_to_admin(chat_id, "👤 User requested HUMAN support.", priority='high')
                        
                        await reply_and_mirror(chat_id, "👤 Modo Humano activado.\n\nDejanos tu consulta escrita acá abajo 👇 y te responderemos en cuanto estemos online.", buttons=[{"id": "btn_return_bot", "title": "🤖 Volver al Bot"}])
                        continue

                    # 2b. Return to Bot
                    elif btn_id == 'btn_return_bot':
                         await supabase.table("users").update({"status": "bot"}).eq("phone", chat_id).execute()
                         await reply_and_mirror(chat_id, SHORT_WELCOME)
                         await telegram_crm.send_log_to_admin(chat_id, "🔄 User returned to Bot via Button.", priority='log')

                    # 3. Search Retry / Error
                    elif btn_id == "btn_search_retry" or btn_id == "btn_search_error":
                        await send_whatsapp_message(chat_id, SHORT_WELCOME)
                        # Reset status
                        await supabase.table("users").update({"status": "bot"}).eq("phone", chat_id).execute()
                        continue

                    # 4. Dónde comprar
                    elif btn_id.startswith('btn_buy_loc'):
                        await supabase.table("users").update({"status": "waiting_buyer_location"}).eq("phone", chat_id).execute()
                        await reply_and_mirror(chat_id, "📍 ¿De qué Barrio o Ciudad sos?")
                    
                    # 5. Menú / Taller
                    elif btn_id.startswith('btn_menu_mech'):
                        await supabase.table("users").update({"status": "menu_mode"}).eq("phone", chat_id).execute()
                        try:
                            vid = btn_id.split('_')[-1]
                        except:
                            vid = "0"

                        reply = "¿Eres colega? Seleccioná una opción.\n\n⚠️ ¿Encontraste un error? Simplemente escribe los detalles aquí y te responderemos."
                        sub_btns = [
                            {"id": "btn_is_mechanic", "title": "🔧 Soy Mecánico"},
                            {"id": "btn_is_seller", "title": "🏪 Soy Vendedor"},
                            {"id": f"btn_back_actions_{vid}", "title": "🔙 Volver"}
                        ]
                        await reply_and_mirror(chat_id, reply, buttons=sub_btns)

                    elif btn_id.startswith('btn_back_actions'):
                        try:
                            vid = btn_id.split('_')[-1]
                            await send_car_actions(chat_id, vid)
                        except Exception as e:
                            await reply_and_mirror(chat_id, "⚠️ Error recuperando menú.")

                    # START MECHANIC FLOW
                    elif btn_id == 'btn_is_mechanic':
                        await log_user_event(chat_id, "funnel_start", "mechanic_registration")
                        await supabase.table("users").update({"status": "waiting_mechanic_priority"}).eq("phone", chat_id).execute()
                        btns = [
                            {"id": "btn_prio_speed", "title": "🚀 Velocidad"},
                            {"id": "btn_prio_price", "title": "💰 Precio"},
                            {"id": "btn_cancel_survey", "title": "🔙 Cancelar"}
                        ]
                        await reply_and_mirror(chat_id, "🚀 Para optimizar tu perfil: ¿Qué priorizás habitualmente?\n_(Seleccioná o escribí tu respuesta)_", buttons=btns)

                    # START SELLER FLOW
                    elif btn_id == 'btn_is_seller':
                         await log_user_event(chat_id, "funnel_start", "seller_registration")
                         await supabase.table("users").update({"status": "waiting_seller_name"}).eq("phone", chat_id).execute()
                         # Ask Name (Step 1)
                         btns = [{"id": "btn_cancel_survey", "title": "🔙 Cancelar"}]
                         await reply_and_mirror(chat_id, "🏪 Alta de Vendedor: ¿Cómo se llama tu Negocio/Repuestera?", buttons=btns)



    return {"status": "ok"}
