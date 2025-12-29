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
CHANNEL_ID = os.getenv("REQUIRED_CHANNEL_ID") # e.g., "@MyCryptoChannel"
CURRENT_SEASON = "Season 2"

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
    """Checks if Season 2 is active and not expired."""
    response = supabase.table("seasons").select("*").eq("season_name", CURRENT_SEASON).execute()
    if not response.data:
        return False
    
    season = response.data[0]
    end_date = datetime.fromisoformat(season["end_date"].replace('Z', '+00:00'))
    now = datetime.now(timezone.utc)
    
    return season["is_active"] and now < end_date

# --- Bot Logic Handlers ---

async def handle_start(chat_id: int, username: str, args: str):
    # 1. Check Season Status
    if not get_season_status():
        await send_telegram_message(chat_id, f"🚫 *{CURRENT_SEASON}* has ended. Stay tuned for the next season!")
        return

    # 2. Parse Referrer
    referrer_id = None
    if args and args.isdigit() and int(args) != chat_id:
        referrer_id = int(args)

    # 3. Upsert User (Don't overwrite participation status if exists)
    user_data = {
        "user_id": chat_id,
        "username": username,
        "season": CURRENT_SEASON
    }
    # Only add referrer if user is new
    existing = supabase.table("users").select("user_id").eq("user_id", chat_id).execute()
    if not existing.data and referrer_id:
        # Verify referrer exists
        ref_check = supabase.table("users").select("user_id").eq("user_id", referrer_id).execute()
        if ref_check.data:
            user_data["referred_by"] = referrer_id

    supabase.table("users").upsert(user_data, on_conflict="user_id").execute()

    # 4. Send Welcome & Participation Button
    keyboard = {
        "inline_keyboard": [[{"text": "✅ Participate Now", "callback_data": "participate"}]]
    }
    msg = (
        f"🏆 *Welcome to the 100 USDT Giveaway ({CURRENT_SEASON})!*\n\n"
        f"1. Join our channel: {CHANNEL_ID}\n"
        f"2. Click 'Participate' below.\n"
        f"3. Get your referral link!\n\n"
        f"⚠️ _Rules: You must stay in the channel until Jan 2._"
    )
    await send_telegram_message(chat_id, msg, keyboard)

async def handle_participate(chat_id: int):
    # 1. Check Season
    if not get_season_status():
        await send_telegram_message(chat_id, "🚫 Season is over.")
        return

    # 2. Check Channel Membership
    is_member = await check_membership(chat_id)
    if not is_member:
        await send_telegram_message(chat_id, f"❌ You must join {CHANNEL_ID} first, then click Participate again.")
        return

    # 3. Update Status
    user = supabase.table("users").select("*").eq("user_id", chat_id).execute()
    if not user.data:
        await send_telegram_message(chat_id, "⚠️ Please type /start first.")
        return
    
    current_user = user.data[0]
    
    if current_user["is_participating"]:
        await send_telegram_message(chat_id, "✅ You are already participating!")
    else:
        # Mark participating
        supabase.table("users").update({"is_participating": True}).eq("user_id", chat_id).execute()
        
        # Credit Referrer (if exists and valid)
        if current_user["referred_by"]:
            # Increment referrer count safely using RPC or logic (Logic used here for simplicity)
            # Fetch referrer
            ref_data = supabase.table("users").select("valid_referrals").eq("user_id", current_user["referred_by"]).execute()
            if ref_data.data:
                new_count = ref_data.data[0]["valid_referrals"] + 1
                supabase.table("users").update({"valid_referrals": new_count}).eq("user_id", current_user["referred_by"]).execute()

        # Send Referral Link
        ref_link = f"https://t.me/PrizeLedgerBot?start={chat_id}"
        await send_telegram_message(chat_id, f"🎉 *You are in!*\n\nYour Referral Link:\n`{ref_link}`\n\nShare this to climb the leaderboard!")

# --- API Endpoints ---

@app.post("/webhook")
async def telegram_webhook(request: Request, background_tasks: BackgroundTasks):
    """Main entry point for Telegram updates."""
    data = await request.json()
    
    # Handle Callback Queries (Buttons)
    if "callback_query" in data:
        cb = data["callback_query"]
        chat_id = cb["message"]["chat"]["id"]
        if cb["data"] == "participate":
            background_tasks.add_task(handle_participate, chat_id)
        return {"status": "ok"}

    # Handle Text Messages
    if "message" in data:
        msg = data["message"]
        chat_id = msg["chat"]["id"]
        text = msg.get("text", "")
        username = msg["from"].get("username", "Unknown")

        if text.startswith("/start"):
            args = text.split(" ")[1] if len(text.split(" ")) > 1 else ""
            background_tasks.add_task(handle_start, chat_id, username, args)
        
        elif text == "/leaderboard":
            # Fetch Top 5
            res = supabase.table("users").select("username,valid_referrals")\
                .eq("is_participating", True)\
                .eq("season", CURRENT_SEASON)\
                .order("valid_referrals", desc=True)\
                .limit(5).execute()
            
            board = "\n".join([f"{i+1}. {u['username']}: {u['valid_referrals']}" for i, u in enumerate(res.data)])
            await send_telegram_message(chat_id, f"🏆 *Leaderboard*\n\n{board}")

    return {"status": "ok"}

@app.get("/validate-users")
async def validate_users_cron():
    """
    CRON ENDPOINT: Runs via cronjob.org
    Checks participating users to see if they left the channel.
    """
    if not get_season_status():
        return {"status": "Season ended, validation stopped"}

    # Fetch batch of participating users (Limit to avoid Vercel timeout)
    # In production, use pagination. checking 50 users per run here.
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

    return {
        "status": "success",
        "processed": processed,
        "invalidated": invalidated
    }

@app.get("/")
def health_check():
    return {"status": "Bot is running", "season": CURRENT_SEASON}
