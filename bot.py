import os
import asyncio
import re
import json
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

# --- Analytics / DB Helpers ---
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

async def update_user_metadata(phone: str, updates: dict):
    if not supabase: return
    try:
        res = supabase.table("users").select("metadata").eq("phone", phone).maybe_single().execute()
        current = res.data.get("metadata") or {}
        # Ensure dict
        if isinstance(current, str):
             try: current = json.loads(current)
             except: current = {}
        
        current.update(updates)
        supabase.table("users").update({"metadata": current}).eq("phone", phone).execute()
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
    if not text: return {}
    text_pre = re.sub(r'(\d+),(\d+)', r'\1.\2', text.lower())
    clean_text = text_pre.replace(',', '').replace('(', '').replace(')', '').replace("'", "")
    for k, v in SYNONYMS.items():
        clean_text = clean_text.replace(k, v)
        
    tokens = clean_text.split()
    parsed = {"text_tokens": [], "year_filter": None, "engine_filter": None}
    
    for token in tokens:
        if token in STOP_WORDS: continue
        if token in NUMERIC_MODEL_WHITELIST:
            parsed["text_tokens"].append(token)
            continue
            
        if token.isdigit() and len(token) == 4:
            val = int(token)
            if 1950 <= val <= 2030:
                parsed["year_filter"] = val
                continue
                
        token_lower = token.lower()
        if len(token) <= 5:
             is_liter = False
             if token_lower.endswith('l'):
                 token_clean = token_lower[:-1]
                 is_liter = True
             else:
                 token_clean = token_lower

             if any(c in token_clean for c in ['.', ',']) or is_liter or token_clean.isdigit():
                 try:
                     norm = token_clean.replace(',', '.')
                     val_float = float(norm)
                     if '.' not in norm: norm += ".0"
                     if 0.5 <= val_float <= 16.0:
                         parsed["engine_filter"] = norm
                         continue
                 except:
                     pass
        parsed["text_tokens"].append(token)
    return parsed

def to_accent_regex(text: str) -> str:
    mapping = {'a': '[aá]', 'e': '[eé]', 'i': '[ií]', 'o': '[oó]', 'u': '[uúü]', 'n': '[nñ]'}
    return "".join([mapping.get(c, c) for c in text])

async def search_vehicle(query_data: dict, limit: int = 12):
    if not supabase: return []
    query = supabase.table("vehicle").select("vehicle_id, brand_car, model, series_suffix, body_type, fuel_type, year_from, year_to, engine_disp_l, power_hp, engine_valves, metadata")
    
    if query_data.get("year_filter"):
        y = query_data["year_filter"]
        raw_filter = f"and(year_from.lte.{y},or(year_to.gte.{y},year_to.is.null)),model.ilike.*{y}*"
        query = query.or_(raw_filter)
        
    if query_data.get("engine_filter"):
        query = query.eq('engine_disp_l', query_data["engine_filter"])
        
    target_cols = ["brand_car", "model", "series_suffix", "engine_code", "engine_series", "body_type", "fuel_type", "engine_valves"]
    
    for token in query_data.get("text_tokens", []):
        safe_token = token.replace("'", "").replace("%", "")
        fuzzy_token = to_accent_regex(safe_token)
        if len(safe_token) > 3:
            or_conditions = ",".join([f"{col}.imatch.{fuzzy_token}" for col in target_cols])
        else:
            regex_pattern = f"\\y{fuzzy_token}\\y"
            or_conditions = ",".join([f"{col}.imatch.{regex_pattern}" for col in target_cols])
        query = query.or_(or_conditions)
        
    res = query.limit(limit).execute()
    return res.data

async def process_search_request(chat_id: str, text_body: str, status: str):
    try:
        q_data = parse_search_query(text_body)
        vehicles = await search_vehicle(q_data, limit=15)
        
        if not vehicles:
            if status == 'menu_mode':
                await telegram_crm.send_log_to_admin(chat_id, f"📝 **Feedback:** {text_body}", priority='log')
                await reply_and_mirror(chat_id, "✅ Gracias. Mensaje recibido, lo revisaremos.", buttons=[{"id": "btn_search_error", "title": "🔍 Buscar otro"}])
                supabase.table("users").update({"status": "bot"}).eq("phone", chat_id).execute()
            else:
                reply = f"🤔 No encontré '{text_body}'.\n💡 Consejo: Probá 'Gol 1.6' o 'Hilux 2015'."
                buttons = [
                    {"id": "btn_human_help", "title": "🙋‍♂️ Ayuda / Error"},
                    {"id": "btn_search_error", "title": "🔙 Probar de nuevo"}
                ]
                await reply_and_mirror(chat_id, reply, buttons=buttons)
        
        elif len(vehicles) > 10:
            unique_brands = list(set([v['brand_car'] for v in vehicles]))
            unique_models = sorted(list(set([v['model'] for v in vehicles])))
            
            if len(unique_brands) == 1 and len(unique_models) > 1:
                if 2 <= len(unique_models) <= 10:
                    list_rows = []
                    brand = unique_brands[0]
                    for m in unique_models:
                        list_rows.append({
                            "id": f"cmd_search_{brand} {m}",
                            "title": m[:24],
                            "description": "Ver versiones"
                        })
                    await reply_and_mirror(chat_id, f"Encontré modelos de {brand}. Seleccioná uno:", list_rows=list_rows, list_title="Modelos")
                else:
                    models_str = "\n".join([f"• {m}" for m in unique_models[:8]])
                    reply = f"🖐 Encontré muchos **{unique_brands[0]}**. Por favor escribí el modelo:\n\n{models_str}\n\n..."
                    await reply_and_mirror(chat_id, reply)
            else:
                reply = f"🖐 Encontré muchos vehículos. Por favor escribí **Modelo + Año** (ej: *Hilux 2015*)."
                await reply_and_mirror(chat_id, reply)

        else: # 1-10 Results
            list_rows = []
            for v in vehicles:
                f_raw = (v.get('fuel_type') or '').lower()
                fuel_badge = "🛢️ Diesel" if 'diesel' in f_raw else ("🔥 GNC" if ('gnc' in f_raw or 'gas' in f_raw) else ("⛽" if ('content' in f_raw or 'nafta' in f_raw or 'benz' in f_raw) else ""))
                
                title_parts = []
                if v.get('engine_disp_l'): title_parts.append(f"{v['engine_disp_l']}L")
                if v.get('power_hp'): title_parts.append(f"{v['power_hp']}CV")
                if v.get('engine_valves'): title_parts.append(str(v['engine_valves']))
                if fuel_badge: title_parts.append(fuel_badge)

                title_str = " ".join(title_parts) 
                if not title_str.strip(): title_str = "Ver Detalles"
                
                y_to = str(v['year_to']) if v.get('year_to') else 'Pres'
                year_str = f"{v.get('year_from')}-{y_to}" if v.get('year_from') else ""
                
                model_full = v.get('model', '')
                if v.get('series_suffix'): model_full += f" {v['series_suffix']}"
                
                desc_parts = [v.get('brand_car'), model_full, year_str]
                full_desc = " • ".join([str(p) for p in desc_parts if p])
                
                list_rows.append({
                    "id": str(v['vehicle_id']),
                    "title": title_str[:24],
                    "description": full_desc[:72]
                })
            
            await reply_and_mirror(chat_id, f"Encontré {len(vehicles)} opciones. Seleccioná motor:", list_rows=list_rows, list_title="Motores")

    except Exception as e:
        print(f"Process Search Error: {e}")
        send_whatsapp_message(chat_id, "⚠️ Error en motor de búsqueda.")

# --- Context-Aware Navigation ---
async def send_car_actions(phone: str, vehicle_id: str):
    try:
        if not vehicle_id or not vehicle_id.isdigit():
            await reply_and_mirror(phone, "⚠️ No pude recuperar el contexto. Por favor buscá de nuevo.")
            return

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

# --- Response Wrapper ---
async def reply_and_mirror(phone: str, text: str, buttons: list = None, list_rows: list = None, list_title: str = None):
    try:
        if buttons:
            send_interactive_buttons(phone, text, buttons)
        elif list_rows:
            send_interactive_list(phone, text, "Ver Opciones", list_title or "Resultados", list_rows)
        else:
            send_whatsapp_message(phone, text)
    except Exception as e:
        print(f"WhatsApp Send Error: {e}")

    try:
        mirror_msg = f"🤖 Bot: {text}"
        if buttons:
            btn_titles = " | ".join([f"[{b['title']}]" for b in buttons])
            mirror_msg += f"\n🔘 *Opciones:* {btn_titles}"
        if list_rows:
            mirror_msg += f"\n📋 *Mostró Lista:* {len(list_rows)} ítems"
        
        # Mirror logs are usually low priority
        await telegram_crm.send_log_to_admin(phone, mirror_msg, priority='log')
    except Exception as e:
        print(f"Telegram Mirror Error: {e}")

# --- Pydantic Models ---
class MetaWebhookPayload(BaseModel):
    object: str
    entry: List[Dict[str, Any]]

# --- Webhook Verification ---
@app.get("/webhook")
async def verify_webhook(mode: str = Query(..., alias="hub.mode"), verify_token: str = Query(..., alias="hub.verify_token"), challenge: str = Query(..., alias="hub.challenge")):
    if mode == "subscribe" and verify_token == VERIFY_TOKEN:
        return Response(content=challenge, media_type="text/plain")
    raise HTTPException(status_code=403, detail="Verification failed")

# --- Main Logic ---
@app.post("/webhook")
async def webhook(payload: MetaWebhookPayload):
    for entry in payload.entry:
        for change in entry.get('changes', []):
            value = change.get('value', {})
            messages = value.get('messages', [])
            
            if not messages: continue

            msg = messages[0]
            chat_id = msg['from']
            user_name = value.get('contacts', [{}])[0].get('profile', {}).get('name', 'Unknown')
            msg_type = msg.get('type')

            if not supabase: continue

            # 1. User & Session
            user_res = supabase.table("users").select("*").eq("phone", chat_id).maybe_single().execute()
            user = user_res.data if user_res else None
            now = datetime.now(timezone.utc)
            
            if not user:
                user = {
                    "phone": chat_id,
                    "name": user_name,
                    "status": "bot",
                    "user_type": "unknown",
                    "last_active_at": now.isoformat()
                }
                supabase.table("users").insert(user).execute()
                await telegram_crm.get_or_create_topic(chat_id, user_name)
            else:
                last_active_str = user.get("last_active_at")
                current_status = user.get("status", "bot")
                if current_status == 'human' and last_active_str:
                    try:
                        last_active = datetime.fromisoformat(last_active_str.replace("Z", "+00:00"))
                        if (now - last_active) > timedelta(minutes=60):
                            supabase.table("users").update({"status": "bot", "last_active_at": now.isoformat()}).eq("phone", chat_id).execute()
                            user['status'] = 'bot'
                            await telegram_crm.send_log_to_admin(chat_id, "ℹ️ Sesión expirada. Bot reactivado.", priority='log')
                    except Exception as e: pass
                supabase.table("users").update({"last_active_at": now.isoformat()}).eq("phone", chat_id).execute()

            status = user.get('status', 'bot')

            # 2. Hybrid Routing
            
            # --- HUMAN MODE ---
            if status == 'human':
                text_body = get_message_content(msg)
                
                # Check keywords to break out
                keywords = ["menu", "start", "bot", "volver", "inicio"]
                
                # Check for Return Button specifically
                is_return_btn = (msg_type == 'interactive' and msg.get('interactive', {}).get('button_reply', {}).get('id') == 'btn_return_bot')
                
                if (text_body and text_body.lower().strip() in keywords) or is_return_btn:
                    supabase.table("users").update({"status": "bot"}).eq("phone", chat_id).execute()
                    await update_user_metadata(chat_id, {}) # Optional cleanup or keep metadata
                    await reply_and_mirror(chat_id, WELCOME_TEXT)
                    await telegram_crm.send_log_to_admin(chat_id, f"🔄 User returned to Bot.", priority='normal')
                    if not is_return_btn:
                         await telegram_crm.update_topic_title(chat_id, 'bot', user.get('user_type', 'unknown'))
                    continue
                else:
                    await telegram_crm.send_log_to_admin(chat_id, f"📩 {text_body or '[Media]'}", priority='normal')
                    continue

            # --- SMART SURVEYS (New States) ---
            
            input_val = get_message_content(msg).strip()

            # A. Mechanic Flow
            if status == 'waiting_mechanic_priority':
                if input_val:
                    priority_val = 'speed' if 'velocidad' in input_val.lower() or 'rocket' in input_val.lower() else 'price'
                    await update_user_metadata(chat_id, {"priority": priority_val})
                    
                    supabase.table("users").update({"status": "waiting_mechanic_name"}).eq("phone", chat_id).execute()
                    await reply_and_mirror(chat_id, "📝 ¿Cuál es el nombre de tu Taller?")
                    continue

            elif status == 'waiting_mechanic_name':
                if input_val:
                    await update_user_metadata(chat_id, {"shop_name": input_val})
                    # Finalize
                    supabase.table("users").update({"status": "bot", "user_type": "mechanic"}).eq("phone", chat_id).execute()
                    await telegram_crm.update_topic_title(chat_id, 'bot', 'mechanic')
                    await telegram_crm.send_log_to_admin(chat_id, f"👨‍🔧 Mechanic Registered: {input_val}", priority='high')
                    
                    await reply_and_mirror(chat_id, "✅ Registro Provisorio OK. Te avisaremos cuando habilitemos tu cuenta.", buttons=[{"id": "btn_search_error", "title": "🔍 Buscar repuesto"}])
                    continue

            # B. Seller Flow
            elif status == 'waiting_seller_location':
                if input_val:
                    await update_user_metadata(chat_id, {"location": input_val})
                    
                    supabase.table("users").update({"status": "waiting_seller_logistics"}).eq("phone", chat_id).execute()
                    await reply_and_mirror(chat_id, "🚚 ¿Hacés envíos? (Si/No/Radio...)")
                    continue
            
            elif status == 'waiting_seller_logistics':
                if input_val:
                    await update_user_metadata(chat_id, {"logistics": input_val})
                    # Finalize
                    supabase.table("users").update({"status": "bot", "user_type": "seller"}).eq("phone", chat_id).execute()
                    await telegram_crm.update_topic_title(chat_id, 'bot', 'seller')
                    await telegram_crm.send_log_to_admin(chat_id, f"🏪 Seller Registered: {input_val}", priority='high')
                    
                    await reply_and_mirror(chat_id, "✅ Gracias. Te contactaremos para validar tu cuenta.", buttons=[{"id": "btn_search_error", "title": "🔍 Buscar repuesto"}])
                    continue

            # C. Buyer Flow
            elif status == 'waiting_buyer_location':
                if input_val:
                    # Save location to column AND metadata
                    await update_user_metadata(chat_id, {"location": input_val})
                    supabase.table("users").update({"location": input_val, "status": "waiting_buyer_urgency"}).eq("phone", chat_id).execute()
                    
                    # Ask Urgency
                    btns = [
                        {"id": "btn_urgency_high", "title": "🔥 URGENTE"},
                        {"id": "btn_urgency_normal", "title": "💸 Cotizar / Mañana"}
                    ]
                    await reply_and_mirror(chat_id, "⏳ Última: ¿Qué urgencia tenés?", buttons=btns)
                    continue

            elif status == 'waiting_buyer_urgency':
                if input_val:
                    is_urgent = 'urgent' in input_val.lower() or 'fuego' in input_val.lower() or 'fire' in input_val.lower() or '🔥' in input_val
                    priority_level = 'high' if is_urgent else 'normal'
                    tag = "🔥" if is_urgent else "💸"
                    
                    await update_user_metadata(chat_id, {"urgency": input_val})
                    supabase.table("users").update({"status": "bot"}).eq("phone", chat_id).execute()
                    
                    # Alert actions
                    if is_urgent:
                        # "Set Title to 🔥" -> We can't easily change strict icon, but we utilize high priority log
                        await telegram_crm.send_log_to_admin(chat_id, f"🔥 Buyer Urgency: {input_val}", priority='high')
                    else:
                        await telegram_crm.send_log_to_admin(chat_id, f"💸 Buyer Inquiry: {input_val}", priority='normal')

                    await reply_and_mirror(chat_id, f"{tag} Solicitud enviada. Buscando proveedores en tu zona...", buttons=[{"id": "btn_search_error", "title": "🔍 Buscar otro"}])
                    continue


            # --- BOT MODE (Standard) ---
            if status in ['bot', 'menu_mode']:
                
                # A. Search Logic (Text)
                if msg_type == 'text':
                    text_body = msg['text']['body'].strip()
                    log_to_db(chat_id, 'search_text', text_body, payload=msg)
                    
                    stop_words_greetings = ['hola', 'start', 'hi', 'hello', 'menú', 'menu']
                    if text_body.lower() in stop_words_greetings:
                        await reply_and_mirror(chat_id, WELCOME_TEXT)
                    else:
                        search_term = text_body.replace(',', '').replace('(', '').replace(')', '').replace("'", "")
                        await telegram_crm.send_log_to_admin(chat_id, f"🔍 Buscó: {text_body}", priority='log')
                        await process_search_request(chat_id, text_body, status)

                # B. Interactive List Selection (Vehicle)
                elif msg_type == 'interactive' and msg['interactive']['type'] == 'list_reply':
                    vid = msg['interactive']['list_reply']['id']
                    
                    if vid.startswith("cmd_search_"):
                        new_query = vid.replace("cmd_search_", "")
                        await telegram_crm.send_log_to_admin(chat_id, f"👆 List Selection: {new_query}", priority='log')
                        await process_search_request(chat_id, new_query, status)
                        continue

                    # Fetch Vehicle
                    v_res = supabase.table("vehicle").select("*").eq("vehicle_id", vid).single().execute()
                    vehicle = v_res.data
                    if not vehicle: continue

                    sel_desc = f"{vehicle.get('brand_car')} {vehicle.get('model')}"
                    await telegram_crm.send_log_to_admin(chat_id, f"👆 Selected: {sel_desc}", priority='log')

                    parts_res = supabase.table("vehicle_part").select("role, part(brand_filter, part_code, part_type)").eq("vehicle_id", vid).execute()
                    
                    msg_body = f"🚗 **{sel_desc}**\n\n"
                    found_parts = {}
                    for item in parts_res.data:
                        part = item.get('part')
                        if part:
                            ptype = part.get('part_type', 'other').lower()
                            line = f"• {part.get('brand_filter')}: {part.get('part_code')}"
                            found_parts.setdefault(ptype, []).append(line)
                    
                    type_dic = {'oil': '🛢️ Aceite', 'air': '💨 Aire', 'cabin': '❄️ Habitáculo', 'fuel': '⛽ Combustible'}
                    for k, label in type_dic.items():
                        if k in found_parts:
                            msg_body += f"{label}\n" + "\n".join(found_parts[k]) + "\n\n"
                    
                    if not found_parts: msg_body += "⚠️ Sin filtros cargados.\n"
                    
                    # Tech Info Footer
                    meta = vehicle.get('metadata') or {}
                    if isinstance(meta, str):
                        try: meta = json.loads(meta)
                        except: meta = {}
                    eng_code = meta.get('engine_code') or vehicle.get('engine_code')
                    eng_series = meta.get('engine_series')
                    if eng_code or eng_series:
                        info = [s for s in [eng_series, eng_code] if s]
                        msg_body += f"\n🔧 {' | '.join(info)}"

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
                    await telegram_crm.send_log_to_admin(chat_id, f"👆 Click: {btn_title}", priority='log')

                    if btn_id == 'btn_human_help':
                        supabase.table("users").update({"status": "human"}).eq("phone", chat_id).execute()
                        await telegram_crm.send_log_to_admin(chat_id, "🚨 User requested Help.", priority='high')
                        await telegram_crm.update_topic_title(chat_id, "human", user.get('user_type', 'unknown'))
                        await reply_and_mirror(chat_id, "✅ Ticket creado. Te contestaré en breve.", buttons=[{"id": "btn_return_bot", "title": "🤖 Volver al Bot"}])
                    
                    elif btn_id == 'btn_return_bot':
                         supabase.table("users").update({"status": "bot"}).eq("phone", chat_id).execute()
                         await reply_and_mirror(chat_id, WELCOME_TEXT)
                         await telegram_crm.update_topic_title(chat_id, "bot", user.get('user_type', 'unknown'))

                    elif btn_id == 'btn_search_error':
                        supabase.table("users").update({"status": "bot"}).eq("phone", chat_id).execute()
                        await reply_and_mirror(chat_id, WELCOME_TEXT)

                    # START BUYER FLOW
                    elif btn_id.startswith('btn_buy_loc'):
                        supabase.table("users").update({"status": "waiting_buyer_location"}).eq("phone", chat_id).execute()
                        await reply_and_mirror(chat_id, "📍 ¿De qué Barrio o Ciudad sos?")
                    
                    # SELECTOR MENU
                    elif btn_id.startswith('btn_menu_mech'):
                        supabase.table("users").update({"status": "menu_mode"}).eq("phone", chat_id).execute()
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
                        supabase.table("users").update({"status": "waiting_mechanic_priority"}).eq("phone", chat_id).execute()
                        btns = [
                            {"id": "btn_prio_speed", "title": "🚀 Velocidad"},
                            {"id": "btn_prio_price", "title": "💰 Precio"}
                        ]
                        await reply_and_mirror(chat_id, "👨‍🔧 Taller PRO: ¿Qué priorizás más?", buttons=btns)

                    # START SELLER FLOW
                    elif btn_id == 'btn_is_seller':
                         supabase.table("users").update({"status": "waiting_seller_location"}).eq("phone", chat_id).execute()
                         await reply_and_mirror(chat_id, "🏪 Alta Vendedor: ¿En qué Ciudad/Zona operás?")

    return {"status": "ok"}
