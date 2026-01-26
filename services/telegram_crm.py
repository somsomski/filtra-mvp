import os
import asyncio
from aiogram import Bot, Dispatcher, Router, F
from aiogram.client.default import DefaultBotProperties
from aiogram.types import Message, ForumTopic, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from aiogram.filters import Command
from supabase import create_client, Client
from services.whatsapp import send_whatsapp_message, send_interactive_buttons

# Environment Variables
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
# Check env first, else fallback
ADMIN_GROUP_ID = int(os.environ.get("ADMIN_GROUP_ID", -1003686781828))
ADMIN_TAG = os.environ.get("ADMIN_TAG")
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

async def update_topic_title(phone: str, new_status: str, user_type: str):
    """
    Updates the topic title based on status and user type.
    """
    bot = get_bot()
    if not bot or not supabase:
        return

    try:
        # Get topic_id and name from DB
        res = supabase.table("users").select("telegram_topic_id, name").eq("phone", phone).maybe_single().execute()
        if not res or not res.data:
            return

        topic_id = res.data.get("telegram_topic_id")
        user_name = res.data.get("name") or phone
        
        if not topic_id:
            return

        # Determine Emojis
        # Circle (Status)
        status_to_emoji = {
            'bot': '🔵',
            'human': '🟡',
            'waiting...': '🟢'
        }
        circle = status_to_emoji.get(new_status, '🔵')

        # Icon (Type)
        type_to_emoji = {
            'mechanic': '👨🔧',
            'seller': '🏪',
            'unknown': '👤'
        }
        icon = type_to_emoji.get(user_type, '👤')

        new_title = f"{circle} {icon} {user_name}"

        await bot.edit_forum_topic(
            chat_id=ADMIN_GROUP_ID,
            message_thread_id=int(topic_id),
            name=new_title
        )

    except Exception as e:
        print(f"[Telegram Error] update_topic_title: {e}")

async def send_log_to_admin(phone: str, text: str, priority: str = 'log'):
    """
    Forwards a message/log from WhatsApp to the user's Telegram topic.
    Priority:
    - 'log': disable_notification=True, "📝 "
    - 'normal': disable_notification=False, "📩 "
    - 'high': disable_notification=False, "🚨 " + ADMIN_TAG
    """
    bot = get_bot()
    if not bot:
        return

    # Get topic
    topic_id = await get_or_create_topic(phone)
    if not topic_id:
        print(f"Could not find/create topic for {phone}")
        return

    prefix = ""
    disable_notif = False
    
    if priority == 'log':
        prefix = "📝 "
        disable_notif = True
    elif priority == 'normal':
        prefix = "📩 "
        disable_notif = False
    elif priority == 'high':
        prefix = "🚨 "
        disable_notif = False
        if ADMIN_TAG:
            text += f" {ADMIN_TAG}"
    
    final_text = f"{prefix}{text}"

    try:
        # High priority -> Reopen topic
        if priority == 'high':
            try:
                await bot.reopen_forum_topic(chat_id=ADMIN_GROUP_ID, message_thread_id=topic_id)
            except:
                pass

        await bot.send_message(
            chat_id=ADMIN_GROUP_ID,
            message_thread_id=topic_id,
            text=final_text,
            disable_notification=disable_notif
        )

    except Exception as e:
        print(f"[Telegram Log Error] {e}")

async def send_resolved_button(phone: str):
    """
    Sends a message with [✅ Volver a Bot] button to the Telegram topic.
    """
    bot = get_bot()
    if not bot: return

    topic_id = await get_or_create_topic(phone)
    if not topic_id: return

    try:
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✅ Volver a Bot", callback_data=f"resolve_{phone}")]
        ])
        
        await bot.send_message(
            chat_id=ADMIN_GROUP_ID,
            message_thread_id=topic_id,
            text="Control manual finalizado?",
            reply_markup=kb
        )
    except Exception as e:
        print(f"[Telegram Error] send_resolved_button: {e}")

# --- Admin Reply Handler ---

@admin_router.message(F.chat.id == ADMIN_GROUP_ID)
async def handle_admin_reply(message: Message):
    """
    Listens for replies in topics and forwards them to WhatsApp.
    """
    if not message.message_thread_id:
        return 

    topic_id = message.message_thread_id
    text = message.text or message.caption

    if not text:
        return

    try:
        # Find user by topic_id
        res = supabase.table("users").select("*").eq("telegram_topic_id", topic_id).maybe_single().execute()
        user = res.data if res else None
        
        if not user:
            return 

        phone = user['phone']
        current_status = user.get('status', 'bot')

        # Send to WhatsApp
        if current_status != 'human':
             supabase.table("users").update({"status": "human"}).eq("phone", phone).execute()
             
             # Create task to update title (non-blocking ideally, but await here is fine)
             # Assumption: user_type unknown if not in DB, but we pass unknown. 
             # In a real app we might fetch 'user_type' from 'user' dict if exists.
             await update_topic_title(phone, "human", "unknown") 

        # Try to send as interactive button message (cleanest UX)
        try:
             exit_btn = [{"id": "btn_return_bot", "title": "🤖 Volver al Bot"}]
             send_interactive_buttons(phone, text, exit_btn)
        except Exception as e:
             # Fallback
             print(f"Fallback to text: {e}")
             send_whatsapp_message(phone, text)

    except Exception as e:
        print(f"[Telegram Reply Error] {e}")


@admin_router.callback_query(F.data.startswith("resolve_"))
async def on_resolve_click(callback: CallbackQuery):
    """
    Handler for [✅ Volver a Bot] button in Telegram.
    """
    try:
        phone = callback.data.split("_")[1]
        
        # Update DB
        if supabase:
            supabase.table("users").update({"status": "bot"}).eq("phone", phone).execute()
        
        # Update Title
        await update_topic_title(phone, "bot", "unknown")

        await callback.message.edit_text(f"✅ Conversación marcada como resuelta (Bot).")
        await callback.answer()
        
    except Exception as e:
        print(f"[Telegram Callback Error] {e}")


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
        
        welcome_msg = f"Hola {name}! 👋 Gracias por contactarnos via FiltraBot."
        send_whatsapp_message(phone, welcome_msg)
        
        # Alert admin in topic
        await send_log_to_admin(phone, "Outreach iniciado por Admin.", priority='log')
    else:
        await message.reply("Error creando topic.")
