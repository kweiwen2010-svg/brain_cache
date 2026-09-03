import os
import logging
from datetime import datetime, timedelta
from dotenv import load_dotenv
from telegram import Update, Bot
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from google import genai
from google.genai import types
import requests
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from apscheduler.schedulers.background import BackgroundScheduler
import asyncio
import json
import re

# 載入環境變數
load_dotenv()
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

# 設定日誌
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)

# 初始化 Gemini 客戶端
ai_client = genai.Client(api_key=GEMINI_API_KEY)

# Supabase 專用的 HTTP Headers
SUPABASE_HEADERS = {}

# 全域宣告 APScheduler 排程器
scheduler = BackgroundScheduler()

def get_system_instruction():
    """動態生成包含當前正確時間的系統指令"""
    current_date_str = datetime.now().strftime("%Y年%m月%d日")
    return f"""
你是一個專為主人 Vincent 服務的智慧大腦快取秘書。
【重要時間校正】：今天是 {current_date_str}。請記住現在就是 2026 年，絕對不要把 2026 年的任何歷史紀錄誤認為是『未來』！

你的核心任務：
1. 【日常碎碎念與提醒設定】：當主人向你傾倒生活瑣碎、待辦、或是要求『提醒』時：
   - 給予一句幽默、輕鬆調侃但絕對忠誠的垃圾話吐槽或安慰。
   - 【✨ 核心提煉】：將重點用極簡條列式整理。
"""

def read_supabase_history() -> str:
    """從 Supabase 雲端讀取歷史紀錄"""
    try:
        url = f"{SUPABASE_URL}/rest/v1/brain_box?select=*&order=created_at.desc&limit=50"
        response = requests.get(url, headers=SUPABASE_HEADERS)
        
        if response.status_code != 200:
            return f"[無法從雲端讀取歷史紀錄: HTTP {response.status_code}]"
            
        records = response.json()
        if not records:
            return "[目前沒有任何歷史紀錄]"
        
        records.reverse()
        history_str = ""
        for row in records:
            history_str += f"--- 紀錄時間: {row.get('created_at')} ---\n"
            history_str += f"原始輸入: {row.get('user_message')}\n"
            history_str += f"AI 提煉結構:\n{row.get('ai_reply')}\n\n"
            
        return history_str
    except Exception as e:
        return f"[無法從雲端讀取歷史紀錄: {str(e)}]"

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    await update.message.reply_text(f"🤖 雲端大腦快取黑盒子：智慧動態提醒架構已全面上線！\n（你的 Chat ID: `{chat_id}`）", parse_mode="Markdown")

async def send_push_message(bot_token: str, chat_id: str, message: str):
    """執行實際的主動推播"""
    try:
        bot = Bot(token=bot_token)
        await bot.send_message(chat_id=chat_id, text=f"🔔【大腦快取主動提醒】：{message}")
        logging.info(f"成功發送主動提醒給 {chat_id}: {message}")
    except Exception as e:
        logging.error(f"主動推播失敗: {e}")

def run_push_job(bot_token: str, chat_id: str, message: str):
    """供 APScheduler 呼叫的包裝函數"""
    asyncio.run(send_push_message(bot_token, chat_id, message))

async def handle_inbox(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_message = update.message.text
    chat_id = update.effective_chat.id
    if not user_message:
        return

    await update.message.reply_chat_action(action="typing")
    history_context = read_supabase_history()
    current_time_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # 1. 智慧意圖與提醒解析 (Intent & Reminder Parser)
    parsing_prompt = f"""
    現在時間是: {current_time_str}
    請分析主人說的一句話：『{user_message}』
    
    請判斷這是否包含「設定提醒、多久後提醒、幾點幾分提醒」的需求。
    請嚴格回傳一個 JSON 格式（不要有 markdown 程式碼區塊語法，直接回傳純 JSON 字串）：
    {{
      "is_reminder": true 或 false,
      "delay_minutes": 數字 (如果是相對時間，例如 3分鐘後就填 3，半小時填 30。若不是相對時間則填 0),
      "target_time": "YYYY-MM-DD HH:MM:SS" (如果是絕對時間，請依據當前時間推算出正確的目標時間字串；若不是絕對時間填 ""),
      "reminder_content": "要提醒的具體事情內容",
      "intent": "QUERY" 或 "WRITE" 或 "REMINDER"
    }}
    """
    
    try:
        parse_res = ai_client.models.generate_content(
            model='gemini-2.5-flash',
            contents=parsing_prompt
        )
        clean_text = re.sub(r```(?:json)?\s*([\s\S]*?)\s*```, r"\1", parse_res.text.strip())
        parsed_data = json.loads(clean_text)
    except Exception as e:
        logging.error(f"解析意圖失敗: {e}")
        parsed_data = {"is_reminder": False, "intent": "WRITE"}

    is_asking_history = (parsed_data.get("intent") == "QUERY")
    is_reminder = parsed_data.get("is_reminder", False)

    # 2. 處理回覆內容生成
    try:
        if is_asking_history:
            input_content = f"主人正在查詢歷史，提問如下：\n『{user_message}』\n\n【以下是為您調出的歷史黑盒子檔案】:\n{history_context}\n\n請根據檔案內容，精確回答主人的問題。"
        elif is_reminder:
            content_desc = parsed_data.get("reminder_content", user_message)
            input_content = f"主人設定了一個動態提醒：『{user_message}』。\n請依據系統指令給予幽默吐槽，並在核心提煉中條列：1. 設定動態提醒。 2. 提醒內容：{content_desc}。"
        else:
            input_content = f"主人正在記錄新日常：\n『{user_message}』\n\n請依據系統指令進行排毒與提煉。"
            
        response = ai_client.models.generate_content(
            model='gemini-2.5-flash',
            contents=input_content,
            config=types.GenerateContentConfig(system_instruction=get_system_instruction(), temperature=0.3)
        )
        ai_reply = response.text
    except Exception as e:
        ai_reply = f"❌ AI 腦袋卡住：({str(e)})"

    # 3. 如果是提醒，動態加入 APScheduler 排程
    if is_reminder:
        reminder_content = parsed_data.get("reminder_content", user_message)
        delay_min = parsed_data.get("delay_minutes", 0)
        target_time_str = parsed_data.get("target_time", "")

        run_date = None
        if delay_min and delay_min > 0:
            run_date = datetime.now() + timedelta(minutes=float(delay_min))
        elif target_time_str:
            try:
                run_date = datetime.strptime(target_time_str, "%Y-%m-%d %H:%M:%S")
            except Exception:
                pass

        if run_date and run_date > datetime.now():
            scheduler.add_job(
                run_push_job,
                'date',
                run_date=run_date,
                args=[TELEGRAM_BOT_TOKEN, str(chat_id), reminder_content]
            )
            logging.info(f"成功動態排程提醒於: {run_date}，內容: {reminder_content}")

    # 4. 寫入 Supabase 雲端資料庫
    if not is_asking_history and user_message:
        try:
            url = f"{SUPABASE_URL}/rest/v1/brain_box"
            payload = {
                "user_message": user_message,
                "ai_reply": ai_reply,
                "entry_type": "telegram_chat"
            }
            res = requests.post(url, headers=SUPABASE_HEADERS, json=payload)
            if res.status_code not in [200, 201]:
                logging.error(f"寫入 Supabase 失敗: {res.text}")
        except Exception as e:
            logging.error(f"寫入 Supabase 失敗: {e}")

    await update.message.reply_text(ai_reply)

# 保持 Render 服務在線的 HTTP 伺服器
class SimpleHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        response_text = b"Bot is running!"
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(response_text)))
        self.end_headers()
        self.wfile.write(response_text)

    def do_HEAD(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()

    def log_message(self, format, *args):
        return

def run_dummy_server():
    port = int(os.environ.get("PORT", 10000))
    server = HTTPServer(("0.0.0.0", port), SimpleHandler)
    server.serve_forever()

def main():
    global SUPABASE_HEADERS, scheduler
    if not TELEGRAM_BOT_TOKEN or not GEMINI_API_KEY or not SUPABASE_URL or not SUPABASE_KEY:
        logging.error("環境變數缺失，請檢查設定！")
        return
    
    SUPABASE_HEADERS = {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
        "Prefer": "return=minimal"
    }
    
    # 啟動背景 HTTP 伺服器
    server_thread = threading.Thread(target=run_dummy_server, daemon=True)
    server_thread.start()
    
    # 啟動 APScheduler
    scheduler.start()
    logging.info("APScheduler 智慧動態排程核心已啟動！")

    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_inbox))
    
    try:
        app.run_polling()
    finally:
        if scheduler.running:
            scheduler.shutdown()

if __name__ == "__main__":
    main()