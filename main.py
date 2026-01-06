import os
import httpx
import asyncio
import hashlib
import random
from datetime import datetime, timezone
from fastapi import FastAPI, Request, HTTPException, BackgroundTasks
from supabase import create_client, Client
from pydantic import BaseModel
from google import genai 

# --- Configuration ---
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHANNEL_ID = os.getenv("REQUIRED_CHANNEL_ID")
CHANNEL_USERNAME = os.getenv("REQUIRED_CHANNEL_USERNAME", CHANNEL_ID.replace('@', ''))  # Extract username
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
BROADCAST_MODE = os.getenv("BROADCAST_MODE", "both")
CURRENT_SEASON = "Season 2"
BOT_USERNAME = os.getenv("BOT_USERNAME", "PrizeLedgerBot")
ADMIN_ID = int(os.getenv("ADMIN_ID", "6950876107"))

# Setup Gemini
client = genai.Client(api_key=GEMINI_API_KEY)

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
app = FastAPI()

# --- Global State for Broadcast Wizard ---
broadcast_state = {}

# --- Helpers ---
async def send_telegram_message(chat_id: int, text: str, reply_markup: dict = None, parse_mode: str = "Markdown"):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {"chat_id": chat_id, "text": text}
    
    # Only add parse_mode if it is provided (not None)
    if parse_mode:
        payload["parse_mode"] = parse_mode
        
    if reply_markup:
        payload["reply_markup"] = reply_markup
        
    async with httpx.AsyncClient() as client:
        resp = await client.post(url, json=payload)
        return resp.json()

async def send_telegram_photo(chat_id: int, photo_id: str, caption: str = None, reply_markup: dict = None, parse_mode: str = "Markdown"):
    # UPDATED: Added parse_mode argument to support links in captions
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendPhoto"
    payload = {"chat_id": chat_id, "photo": photo_id}
    
    if parse_mode:
        payload["parse_mode"] = parse_mode
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

def get_ordinal(n):
    if 11 <= (n % 100) <= 13:
        suffix = 'th'
    else:
        suffix = {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th')
    return f"{n}{suffix}"

async def notify_referrer(referrer_id: int, new_user_username: str = "Anonymous"):
    """Send notification to referrer when someone joins using their referral link"""
    try:
        message = f"""🎉 <b>New Referral!</b>

Someone just joined using your referral link!

👤 User: @{new_user_username if new_user_username else 'Anonymous'}
📈 Keep inviting to climb the leaderboard!

Use /start to check your stats."""
        
        keyboard = {
            "inline_keyboard": [
                [{"text": "📊 Check Stats", "callback_data": "stats"}]
            ]
        }
        
        await send_telegram_message(referrer_id, message, keyboard, parse_mode="HTML")
    except Exception as e:
        print(f"Failed to notify referrer {referrer_id}: {e}")

# --- AI BROADCAST LOGIC ---

async def generate_ai_broadcast_message(season_name: str, days_remaining: int):
    # UPDATED PROMPT: NO HTML, NO MARKDOWN. Plain Text Only.
    prompt = f"""You are a Telegram Community Manager writing a broadcast for "{season_name}".
    
    Context:
    - Time Remaining: {days_remaining} days.
    - Goal: Motivate users to invite friends and check the leaderboard.
    
    Instructions:
    1. Write a short, high-energy update using PLAIN TEXT ONLY.
    2. Do NOT use HTML tags (No <b>, No <i>).
    3. Do NOT use Markdown (No **bold**, No *italics*).
    4. Include these 5 distinct sections (just write them naturally):
       - A catchy Headline about the countdown (Use CAPS for emphasis instead of bold).
       - A punchy sentence stating exactly {days_remaining} days remain.
       - A Warning: Remind users that leaving the group voids their prize (Use an emoji like ⚠️ or ❌).
       - A Leaderboard Note: Mention that rankings are still shifting and every invite counts.
       - A Call to Action list (Stay active, Invite, Finish strong).
    
    5. VARY your wording, sentence structure, and emoji choices every time you generate this.
    6. Do NOT use placeholder text or brackets.
    7. Do NOT include links.
    8. Output ONLY the raw text message.
    """

    for _ in range(3): # Try up to 3 times to get a unique message
        response = client.models.generate_content(
            model='gemini-2.5-flash',
            contents=prompt
        )
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

def fetch_all_targets(table_name, select_col, filters=None):
    """Helper to fetch ALL rows from Supabase using pagination (avoids 1000 row limit)"""
    all_items = []
    offset = 0
    limit = 1000 # Fetch in chunks of 1000
    
    while True:
        query = supabase.table(table_name).select(select_col)
        
        if filters:
            for key, value in filters.items():
                query = query.eq(key, value)
                
        # Use range for pagination
        response = query.range(offset, offset + limit - 1).execute()
        data = response.data
        
        if not data:
            break
            
        all_items.extend([item[select_col] for item in data])
        
        if len(data) < limit:
            break
            
        offset += limit
        
    return all_items

@app.get("/cron/season-broadcast")
async def cron_season_broadcast(background_tasks: BackgroundTasks):
    """Send AI-generated broadcast to all users and groups"""
    # 1. Fetch Season Data
    res = supabase.table("seasons").select("*").eq("is_active", True).eq("season_name", CURRENT_SEASON).execute()
    if not res.data:
        return {"status": "No active season found"}
    
    season = res.data[0]
    end_date = datetime.fromisoformat(season["end_date"].replace('Z', '+00:00'))
    days_remaining = (end_date - datetime.now(timezone.utc)).days
    
    # 2. Generate AI Message (Plain Text now)
    broadcast_text = await generate_ai_broadcast_message(season["season_name"], days_remaining)
    if not broadcast_text:
        return {"status": "Failed to generate unique message"}

    # 3. Determine Targets with PAGINATION to ensure ALL users are fetched
    targets = []
    
    if BROADCAST_MODE in ["users", "both"]:
        # Fetch ALL users where is_active is True
        user_ids = fetch_all_targets("users", "user_id", {"is_active": True})
        targets.extend(user_ids)
        
    if BROADCAST_MODE in ["groups", "both"]:
        # Fetch ALL groups where is_admin and is_active are True
        group_ids = fetch_all_targets("bot_groups", "chat_id", {"is_admin": True, "is_active": True})
        targets.extend(group_ids)

    # 4. Execute Sending (Background)
    # The message is sent without preview, directly to the background task
    if targets:
        # Remove duplicates just in case
        targets = list(set(targets))
        background_tasks.add_task(run_broadcast_batch, targets, broadcast_text)
        return {"status": "Broadcast initiated", "target_count": len(targets)}
    else:
        return {"status": "No targets found"}

async def run_broadcast_batch(targets, text):
    """Send broadcast messages to all targets with error handling"""
    success_count = 0
    failed_count = 0
    blocked_count = 0
    
    print(f"Starting broadcast to {len(targets)} targets...")
    
    for chat_id in targets:
        try:
            # Sending with parse_mode=None to handle RAW TEXT only (prevents HTML/Markdown errors)
            resp = await send_telegram_message(chat_id, text, parse_mode=None)
            
            # Check response
            if resp.get("ok"):
                success_count += 1
            else:
                error_code = resp.get("error_code")
                error_desc = resp.get("description", "Unknown error")
                failed_count += 1
                
                # Auto-disable targets if bot is blocked or removed
                if error_code in [403, 400]: # Blocked by user or kicked from group
                    blocked_count += 1
                    try:
                        if chat_id > 0: # User
                            supabase.table("users").update({"is_active": False}).eq("user_id", chat_id).execute()
                        else: # Group
                            supabase.table("bot_groups").update({"is_active": False}).eq("chat_id", chat_id).execute()
                    except Exception as db_error:
                        print(f"Database update failed for {chat_id}: {db_error}")
                
                print(f"Failed to send to {chat_id}: {error_code} - {error_desc}")
            
            # Small delay to respect Telegram limits (30 msgs/sec), preventing 429 Too Many Requests
            await asyncio.sleep(0.04) 
            
        except Exception as e:
            failed_count += 1
            print(f"Exception sending to {chat_id}: {e}")
            continue
    
    print(f"Broadcast complete: {success_count} sent, {failed_count} failed, {blocked_count} blocked")

# --- Keyboards ---
def get_main_menu_keyboard():
    return {
        "keyboard": [
            [{"text": "✅ My Referrals"}, {"text": "🏆 Leaderboard"}],
            [{"text": "📊 My Stats"}, {"text": "💸 Withdraw"}]
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

def get_share_button(ref_link):
    share_text = f"Join {CURRENT_SEASON} and win big prizes! 🚀"
    # Create the telegram share url
    share_url = f"https://t.me/share/url?url={ref_link}&text={share_text}"
    return {
        "inline_keyboard": [
            [{"text": "🚀 Share Link", "url": share_url}]
        ]
    }

# --- Manual Broadcast Logic (Preserved & Updated) ---
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
        # UPDATED: Instruct user they can use Markdown links in the caption
        await send_telegram_message(chat_id, "📸 Photo received.\n\nNow send the **Caption**.\n\n💡 *Tip:* You can use `[Link Text](URL)` to add links inside the caption, and add a line button at the end like: `Button | URL`.")
    
    elif step == "waiting_for_caption":
        caption = text if text and text.lower() != "skip" else ""
        button = None
        
        # Check for button line at the bottom
        if caption and len(caption.splitlines()) > 0 and "|" in caption.splitlines()[-1]:
            lines = caption.splitlines()
            btn_line = lines.pop()
            btn_text, btn_url = btn_line.split("|", 1)
            button = {"inline_keyboard": [[{"text": btn_text.strip(), "url": btn_url.strip()}]]}
            caption = "\n".join(lines).strip()
            
        state["data"]["caption"] = caption
        state["data"]["markup"] = button
        state["step"] = "confirming"
        await send_telegram_message(chat_id, "👀 *Preview:*")
        # Ensure parse_mode is passed as Markdown so links work
        await send_telegram_photo(chat_id, state["data"]["photo_id"], caption, button, parse_mode="Markdown")
        await send_telegram_message(chat_id, "Do you want to send this broadcast?", get_confirm_broadcast_keyboard())

    elif step == "waiting_for_text":
        if not text:
            await send_telegram_message(chat_id, "⚠️ Please send TEXT.")
            return
        button = None
        msg_text = text
        
        # Check for button line at the bottom
        if len(text.splitlines()) > 0 and "|" in text.splitlines()[-1]:
            lines = text.splitlines()
            btn_line = lines.pop()
            btn_text, btn_url = btn_line.split("|", 1)
            button = {"inline_keyboard": [[{"text": btn_text.strip(), "url": btn_url.strip()}]]}
            msg_text = "\n".join(lines).strip()
            
        state["data"]["text"] = msg_text
        state["data"]["markup"] = button
        state["step"] = "confirming"
        await send_telegram_message(chat_id, "👀 *Preview:*")
        await send_telegram_message(chat_id, msg_text, button, parse_mode="Markdown")
        await send_telegram_message(chat_id, "Do you want to send this broadcast?", get_confirm_broadcast_keyboard())

async def execute_broadcast(admin_id: int):
    state = broadcast_state.get(admin_id)
    if not state or state["step"] != "confirming": return
    data = state["data"]
    bc_type = state.get("type")
    
    # Updated: Fetch all users for manual broadcast too
    user_list = fetch_all_targets("users", "user_id")
    
    await send_telegram_message(admin_id, f"🚀 Starting broadcast to {len(user_list)} users...")
    success_count = 0
    for uid in user_list:
        try:
            if bc_type == "photo":
                # UPDATED: Sending with parse_mode="Markdown" ensures links inside caption work
                await send_telegram_photo(uid, data["photo_id"], data["caption"], data["markup"], parse_mode="Markdown")
            else:
                # UPDATED: Sending with parse_mode="Markdown" ensures links inside text work
                await send_telegram_message(uid, data["text"], data["markup"], parse_mode="Markdown")
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
    
    # Check if this is a new user joining via referral
    is_new_user = not existing.data and referrer_id
    
    if is_new_user:
        # Validate referrer exists
        ref_check = supabase.table("users").select("user_id").eq("user_id", referrer_id).execute()
        if ref_check.data:
            user_data["referred_by"] = referrer_id
            
            # Notify referrer about new referral
            await notify_referrer(referrer_id, username)
    
    supabase.table("users").upsert(user_data, on_conflict="user_id").execute()
    
    if not is_member:
        keyboard = {
            "inline_keyboard": [
                [{"text": f"👉 Join @{CHANNEL_USERNAME}", "url": f"https://t.me/{CHANNEL_USERNAME}"}],
                [{"text": "🔄 I have Joined", "callback_data": "check_join"}]
            ]
        }
        await send_telegram_message(chat_id, f"❌ You must join our channel @{CHANNEL_USERNAME} to use this bot.", keyboard)
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
        await send_telegram_message(chat_id, f"❌ You left the channel! Join @{CHANNEL_USERNAME} again.")
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
    await send_telegram_message(chat_id, f"✅ *You are now participating!*\n\n🔗 *Your Referral Link:*\n`{ref_link}`", get_share_button(ref_link))
    await send_telegram_message(chat_id, "Use the menu below to track your progress.", get_main_menu_keyboard())

async def handle_stats_request(chat_id: int, request_type: str):
    if request_type == "referrals":
        user = supabase.table("users").select("valid_referrals").eq("user_id", chat_id).execute()
        if user.data:
            count = user.data[0]['valid_referrals']
            ref_link = f"https://t.me/{BOT_USERNAME}?start={chat_id}"
            await send_telegram_message(chat_id, f"🔗 *Your Referral Link:*\n`{ref_link}`\n\n👥 *Valid Referrals:* {count}", get_share_button(ref_link))
            
    elif request_type == "leaderboard":
        res = supabase.table("users").select("user_id,username,valid_referrals").eq("is_participating", True).eq("season", CURRENT_SEASON).order("valid_referrals", desc=True).limit(10).execute()
        
        board_lines = []
        for i, u in enumerate(res.data):
            rank = i + 1
            if rank == 1:
                prefix = "🥇 👑" 
            elif rank == 2:
                prefix = "🥈"
            elif rank == 3:
                prefix = "🥉"
            else:
                prefix = f"<b>{rank}.</b>"
            
            name = u.get("username", "Unknown") or "Unknown"
            name = name.replace("<", "&lt;").replace(">", "&gt;")
            
            user_link = f"<a href='tg://user?id={u['user_id']}'>{name}</a>"
            
            board_lines.append(f"{prefix} {user_link} : <b>{u['valid_referrals']}</b>")
        
        board_text = "\n".join(board_lines) if board_lines else "No participants yet."
        await send_telegram_message(chat_id, f"🏆 <b>Leaderboard</b>\n\n{board_text}", parse_mode="HTML")
        
    elif request_type == "stats":
        user = supabase.table("users").select("*").eq("user_id", chat_id).execute()
        if user.data:
            u = user.data[0]
            status = "✅ Active" if u['is_participating'] else "❌ Inactive"
            
            # --- RANK CALCULATION ---
            # 1. Get Total Participants Count
            total_query = supabase.table("users").select("user_id", count="exact").eq("is_participating", True).eq("season", CURRENT_SEASON).execute()
            total_participants = total_query.count
            
            # 2. Get User Rank (Count users with MORE referrals than current user)
            my_referrals = u['valid_referrals']
            better_query = supabase.table("users").select("user_id", count="exact")\
                .eq("is_participating", True)\
                .eq("season", CURRENT_SEASON)\
                .gt("valid_referrals", my_referrals)\
                .execute()
            
            # Rank is (people better than me) + 1
            my_rank_num = better_query.count + 1
            rank_str = f"#{get_ordinal(my_rank_num)}"
            
            # Send updated stats message
            msg = (
                f"📊 <b>My Stats</b>\n\n"
                f"Status: {status}\n"
                f"Referrals: <b>{my_referrals}</b>\n"
                f"Rank: <b>{rank_str}</b> out of <b>{total_participants}</b>\n"
                f"Season: {u['season']}"
            )
            await send_telegram_message(chat_id, msg, parse_mode="HTML")

# --- Withdrawal Handler (NEW) ---
async def handle_withdrawal_click(chat_id: int):
    """Handles the withdrawal button click with a dynamic countdown"""
    
    # 1. Fetch Season Data to get the End Date
    response = supabase.table("seasons").select("*").eq("season_name", CURRENT_SEASON).execute()
    
    days_text = "soon" # Default fallback
    
    if response.data:
        season = response.data[0]
        end_date = datetime.fromisoformat(season["end_date"].replace('Z', '+00:00'))
        now = datetime.now(timezone.utc)
        
        # Calculate difference
        remaining = end_date - now
        
        if remaining.days > 0:
            days_text = f"{remaining.days} days"
        elif remaining.days == 0:
            days_text = "less than 24 hours"
        else:
            days_text = "very soon"

    # 2. Professional Message with Red Alert Style
    message = f"""🔴 <b>WITHDRAWAL GATEWAY LOCKED</b> 🔴

The withdrawal functionality is currently <b>disabled</b> for security verification.

⏳ <b>Time Remaining:</b>
The portal is scheduled to open in approximately <b>{days_text}</b>.

⚠️ <b>Notice:</b>
The system is currently finalizing the referral validation process. Any attempt to use fake referrals will result in immediate disqualification.

Please wait for the official announcement in the channel."""

    await send_telegram_message(chat_id, message, parse_mode="HTML")

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
            # UPDATED: Instruct user about link format
            await send_telegram_message(chat_id, "📝 Send the MESSAGE TEXT now.\n\n💡 *Tip:* You can use `[Link Text](URL)` to add links inside the text, and add a line button at the end like: `Button | URL`.")
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
        elif data_cb == "stats":
            background_tasks.add_task(handle_stats_request, chat_id, "stats")
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
        elif text == "✅ My Referrals": 
            background_tasks.add_task(handle_stats_request, chat_id, "referrals")
        elif text == "🏆 Leaderboard": 
            background_tasks.add_task(handle_stats_request, chat_id, "leaderboard")
        elif text == "📊 My Stats": 
            background_tasks.add_task(handle_stats_request, chat_id, "stats")
        elif text == "💸 Withdraw": 
            background_tasks.add_task(handle_withdrawal_click, chat_id)
    
    return {"status": "ok"}

@app.get("/validate-users")
async def validate_users_cron(background_tasks: BackgroundTasks):
    """Validate users are still in the channel and update their status accordingly"""
    if not get_season_status(): 
        return {"status": "Season ended"}
    
    users = supabase.table("users").select("user_id, referred_by").eq("is_participating", True).limit(50).execute()
    
    for user in users.data:
        is_member = await check_membership(user["user_id"])
        
        if not is_member:
            # DB Updates: Mark inactive and decrease referral count
            supabase.table("users").update({"is_participating": False}).eq("user_id", user["user_id"]).execute()
            
            if user["referred_by"]:
                ref_data = supabase.table("users").select("valid_referrals").eq("user_id", user["referred_by"]).execute()
                if ref_data.data and ref_data.data[0]["valid_referrals"] > 0:
                    supabase.table("users").update({"valid_referrals": ref_data.data[0]["valid_referrals"] - 1}).eq("user_id", user["referred_by"]).execute()
            
            # Send Notification Message with channel username for easy rejoin
            msg = f"""❌ <b>You are no longer participating in {CURRENT_SEASON}</b>

<b>Reason:</b> You left the required channel.

<b>📢 Good News!</b>
Join back and participate again — everything will come back!

👉 <a href="https://t.me/{CHANNEL_USERNAME}">Click here to rejoin @{CHANNEL_USERNAME}</a>

After joining, use /start to continue."""
            
            keyboard = {
                "inline_keyboard": [
                    [{"text": f"👉 Join @{CHANNEL_USERNAME}", "url": f"https://t.me/{CHANNEL_USERNAME}"}]
                ]
            }
            
            background_tasks.add_task(send_telegram_message, user["user_id"], msg, keyboard, "HTML")
    
    return {"status": "success", "channel_username": CHANNEL_USERNAME}
