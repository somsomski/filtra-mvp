import os
import asyncio
from aiogram import Bot, Dispatcher, Router, F
from aiogram.client.default import DefaultBotProperties
from aiogram.types import Message, ForumTopic
from aiogram.filters import Command
from supabase import create_client, Client
from services.whatsapp import send_whatsapp_message, send_interactive_buttons

# Environment Variables
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
# Check env first, else fallback
ADMIN_GROUP_ID = int(os.environ.get("ADMIN_GROUP_ID", -1003686781828))
SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")

# Initialize Supabase
supabase: Client = None
if SUPABASE_URL and SUPABASE_KEY:
    supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

# Initialize Router
admin_router = Router()

# Global Instances
bot_instance = None
dp_instance = None

def get_bot():
    """
    Returns the singleton Bot instance.
    """
    global bot_instance
    if bot_instance:
        return bot_instance
    
    if not TELEGRAM_BOT_TOKEN:
        print("WARNING: TELEGRAM_BOT_TOKEN not set.")
        return None
        
    # Lazy init if strictly needed, but start_telegram should be called first
    # However, for safety we can create it here if missing, 
    # but we want to stick to the pattern of one instance.
    # If get_bot is called before start_telegram, we might create one 
    # but lifespan manages the session closure.
    # Best practice: Initialize in start_telegram.
    return None

async def start_telegram():
    """
    Initializes the Bot and Dispatcher as Singletons.
    Returns the dispatcher and bot instance for the lifespan handler.
    """
    global bot_instance, dp_instance
    
    if not TELEGRAM_BOT_TOKEN:
        print("WARNING: TELEGRAM_BOT_TOKEN not set.")
        return None, None

    if bot_instance is None:
        bot_instance = Bot(
            token=TELEGRAM_BOT_TOKEN, 
            default=DefaultBotProperties(parse_mode="Markdown")
        )
    
    if dp_instance is None:
        dp_instance = Dispatcher()
        dp_instance.include_router(admin_router)
        
    return bot_instance, dp_instance

# --- Topic Management ---

async def get_or_create_topic(phone: str, user_name: str = "Unknown") -> int:
    """
    Checks if a topic exists in DB. If not, creates one in the Admin Group.
    Returns the topic_id (message_thread_id).
    """
    bot = get_bot()
    if not bot or not supabase:
        return 0

    try:
        # 1. Check DB
        res = supabase.table("users").select("telegram_topic_id").eq("phone", phone).maybe_single().execute()
        if res and res.data and res.data.get("telegram_topic_id"):
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
            text=card_text
        )
        try:
            await bot.pin_chat_message(chat_id=ADMIN_GROUP_ID, message_id=pinned_msg.message_id)
        except:
            pass # Non-critical

        # 4. Save to DB
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
    bot = get_bot()
    if not bot:
        return

    # Get topic
    topic_id = await get_or_create_topic(phone)
    if not topic_id:
        print(f"Could not find/create topic for {phone}")
        return

    try:
        # Send message
        
        if is_alert:
            try:
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
        res = supabase.table("users").select("*").eq("telegram_topic_id", topic_id).maybe_single().execute()
        user = res.data if res else None
        
        if not user:
            # Maybe a topic for something else?
            return 

        phone = user['phone']
        status = user.get('status', 'bot')

        # Send to WhatsApp
        supabase.table("users").update({"status": "human"}).eq("phone", phone).execute()
        
        # Try to send as interactive button message (cleanest UX)
        try:
             exit_btn = [{"id": "btn_return_bot", "title": "🤖 Volver al Bot"}]
             send_interactive_buttons(phone, text, exit_btn)
        except Exception as e:
             # Fallback for very long text (>1024 chars) or other errors
             print(f"Fallback to text: {e}")
             send_whatsapp_message(phone, text)

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
        # Using send_whatsapp_message for now as we don't have template logic defined in requirements
        
        welcome_msg = f"Hola {name}! 👋 Gracias por contactarnos via FiltraBot."
        send_whatsapp_message(phone, welcome_msg)
        
        # Alert admin in topic
        await send_log_to_admin(phone, "Outreach iniciado por Admin.", is_alert=False)
    else:
        await message.reply("Error creando topic.")
