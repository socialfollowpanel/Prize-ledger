import os
import httpx
from datetime import datetime, timezone
from fastapi import FastAPI, Request, HTTPException, BackgroundTasks
from supabase import create_client, Client
from pydantic import BaseModel

# --- Configuration ---
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHANNEL_ID = os.getenv("REQUIRED_CHANNEL_ID") 
CURRENT_SEASON = "Season 2"
BOT_USERNAME = os.getenv("BOT_USERNAME", "PrizeLedgerBot") # Add your bot username here

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
app = FastAPI()

# --- Helpers ---

async def send_telegram_message(chat_id: int, text: str, reply_markup: dict = None):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "Markdown"}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    async with httpx.AsyncClient() as client:
        await client.post(url, json=payload)

async def delete_telegram_message(chat_id: int, message_id: int):
    """Deletes a specific message to clean up chat."""
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/deleteMessage"
    payload = {"chat_id": chat_id, "message_id": message_id}
    async with httpx.AsyncClient() as client:
        await client.post(url, json=payload)

async def check_membership(user_id: int) -> bool:
    """Checks if user is currently a member/admin/creator of the channel."""
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

# --- Keyboards ---

def get_main_menu_keyboard():
    """Returns the persistent Reply Keyboard."""
    return {
        "keyboard": [
            [{"text": "✅ My Referrals"}, {"text": "🏆 Leaderboard"}],
            [{"text": "📊 My Stats"}]
        ],
        "resize_keyboard": True,
        "one_time_keyboard": False
    }

# --- Bot Logic Handlers ---

async def handle_start(chat_id: int, username: str, args: str, message_id: int):
    # 1. Check Season Status
    if not get_season_status():
        await send_telegram_message(chat_id, f"🚫 *{CURRENT_SEASON}* has ended.")
        return

    # 2. Check Membership IMMEDIATELY
    is_member = await check_membership(chat_id)

    # 3. Upsert User (Track them even if not joined yet, to store referrer)
    referrer_id = None
    if args and args.isdigit() and int(args) != chat_id:
        referrer_id = int(args)

    user_data = {
        "user_id": chat_id,
        "username": username,
        "season": CURRENT_SEASON
    }
    
    # Check if user exists to avoid overwriting existing referrer
    existing = supabase.table("users").select("*").eq("user_id", chat_id).execute()
    if not existing.data and referrer_id:
        # Verify referrer exists
        ref_check = supabase.table("users").select("user_id").eq("user_id", referrer_id).execute()
        if ref_check.data:
            user_data["referred_by"] = referrer_id
    
    supabase.table("users").upsert(user_data, on_conflict="user_id").execute()

    # 4. Logic Flow
    if not is_member:
        # If NOT a member -> Show Join Button
        keyboard = {
            "inline_keyboard": [
                [{"text": f"👉 Join {CHANNEL_ID}", "url": f"https://t.me/{CHANNEL_ID.replace('@', '')}"}],
                [{"text": "🔄 I have Joined", "callback_data": "check_join"}]
            ]
        }
        await send_telegram_message(chat_id, f"❌ You must join our channel {CHANNEL_ID} to use this bot.", keyboard)
    
    else:
        # If IS a member
        current_user = existing.data[0] if existing.data else user_data
        
        if current_user.get("is_participating"):
            # If ALREADY participating -> Clear old messages, show Main Menu
            await delete_telegram_message(chat_id, message_id) # Delete the /start command
            await send_telegram_message(chat_id, "👋 Welcome back! Use the menu below.", get_main_menu_keyboard())
        else:
            # If NEW member but NOT participating -> Show Participate Button
            keyboard = {
                "inline_keyboard": [[{"text": "✅ Participate", "callback_data": "participate"}]]
            }
            await send_telegram_message(chat_id, f"🎉 You have joined! Click below to register for {CURRENT_SEASON}.", keyboard)

async def handle_participate(chat_id: int):
    # Double Check Membership (Anti-Cheat)
    if not await check_membership(chat_id):
        await send_telegram_message(chat_id, f"❌ You left the channel! Join {CHANNEL_ID} again.")
        return

    # Update Status
    user = supabase.table("users").select("*").eq("user_id", chat_id).execute()
    if not user.data:
        await send_telegram_message(chat_id, "⚠️ Type /start first.")
        return
    
    current_user = user.data[0]
    
    if not current_user["is_participating"]:
        # Mark participating
        supabase.table("users").update({"is_participating": True}).eq("user_id", chat_id).execute()
        
        # Credit Referrer
        if current_user["referred_by"]:
            ref_data = supabase.table("users").select("valid_referrals").eq("user_id", current_user["referred_by"]).execute()
            if ref_data.data:
                new_count = ref_data.data[0]["valid_referrals"] + 1
                supabase.table("users").update({"valid_referrals": new_count}).eq("user_id", current_user["referred_by"]).execute()

    # Generate Link
    ref_link = f"https://t.me/{BOT_USERNAME}?start={chat_id}"
    
    # Send Success Message + PERSISTENT KEYBOARD
    msg = (
        f"✅ *You are now participating!*\n\n"
        f"🔗 *Your Referral Link:*\n`{ref_link}`\n\n"
        f"Use the menu below to check your stats."
    )
    await send_telegram_message(chat_id, msg, get_main_menu_keyboard())

async def handle_stats_request(chat_id: int, request_type: str):
    if request_type == "referrals":
        # Get Referral Link and Count
        user = supabase.table("users").select("valid_referrals").eq("user_id", chat_id).execute()
        if user.data:
            count = user.data[0]['valid_referrals']
            ref_link = f"https://t.me/{BOT_USERNAME}?start={chat_id}"
            await send_telegram_message(chat_id, f"🔗 *Your Referral Link:*\n`{ref_link}`\n\n👥 *Valid Referrals:* {count}")
    
    elif request_type == "leaderboard":
        # Fetch Top 5
        res = supabase.table("users").select("username,valid_referrals")\
            .eq("is_participating", True)\
            .eq("season", CURRENT_SEASON)\
            .order("valid_referrals", desc=True)\
            .limit(5).execute()
        
        board = "\n".join([f"{i+1}. {u['username']}: {u['valid_referrals']}" for i, u in enumerate(res.data)])
        await send_telegram_message(chat_id, f"🏆 *Leaderboard*\n\n{board}")
    
    elif request_type == "stats":
        # Detailed Stats
        user = supabase.table("users").select("*").eq("user_id", chat_id).execute()
        if user.data:
            u = user.data[0]
            status = "✅ Active" if u['is_participating'] else "❌ Inactive"
            msg = f"📊 *My Stats*\n\nStatus: {status}\nReferrals: {u['valid_referrals']}\nSeason: {u['season']}"
            await send_telegram_message(chat_id, msg)

# --- API Endpoints ---

@app.post("/webhook")
async def telegram_webhook(request: Request, background_tasks: BackgroundTasks):
    data = await request.json()
    
    # Handle Callback Queries (Inline Buttons)
    if "callback_query" in data:
        cb = data["callback_query"]
        chat_id = cb["message"]["chat"]["id"]
        
        if cb["data"] == "participate":
            # Delete the "Participate" message so they can't click it twice
            background_tasks.add_task(delete_telegram_message, chat_id, cb["message"]["message_id"])
            background_tasks.add_task(handle_participate, chat_id)
        
        elif cb["data"] == "check_join":
            # Re-run start logic to check membership again
            username = cb["from"].get("username", "Unknown")
            background_tasks.add_task(handle_start, chat_id, username, "", cb["message"]["message_id"])
            
        return {"status": "ok"}

    # Handle Text Messages (Commands & Keyboard)
    if "message" in data:
        msg = data["message"]
        chat_id = msg["chat"]["id"]
        text = msg.get("text", "")
        username = msg["from"].get("username", "Unknown")
        message_id = msg["message_id"]

        if text.startswith("/start"):
            args = text.split(" ")[1] if len(text.split(" ")) > 1 else ""
            background_tasks.add_task(handle_start, chat_id, username, args, message_id)
        
        # Handle Keyboard Inputs
        elif text == "✅ My Referrals":
            background_tasks.add_task(handle_stats_request, chat_id, "referrals")
        elif text == "🏆 Leaderboard":
            background_tasks.add_task(handle_stats_request, chat_id, "leaderboard")
        elif text == "📊 My Stats":
            background_tasks.add_task(handle_stats_request, chat_id, "stats")

    return {"status": "ok"}

@app.get("/validate-users")
async def validate_users_cron():
    """
    CRON ENDPOINT: 
    Checks participating users. If they left the channel, remove them from participating 
    and deduct points from their referrer (Anti-Cheat).
    """
    if not get_season_status():
        return {"status": "Season ended"}

    users = supabase.table("users").select("user_id, referred_by").eq("is_participating", True).limit(50).execute()
    
    processed = 0
    invalidated = 0

    for user in users.data:
        processed += 1
        is_still_member = await check_membership(user["user_id"])
        
        if not is_still_member:
            invalidated += 1
            # 1. Set participating = False
            supabase.table("users").update({"is_participating": False}).eq("user_id", user["user_id"]).execute()
            
            # 2. Deduct referral point from their referrer
            if user["referred_by"]:
                ref_data = supabase.table("users").select("valid_referrals").eq("user_id", user["referred_by"]).execute()
                if ref_data.data:
                    current_count = ref_data.data[0]["valid_referrals"]
                    if current_count > 0:
                        supabase.table("users").update({"valid_referrals": current_count - 1}).eq("user_id", user["referred_by"]).execute()

    return {"status": "success", "processed": processed, "invalidated": invalidated}
