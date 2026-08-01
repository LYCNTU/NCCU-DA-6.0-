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
from google import genai

app = FastAPI()

# 從環境變數讀取金鑰
LINE_CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET")
LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

handler = WebhookHandler(LINE_CHANNEL_SECRET)
configuration = Configuration(access_token=LINE_CHANNEL_ACCESS_TOKEN)
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# 初始化 Gemini SDK
ai_client = genai.Client(api_key=GEMINI_API_KEY) if GEMINI_API_KEY else None

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
        # 1. 先取得發言者暱稱，並將當前訊息存入 Supabase
        # ---------------------------------------------------------
        user_name = "成員"
        try:
            profile = line_bot_api.get_group_member_profile(group_id, user_id)
            user_name = profile.display_name
        except Exception:
            pass

        supabase.table("group_messages").insert({
            "group_id": group_id,
            "user_id": user_id,
            "user_name": user_name,
            "message_text": msg_text
        }).execute()

        # ---------------------------------------------------------
        # 2. 功能：關鍵字搜尋 (!search 關鍵字)
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
                for row in reversed(rows):
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
        # 3. 功能：AI 語意問答與自動整理 (!ask 或訊息包含指令)
        # ---------------------------------------------------------
        # 判斷是否要觸發 AI 整理 (支援 !ask 指令或是訊息中包含 "@bot" 關鍵字)
        if msg_text.startswith("!ask") or "!問" in msg_text:
            if not ai_client:
                line_bot_api.reply_message(
                    ReplyMessageRequest(
                        reply_token=event.reply_token,
                        messages=[TextMessage(text="⚠️ 尚未設定 GEMINI_API_KEY，無法使用 AI 整理功能。")]
                    )
                )
                return

            # 清理問題字樣
            question = msg_text.replace("!ask", "").replace("!問", "").strip()
            if not question:
                question = "請幫忙整理群組最近討論的重點。"

            # 從 Supabase 抓取群組最近 50 筆對話紀錄
            history_response = supabase.table("group_messages") \
                .select("user_name, message_text, created_at") \
                .eq("group_id", group_id) \
                .order("created_at", desc=True) \
                .limit(50) \
                .execute()

            history_rows = history_response.data or []
            # 將撈出來的舊訊息倒轉為按時間順序
            history_rows.reverse()

            # 組裝聊天紀錄上下文
            formatted_history = "\n".join([
                f"[{row.get('user_name', '成員')}]: {row.get('message_text', '')}"
                for row in history_rows
            ])

            prompt = f"""
            你是一個群組助理。請根據以下提供群組近期的歷史聊天紀錄，簡短重點回答使用者的問題。

            【規則】：
            1. 若紀錄中有相關資訊（例如：連結、地點、特定時間、檔案位置），請直接整理出結果。
            2. 若紀錄中完全找不到相關資訊，請明確且有禮貌地說明「歷史紀錄中目前沒有提到相關資訊」。
            3. 請保持回答簡潔，條列式說明。

            【歷史聊天紀錄】：
            {formatted_history}

            【使用者的提問】：
            {question}
            """

            try:
                ai_response = ai_client.models.generate_content(
                    model='gemini-2.5-flash',
                    contents=prompt,
                )
                ai_reply = ai_response.text.strip()
            except Exception as e:
                ai_reply = f"🤖 AI 處理時發生錯誤：{str(e)}"

            line_bot_api.reply_message(
                ReplyMessageRequest(
                    reply_token=event.reply_token,
                    messages=[TextMessage(text=ai_reply)]
                )
            )
            return
