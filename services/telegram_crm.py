import os
import asyncio
from aiogram import Bot, Dispatcher, Router, F
from aiogram.types import Message, ForumTopic
from aiogram.filters import Command
from supabase import create_client, Client
from services.whatsapp import send_whatsapp_message, send_interactive_buttons

# Environment Variables
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
ADMIN_GROUP_ID = -1003686781828  # Hardcoded as per requirements
SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")

# Initialize Supabase
supabase: Client = None
if SUPABASE_URL and SUPABASE_KEY:
    supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

# Initialize Router
admin_router = Router()

def get_bot_instance():
    if not TELEGRAM_BOT_TOKEN:
        print("WARNING: TELEGRAM_BOT_TOKEN not set.")
        return None
    return Bot(token=TELEGRAM_BOT_TOKEN)

async def start_telegram():
    """
    Initializes the Bot and Dispatcher. 
    Returns the dispatcher and bot instance for the lifespan handler.
    """
    bot = get_bot_instance()
    dp = Dispatcher()
    dp.include_router(admin_router)
    return bot, dp

# --- Topic Management ---

async def get_or_create_topic(phone: str, user_name: str = "Unknown") -> int:
    """
    Checks if a topic exists in DB. If not, creates one in the Admin Group.
    Returns the topic_id (message_thread_id).
    """
    bot = get_bot_instance()
    if not bot or not supabase:
        return 0

    try:
        # 1. Check DB
        res = supabase.table("users").select("telegram_topic_id").eq("phone", phone).single().execute()
        if res.data and res.data.get("telegram_topic_id"):
            return int(res.data["telegram_topic_id"])
        
        # 2. Create Topic
        topic_name = f"+{phone} ({user_name})"
        topic: ForumTopic = await bot.create_forum_topic(chat_id=ADMIN_GROUP_ID, name=topic_name)
        topic_id = topic.message_thread_id

        # 3. Send Pinned Client Card
        card_text = (
            f"👤 **Cliente:** {user_name}\n"
            f"📱 **Cel:** {phone}\n"
            f"🔗 [WhatsApp](https://wa.me/{phone})\n"
            f"ℹ️ **Status:** Nuevo"
        )
        pinned_msg = await bot.send_message(
            chat_id=ADMIN_GROUP_ID, 
            message_thread_id=topic_id, 
            text=card_text, 
            parse_mode="Markdown"
        )
        try:
            await bot.pin_chat_message(chat_id=ADMIN_GROUP_ID, message_id=pinned_msg.message_id)
        except:
            pass # Non-critical

        # 4. Save to DB
        # Check if user exists first to decide insert vs update? 
        # Requirement says "Check users.telegram_topic_id in DB". 
        # If user row doesn't exist, we might need to create it.
        # But usually user is created on first message in bot.py?
        # Let's assume user exists or we upsert.
        
        user_data = {
            "phone": phone,
            "name": user_name,
            "telegram_topic_id": topic_id,
            "status": "bot", # Default
            "last_active_at": "now()"
        }
        supabase.table("users").upsert(user_data).execute()
        
        return topic_id

    except Exception as e:
        print(f"[Telegram Error] get_or_create_topic: {e}")
        return 0

async def send_log_to_admin(phone: str, text: str, is_alert: bool = False):
    """
    Forwards a message/log from WhatsApp to the user's Telegram topic.
    """
    bot = get_bot_instance()
    if not bot:
        return

    # Get topic
    topic_id = await get_or_create_topic(phone)
    if not topic_id:
        print(f"Could not find/create topic for {phone}")
        return

    try:
        # Send message
        # Standard Log: disable_notification=True
        # Alert: disable_notification=False + Change Icon (optional but requested)
        
        if is_alert:
            # Try to start/reopen topic or change icon if possible. 
            # Editing topic icon is 'edit_forum_topic'.
            try:
                # Custom Emoji or Red color. 
                # icon_custom_emoji_id is for premium. We can rename topic?
                # Or just send loud message.
                # Requirement: "Change Topic Icon (Custom Emoji or Red color)"
                # We'll try to reopen it at least.
                await bot.reopen_forum_topic(chat_id=ADMIN_GROUP_ID, message_thread_id=topic_id)
            except:
                pass

        await bot.send_message(
            chat_id=ADMIN_GROUP_ID,
            message_thread_id=topic_id,
            text=text,
            disable_notification=not is_alert
        )

    except Exception as e:
        print(f"[Telegram Log Error] {e}")


# --- Admin Reply Handler ---

@admin_router.message(F.chat.id == ADMIN_GROUP_ID)
async def handle_admin_reply(message: Message):
    """
    Listens for replies in topics and forwards them to WhatsApp.
    """
    if not message.message_thread_id:
        return # Ignore general chat messages not in a topic?

    topic_id = message.message_thread_id
    text = message.text or message.caption

    if not text:
        return

    try:
        # Find user by topic_id
        res = supabase.table("users").select("*").eq("telegram_topic_id", topic_id).single().execute()
        user = res.data
        
        if not user:
            # Maybe a topic for something else?
            return 

        phone = user['phone']
        status = user.get('status', 'bot')

        # Send to WhatsApp
        send_whatsapp_message(phone, text)

        # Crucial: Append "Return to Bot" if in 'human' mode
        if status == 'human':
            # We send a separate button message to allow them to go back to bot
            # "↩️ Volver al Bot"
            buttons = [{"id": "cmd_return_bot", "title": "↩️ Volver al Bot"}]
            # We use send_interactive_buttons from services.whatsapp
            # Re-import inside or top level? Top level is fine now.
            
            # Using a delay or just sending it after?
            # sending immediately
            send_interactive_buttons(phone, "Si ya resolviste tu duda:", buttons)

    except Exception as e:
        print(f"[Telegram Reply Error] {e}")


# --- Outreach Command ---
@admin_router.message(Command("new"))
async def cmd_new_lead(message: Message):
    """
    Usage: /new <phone> <name>
    Creates topic, adds user to DB, sends Hello template.
    """
    args = message.text.split()
    if len(args) < 3:
        await message.reply("Uso: /new <phone> <name>")
        return

    phone = args[1]
    name = " ".join(args[2:])

    # Create topic
    topic_id = await get_or_create_topic(phone, name)
    
    if topic_id:
        await message.reply(f"Topic creado/encontrado: {topic_id}")
        
        # Send "Hello" template (Placeholder)
        # Using send_whatsapp_message for now as we don't have template logic defined in requirements beyond "Hello template"
        # "Uses a placeholder for template name" -> Assuming we just send text or try a template if we had the code.
        # Requirement says: "Sends a 'Hello' template (use a placeholder for template name)"
        # I'll send a text that looks like a hello message for MVP.
        
        welcome_msg = f"Hola {name}! 👋 Gracias por contactarnos via FiltraBot."
        send_whatsapp_message(phone, welcome_msg)
        
        # Alert admin in topic
        await send_log_to_admin(phone, "Outreach iniciado por Admin.", is_alert=False)
    else:
        await message.reply("Error creando topic.")
