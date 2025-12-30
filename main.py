import os
import httpx
import asyncio
import hashlib
import random
from datetime import datetime, timezone
from fastapi import FastAPI, Request, HTTPException, BackgroundTasks
from supabase import create_client, Client
from pydantic import BaseModel
import google.genai as genai  # Added for Gemini

# --- Configuration ---
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHANNEL_ID = os.getenv("REQUIRED_CHANNEL_ID")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY") # Added
BROADCAST_MODE = os.getenv("BROADCAST_MODE", "both") # Added (groups | users | both)
CURRENT_SEASON = "Season 2"
BOT_USERNAME = os.getenv("BOT_USERNAME", "PrizeLedgerBot")
ADMIN_ID = int(os.getenv("ADMIN_ID", "6950876107"))

# Setup Gemini
genai.configure(api_key=GEMINI_API_KEY)
model = genai.GenerativeModel('gemini-2.0-flash-001')

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
app = FastAPI()

# --- Global State for Broadcast Wizard ---
broadcast_state = {}

# --- Helpers ---
async def send_telegram_message(chat_id: int, text: str, reply_markup: dict = None, parse_mode: str = "Markdown"):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {"chat_id": chat_id, "text": text, "parse_mode": parse_mode}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    async with httpx.AsyncClient() as client:
        resp = await client.post(url, json=payload)
        return resp.json()

async def send_telegram_photo(chat_id: int, photo_id: str, caption: str = None, reply_markup: dict = None):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendPhoto"
    payload = {"chat_id": chat_id, "photo": photo_id, "parse_mode": "Markdown"}
    if caption:
        payload["caption"] = caption
    if reply_markup:
        payload["reply_markup"] = reply_markup
    async with httpx.AsyncClient() as client:
        await client.post(url, json=payload)

async def delete_telegram_message(chat_id: int, message_id: int):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/deleteMessage"
    payload = {"chat_id": chat_id, "message_id": message_id}
    async with httpx.AsyncClient() as client:
        await client.post(url, json=payload)

async def check_membership(user_id: int) -> bool:
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/getChatMember"
    params = {"chat_id": CHANNEL_ID, "user_id": user_id}
    async with httpx.AsyncClient() as client:
        resp = await client.get(url, params=params)
        data = resp.json()
        if not data.get("ok"):
            return False
        status = data["result"]["status"]
        return status in ["member", "administrator", "creator"]

def get_season_status():
    response = supabase.table("seasons").select("*").eq("season_name", CURRENT_SEASON).execute()
    if not response.data:
        return False
    season = response.data[0]
    end_date = datetime.fromisoformat(season["end_date"].replace('Z', '+00:00'))
    now = datetime.now(timezone.utc)
    return season["is_active"] and now < end_date

# --- AI BROADCAST LOGIC (NEW) ---

async def generate_ai_broadcast_message(season_name: str, days_remaining: int):
    emoji_pool = ["🚀", "⏳", "🔥", "🎯", "💰", "📈", "⚡", "🏆", "🔔", "🎁"]
    selected_emojis = ", ".join(random.sample(emoji_pool, k=random.randint(2, 4)))
    
    prompt = f"""You are generating a Telegram broadcast message.
Context:
- Campaign name: "{season_name}"
- Giveaway ends in {days_remaining} days
- Use the following emojis: {selected_emojis}
- Audience: Telegram users and groups
- Goal: Urge users to invite friends and climb the leaderboard
- Message must be UNIQUE every time
- Do NOT repeat sentence structures from past messages
- Message must be short, confident, and professional
- Do NOT include links
- Do NOT use hashtags
- Do NOT sound promotional or scammy
- Use Telegram HTML formatting ONLY (<b>, <i>)
- Emojis must be naturally placed
- Do NOT mention AI or automation
Output:
Return ONLY the message content in HTML."""

    for _ in range(3): # Try up to 3 times to get a unique message
        response = model.generate_content(prompt)
        message_text = response.text.strip()
        
        # Duplicate Prevention check
        message_hash = hashlib.sha256(message_text.encode()).hexdigest()
        existing = supabase.table("ai_messages_log").select("id").eq("message_hash", message_hash).execute()
        
        if not existing.data:
            # Save hash and return
            supabase.table("ai_messages_log").insert({
                "season_id": season_name,
                "message_hash": message_hash
            }).execute()
            return message_text
            
    return None

@app.get("/cron/season-broadcast")
async def cron_season_broadcast(background_tasks: BackgroundTasks):
    # 1. Fetch Season Data
    res = supabase.table("seasons").select("*").eq("is_active", True).eq("season_name", CURRENT_SEASON).execute()
    if not res.data:
        return {"status": "No active season found"}
    
    season = res.data[0]
    end_date = datetime.fromisoformat(season["end_date"].replace('Z', '+00:00'))
    days_remaining = (end_date - datetime.now(timezone.utc)).days
    
    # 2. Generate AI Message
    broadcast_html = await generate_ai_broadcast_message(season["season_name"], days_remaining)
    if not broadcast_html:
        return {"status": "Failed to generate unique message"}

    # 3. Determine Targets
    targets = []
    if BROADCAST_MODE in ["users", "both"]:
        users = supabase.table("users").select("user_id").eq("is_active", True).execute()
        targets.extend([u["user_id"] for u in users.data])
        
    if BROADCAST_MODE in ["groups", "both"]:
        groups = supabase.table("bot_groups").select("chat_id").eq("is_admin", True).eq("is_active", True).execute()
        targets.extend([g["chat_id"] for g in groups.data])

    # 4. Execute Sending (Background)
    background_tasks.add_task(run_broadcast_batch, targets, broadcast_html)
    
    return {"status": "Broadcast started", "target_count": len(targets)}

async def run_broadcast_batch(targets, text):
    for chat_id in targets:
        try:
            resp = await send_telegram_message(chat_id, text, parse_mode="HTML")
            # Auto-disable targets if bot is blocked or removed
            if resp.get("ok") is False:
                error_code = resp.get("error_code")
                if error_code in [403, 400]: # Blocked by user or kicked from group
                    if chat_id > 0: # User
                        supabase.table("users").update({"is_active": False}).eq("user_id", chat_id).execute()
                    else: # Group
                        supabase.table("bot_groups").update({"is_active": False}).eq("chat_id", chat_id).execute()
            await asyncio.sleep(0.05) # Rate limiting
        except Exception:
            continue

# --- Keyboards ---
def get_main_menu_keyboard():
    return {
        "keyboard": [
            [{"text": "✅ My Referrals"}, {"text": "🏆 Leaderboard"}],
            [{"text": "📊 My Stats"}]
        ],
        "resize_keyboard": True,
        "one_time_keyboard": False
    }

def get_broadcast_type_keyboard():
    return {
        "inline_keyboard": [
            [{"text": "📸 Photo + Caption", "callback_data": "bc_type_photo"}],
            [{"text": "📝 Text Only", "callback_data": "bc_type_text"}],
            [{"text": "❌ Cancel", "callback_data": "bc_cancel"}]
        ]
    }

def get_confirm_broadcast_keyboard():
    return {
        "inline_keyboard": [
            [{"text": "✅ Send to All Users", "callback_data": "bc_confirm_send"}],
            [{"text": "❌ Cancel", "callback_data": "bc_cancel"}]
        ]
    }

# --- Manual Broadcast Logic (Preserved) ---
async def handle_broadcast_command(chat_id: int):
    if chat_id != ADMIN_ID:
        return
    broadcast_state[chat_id] = {"step": "selecting_type", "data": {}}
    await send_telegram_message(chat_id, "📢 *Broadcast Menu*\n\nSelect the type of message you want to send:", get_broadcast_type_keyboard())

async def process_broadcast_step(chat_id: int, text: str = None, photo_id: str = None):
    state = broadcast_state.get(chat_id)
    if not state: return
    step = state["step"]
  
    if step == "waiting_for_photo":
        if not photo_id:
            await send_telegram_message(chat_id, "⚠️ Please send a PHOTO for this broadcast.")
            return
        state["data"]["photo_id"] = photo_id
        state["step"] = "waiting_for_caption"
        await send_telegram_message(chat_id, "📸 Photo received.\n\nNow send the **Caption**.")
  
    elif step == "waiting_for_caption":
        caption = text if text and text.lower() != "skip" else ""
        button = None
        if caption and "|" in caption.splitlines()[-1]:
            lines = caption.splitlines()
            btn_line = lines.pop()
            btn_text, btn_url = btn_line.split("|", 1)
            button = {"inline_keyboard": [[{"text": btn_text.strip(), "url": btn_url.strip()}]]}
            caption = "\n".join(lines).strip()
          
        state["data"]["caption"] = caption
        state["data"]["markup"] = button
        state["step"] = "confirming"
        await send_telegram_message(chat_id, "👀 *Preview:*")
        await send_telegram_photo(chat_id, state["data"]["photo_id"], caption, button)
        await send_telegram_message(chat_id, "Do you want to send this broadcast?", get_confirm_broadcast_keyboard())

    elif step == "waiting_for_text":
        if not text:
            await send_telegram_message(chat_id, "⚠️ Please send TEXT.")
            return
        button = None
        msg_text = text
        if "|" in text.splitlines()[-1]:
            lines = text.splitlines()
            btn_line = lines.pop()
            btn_text, btn_url = btn_line.split("|", 1)
            button = {"inline_keyboard": [[{"text": btn_text.strip(), "url": btn_url.strip()}]]}
            msg_text = "\n".join(lines).strip()
        state["data"]["text"] = msg_text
        state["data"]["markup"] = button
        state["step"] = "confirming"
        await send_telegram_message(chat_id, "👀 *Preview:*")
        await send_telegram_message(chat_id, msg_text, button)
        await send_telegram_message(chat_id, "Do you want to send this broadcast?", get_confirm_broadcast_keyboard())

async def execute_broadcast(admin_id: int):
    state = broadcast_state.get(admin_id)
    if not state or state["step"] != "confirming": return
    data = state["data"]
    bc_type = state.get("type")
    users = supabase.table("users").select("user_id").execute()
    user_list = [u["user_id"] for u in users.data]
    await send_telegram_message(admin_id, f"🚀 Starting broadcast to {len(user_list)} users...")
    success_count = 0
    for uid in user_list:
        try:
            if bc_type == "photo":
                await send_telegram_photo(uid, data["photo_id"], data["caption"], data["markup"])
            else:
                await send_telegram_message(uid, data["text"], data["markup"])
            success_count += 1
            await asyncio.sleep(0.05)
        except Exception: continue
    await send_telegram_message(admin_id, f"✅ Broadcast Complete!\n\nSent: {success_count}")
    del broadcast_state[admin_id]

# --- Bot Logic Handlers ---
async def handle_start(chat_id: int, username: str, args: str, message_id: int):
    if not get_season_status():
        await send_telegram_message(chat_id, f"🚫 *{CURRENT_SEASON}* has ended.")
        return
    is_member = await check_membership(chat_id)
    referrer_id = None
    if args and args.isdigit() and int(args) != chat_id:
        referrer_id = int(args)
    user_data = {"user_id": chat_id, "username": username, "season": CURRENT_SEASON, "is_active": True}
  
    existing = supabase.table("users").select("*").eq("user_id", chat_id).execute()
    if not existing.data and referrer_id:
        ref_check = supabase.table("users").select("user_id").eq("user_id", referrer_id).execute()
        if ref_check.data: user_data["referred_by"] = referrer_id
  
    supabase.table("users").upsert(user_data, on_conflict="user_id").execute()
    if not is_member:
        keyboard = {"inline_keyboard": [[{"text": f"👉 Join {CHANNEL_ID}", "url": f"https://t.me/{CHANNEL_ID.replace('@', '')}"}], [{"text": "🔄 I have Joined", "callback_data": "check_join"}]]}
        await send_telegram_message(chat_id, f"❌ You must join our channel {CHANNEL_ID} to use this bot.", keyboard)
    else:
        current_user = existing.data[0] if existing.data else user_data
        if current_user.get("is_participating"):
            await delete_telegram_message(chat_id, message_id)
            await send_telegram_message(chat_id, "👋 Welcome back! Use the menu below.", get_main_menu_keyboard())
        else:
            keyboard = {"inline_keyboard": [[{"text": "✅ Participate", "callback_data": "participate"}]]}
            await send_telegram_message(chat_id, f"🎉 You have joined! Click below to register for {CURRENT_SEASON}.", keyboard)

async def handle_participate(chat_id: int):
    if not await check_membership(chat_id):
        await send_telegram_message(chat_id, f"❌ You left the channel! Join {CHANNEL_ID} again.")
        return
    user = supabase.table("users").select("*").eq("user_id", chat_id).execute()
    if not user.data:
        await send_telegram_message(chat_id, "⚠️ Type /start first.")
        return
    current_user = user.data[0]
    if not current_user["is_participating"]:
        supabase.table("users").update({"is_participating": True}).eq("user_id", chat_id).execute()
        if current_user["referred_by"]:
            ref_data = supabase.table("users").select("valid_referrals").eq("user_id", current_user["referred_by"]).execute()
            if ref_data.data:
                new_count = ref_data.data[0]["valid_referrals"] + 1
                supabase.table("users").update({"valid_referrals": new_count}).eq("user_id", current_user["referred_by"]).execute()
    ref_link = f"https://t.me/{BOT_USERNAME}?start={chat_id}"
    await send_telegram_message(chat_id, f"✅ *You are now participating!*\n\n🔗 *Your Referral Link:*\n`{ref_link}`", get_main_menu_keyboard())

async def handle_stats_request(chat_id: int, request_type: str):
    if request_type == "referrals":
        user = supabase.table("users").select("valid_referrals").eq("user_id", chat_id).execute()
        if user.data:
            count = user.data[0]['valid_referrals']
            ref_link = f"https://t.me/{BOT_USERNAME}?start={chat_id}"
            await send_telegram_message(chat_id, f"🔗 *Your Referral Link:*\n`{ref_link}`\n\n👥 *Valid Referrals:* {count}")
    elif request_type == "leaderboard":
        res = supabase.table("users").select("username,valid_referrals").eq("is_participating", True).eq("season", CURRENT_SEASON).order("valid_referrals", desc=True).limit(5).execute()
        board = "\n".join([f"{i+1}. {u['username']}: {u['valid_referrals']}" for i, u in enumerate(res.data)])
        await send_telegram_message(chat_id, f"🏆 *Leaderboard*\n\n{board}")
    elif request_type == "stats":
        user = supabase.table("users").select("*").eq("user_id", chat_id).execute()
        if user.data:
            u = user.data[0]
            status = "✅ Active" if u['is_participating'] else "❌ Inactive"
            await send_telegram_message(chat_id, f"📊 *My Stats*\n\nStatus: {status}\nReferrals: {u['valid_referrals']}\nSeason: {u['season']}")

# --- API Endpoints ---
@app.post("/webhook")
async def telegram_webhook(request: Request, background_tasks: BackgroundTasks):
    data = await request.json()
    if "my_chat_member" in data:
        chat = data["my_chat_member"]["chat"]
        new_status = data["my_chat_member"]["new_chat_member"]["status"]
        if chat["type"] in ["group", "supergroup"]:
            is_admin = new_status == "administrator"
            is_active = new_status in ["administrator", "member"]
            supabase.table("bot_groups").upsert({
                "chat_id": chat["id"],
                "is_admin": is_admin,
                "is_active": is_active
            }).execute()

    if "callback_query" in data:
        cb = data["callback_query"]
        chat_id = cb["message"]["chat"]["id"]
        data_cb = cb["data"]
        if data_cb == "bc_type_photo":
            broadcast_state[chat_id].update({"step": "waiting_for_photo", "type": "photo"})
            await send_telegram_message(chat_id, "📸 Send the PHOTO now.")
        elif data_cb == "bc_type_text":
            broadcast_state[chat_id].update({"step": "waiting_for_text", "type": "text"})
            await send_telegram_message(chat_id, "📝 Send the MESSAGE TEXT now.")
        elif data_cb == "bc_confirm_send":
            background_tasks.add_task(execute_broadcast, chat_id)
            await send_telegram_message(chat_id, "⏳ Sending broadcast...")
        elif data_cb == "bc_cancel":
            broadcast_state.pop(chat_id, None)
            await send_telegram_message(chat_id, "❌ Cancelled.")
        elif data_cb == "participate":
            background_tasks.add_task(delete_telegram_message, chat_id, cb["message"]["message_id"])
            background_tasks.add_task(handle_participate, chat_id)
        elif data_cb == "check_join":
            background_tasks.add_task(handle_start, chat_id, cb["from"].get("username", "Unknown"), "", cb["message"]["message_id"])
        return {"status": "ok"}

    if "message" in data:
        msg = data["message"]
        chat_id = msg["chat"]["id"]
        text = msg.get("text", "")
        if chat_id in broadcast_state:
            if "photo" in msg:
                background_tasks.add_task(process_broadcast_step, chat_id, photo_id=msg["photo"][-1]["file_id"])
            elif text and not text.startswith("/"):
                background_tasks.add_task(process_broadcast_step, chat_id, text=text)
            return {"status": "ok"}
        if text.startswith("/start"):
            args = text.split(" ")[1] if len(text.split(" ")) > 1 else ""
            background_tasks.add_task(handle_start, chat_id, msg["from"].get("username", "Unknown"), args, msg["message_id"])
        elif text.startswith("/broadcast"):
            background_tasks.add_task(handle_broadcast_command, chat_id)
        elif text == "✅ My Referrals": background_tasks.add_task(handle_stats_request, chat_id, "referrals")
        elif text == "🏆 Leaderboard": background_tasks.add_task(handle_stats_request, chat_id, "leaderboard")
        elif text == "📊 My Stats": background_tasks.add_task(handle_stats_request, chat_id, "stats")
    return {"status": "ok"}

@app.get("/validate-users")
async def validate_users_cron():
    if not get_season_status(): return {"status": "Season ended"}
    users = supabase.table("users").select("user_id, referred_by").eq("is_participating", True).limit(50).execute()
    for user in users.data:
        if not await check_membership(user["user_id"]):
            supabase.table("users").update({"is_participating": False}).eq("user_id", user["user_id"]).execute()
            if user["referred_by"]:
                ref_data = supabase.table("users").select("valid_referrals").eq("user_id", user["referred_by"]).execute()
                if ref_data.data and ref_data.data[0]["valid_referrals"] > 0:
                    supabase.table("users").update({"valid_referrals": ref_data.data[0]["valid_referrals"] - 1}).eq("user_id", user["referred_by"]).execute()
    return {"status": "success"}
