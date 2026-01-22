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
                        if msg['interactive']['button_reply']['id'] == 'cmd_return_bot':
                            # SWITCH TO BOT
                            supabase.table("users").update({"status": "bot"}).eq("phone", chat_id).execute()
                            await reply_and_mirror(chat_id, "🤖 Bot reactivado. ¿En qué te ayudo?")
                            await telegram_crm.send_log_to_admin(chat_id, "🔄 User returned to Bot.", is_alert=False)
                            continue
                
                # Check keywords to break out
                keywords = ["menu", "start", "hola", "bot"]
                if text_body and text_body.lower().strip() in keywords:
                    # Switch back to bot
                    supabase.table("users").update({"status": "bot"}).eq("phone", chat_id).execute()
                    await reply_and_mirror(chat_id, "🤖 Bot reactivado.")
                    await telegram_crm.send_log_to_admin(chat_id, f"🔄 User detected keyword '{text_body}'. Bot Active.", is_alert=False)
                    # Proceed to bot logic below? 
                    # Prompt says: "If matches keywords like 'Menu' or 'Start'" -> Switch?
                    # "If status == 'human': Do NOT search. Forward text... unless it matches keywords"
                    # We should probably treat this as a fresh bot command.
                    status = 'bot' # Local override to fall through to bot logic
                else:
                    # Just forward to Telegram
                    if text_body:
                        await telegram_crm.send_log_to_admin(chat_id, f"📩 {text_body}")
                    else:
                        await telegram_crm.send_log_to_admin(chat_id, f"📩 [Media/Other Message Type]")
                    # STOP here
                    continue

            # --- BOT MODE ---
            if status == 'bot':
                
                # A. Search Logic (Text)
                if msg_type == 'text':
                    text_body = msg['text']['body'].strip()
                    log_to_db(chat_id, 'search_text', text_body, payload=msg)
                    
                    # Silent Mirroring to Telegram
                    await telegram_crm.send_log_to_admin(chat_id, f"🔍 Buscó: {text_body}", is_alert=False)
                    
                    # Check "Hola" logic or Stop Words if desired, but "Search Logic" is main focus
                    stop_words = ['hola', 'start', 'hi', 'hello', 'menú', 'menu']
                    if text_body.lower() in stop_words:
                        welcome_text = (
                            "👋 **Hola! Soy FiltraBot.**\n"
                            "Tu buscador de filtros al instante. 🇦🇷\n\n"
                            "👇 **Escribí el modelo de tu auto:**\n"
                            "(ej: Gol Trend 1.6)"
                        )
                        await reply_and_mirror(chat_id, welcome_text)
                    else:
                        # Search DB
                        try:
                            res = supabase.table("vehicle").select("vehicle_id, brand_car, model, series_suffix, body_type, fuel_type, year_from, year_to, engine_disp_l, power_hp, engine_valves")\
                                .or_(f"brand_car.ilike.%{text_body}%,model.ilike.%{text_body}%")\
                                .limit(12).execute()
                            
                            vehicles = res.data

                            # 0 Results
                            if not vehicles:
                                reply = f"🤔 No encontré '{text_body}'. ¿No aparece o es un error?"
                                buttons = [
                                    {"id": "btn_human_help", "title": "✅ Sí, pedir ayuda"},
                                    {"id": "btn_search_error", "title": "🔙 No, error mio"}
                                ]
                                await reply_and_mirror(chat_id, reply, buttons=buttons)
                            
                            # >10 Results
                            elif len(vehicles) > 10:
                                await reply_and_mirror(chat_id, f"🖐 Encontré demasiados ({len(vehicles)}+). Sé más específico (ej: agregar motor o año).")

                            # 1-10 Results
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
                                    "Seleccioná tu versión exacta:",
                                    list_rows=list_rows,
                                    list_title="Resultados"
                                )

                        except Exception as e:
                            print(f"Search Error: {e}")
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
                            line = f"• {part.get('brand_filter')}: {part.get('part_code')}"
                            found_parts.setdefault(ptype, []).append(line)
                    
                    type_dic = {'oil': '🛢️ Aceite', 'air': '💨 Aire', 'cabin': '❄️ Habitáculo', 'fuel': '⛽ Combustible'}
                    for k, label in type_dic.items():
                        if k in found_parts:
                            msg_body += f"{label}\n" + "\n".join(found_parts[k]) + "\n\n"
                    
                    if not found_parts: msg_body += "⚠️ Sin filtros cargados.\n"

                    # 3 Action Buttons
                    buttons = [
                        {"id": "btn_buy_loc", "title": "📍 Dónde comprar"},
                        {"id": "btn_menu_mech", "title": "⚙️ Menú / Taller"},
                        {"id": "btn_search_error", "title": "🔍 Buscar otro"} 
                        # Using search_error as 'search other' roughly, or just simple text instructions?
                        # Requirement: "🔍 Buscar otro"
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
                        await reply_and_mirror(chat_id, "✅ Ticket creado. Te contestaré en breve.")
                    
                    # 2. Search Error / Back
                    elif btn_id == 'btn_search_error':
                        await reply_and_mirror(chat_id, "👍 Dale, probá escribiendo de otra forma.")

                    # 3. Dónde comprar
                    elif btn_id == 'btn_buy_loc':
                        # Ask for location
                        # We need to set a temporary state or just expect the next message?
                        # For simplicity in this logic, we'll just Ask.
                        # But wait, if they type "Palermo", the bot will search for car "Palermo".
                        # Current implementation doesn't support multistep flows easily without state.
                        # However, we can use "status". Maybe a sub-status? 
                        # Or, prompt says: "Capture next text input -> Save to users.location"
                        # I can add a specific status: 'waiting_location'.
                        
                        supabase.table("users").update({"status": "waiting_location"}).eq("phone", chat_id).execute()
                        await reply_and_mirror(chat_id, "📍 ¿De qué Barrio o Ciudad sos?")
                    
                    # 4. Menú / Taller
                    elif btn_id == 'btn_menu_mech':
                        reply = "¿Eres colega o encontraste un error?"
                        sub_btns = [
                            {"id": "btn_is_mechanic", "title": "🔧 Soy Mecánico"},
                            {"id": "btn_is_seller", "title": "🏪 Soy Vendedor"},
                            {"id": "btn_report_err", "title": "📝 Reportar error"}
                        ]
                        await reply_and_mirror(chat_id, reply, buttons=sub_btns)

                    # 5. Handler 🔧 Soy Mecánico
                    elif btn_id == 'btn_is_mechanic':
                        reply = "Ofrecemos catálogos PRO para talleres. ¿Te anoto en la lista?"
                        # Button [✅ Sí, quiero PRO]
                        # API limits 3 buttons.
                        await reply_and_mirror(chat_id, reply, buttons=[{"id": "btn_mech_confirm", "title": "✅ Sí, quiero PRO"}])

                    elif btn_id == 'btn_mech_confirm':
                        supabase.table("users").update({"user_type": "mechanic"}).eq("phone", chat_id).execute()
                        await telegram_crm.send_log_to_admin(chat_id, "👨‍🔧 **User is Mechanic** (Requested PRO)", is_alert=True)
                        await reply_and_mirror(chat_id, "¡Anotado! Te contactaremos.")

                    # 6. Handler 🏪 Soy Vendedor
                    elif btn_id == 'btn_is_seller':
                        reply = "¿Quieres recibir clientes de tu zona?"
                        await reply_and_mirror(chat_id, reply, buttons=[{"id": "btn_seller_confirm", "title": "👋 Contactar"}])

                    elif btn_id == 'btn_seller_confirm':
                        supabase.table("users").update({"user_type": "seller"}).eq("phone", chat_id).execute()
                        await telegram_crm.send_log_to_admin(chat_id, "🏪 **User is Seller** (Wants Leads)", is_alert=True)
                        await reply_and_mirror(chat_id, "¡Genial! Hablamos pronto.")

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
                    await reply_and_mirror(chat_id, msg, buttons=[{"id": "btn_search_error", "title": "🔍 Buscar otro"}])

    return {"status": "ok"}
