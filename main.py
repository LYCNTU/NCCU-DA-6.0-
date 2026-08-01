import os
from fastapi import FastAPI, Request, HTTPException, Header
from linebot.v3 import WebhookHandler
from linebot.v3.exceptions import InvalidSignatureError
from linebot.v3.messaging import (
    Configuration,
    ApiClient,
    MessagingApi,
    ReplyMessageRequest,
    TextMessage
)
from linebot.v3.webhooks import MessageEvent, TextMessageContent, GroupSource
from supabase import create_client, Client

app = FastAPI()

# 從環境變數讀取金鑰
LINE_CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET")
LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

handler = WebhookHandler(LINE_CHANNEL_SECRET)
configuration = Configuration(access_token=LINE_CHANNEL_ACCESS_TOKEN)
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

@app.get("/")
def read_root():
    return {"status": "LINE Bot is running!"}

@app.post("/callback")
async def callback(request: Request, x_line_signature: str = Header(None)):
    body = await request.body()
    body_str = body.decode('utf-8')

    try:
        handler.handle(body_str, x_line_signature)
    except InvalidSignatureError:
        raise HTTPException(status_code=400, detail="Invalid signature")

    return "OK"

@handler.add(MessageEvent, message=TextMessageContent)
def handle_text_message(event):
    # 確保訊息來自群組 (Group)
    if not isinstance(event.source, GroupSource):
        return

    group_id = event.source.group_id
    user_id = event.source.user_id or "unknown"
    msg_text = event.message.text.strip()

    with ApiClient(configuration) as api_client:
        line_bot_api = MessagingApi(api_client)

        # ---------------------------------------------------------
        # 功能：指令搜尋 (!search 關鍵字)
        # ---------------------------------------------------------
        if msg_text.startswith("!search"):
            keyword = msg_text.replace("!search", "").strip()
            if not keyword:
                line_bot_api.reply_message(
                    ReplyMessageRequest(
                        reply_token=event.reply_token,
                        messages=[TextMessage(text="請輸入要搜尋的關鍵字，例如：!search 開會")]
                    )
                )
                return

            # 從 Supabase 查詢含有該關鍵字的最新 5 條紀錄
            response = supabase.table("group_messages") \
                .select("user_name, message_text, created_at") \
                .eq("group_id", group_id) \
                .ilike("message_text", f"%{keyword}%") \
                .order("created_at", desc=True) \
                .limit(5) \
                .execute()

            rows = response.data
            if not rows:
                reply_str = f"🔍 找不到與「{keyword}」相關的訊息紀錄。"
            else:
                reply_str = f"🔍 找到以下與「{keyword}」相關的最近訊息：\n"
                for row in reversed(rows): # 按時間由舊到新排列呈現
                    name = row.get("user_name") or "成員"
                    text = row.get("message_text")
                    reply_str += f"\n• [{name}]: {text}"

            line_bot_api.reply_message(
                ReplyMessageRequest(
                    reply_token=event.reply_token,
                    messages=[TextMessage(text=reply_str.strip())]
                )
            )
            return

        # ---------------------------------------------------------
        # 功能：一般訊息自動寫入 Supabase
        # ---------------------------------------------------------
        # 嘗試取得發言者的 LINE 暱稱
        user_name = "成員"
        try:
            profile = line_bot_api.get_group_member_profile(group_id, user_id)
            user_name = profile.display_name
        except Exception:
            pass

        # 存入 Supabase
        supabase.table("group_messages").insert({
            "group_id": group_id,
            "user_id": user_id,
            "user_name": user_name,
            "message_text": msg_text
        }).execute()
