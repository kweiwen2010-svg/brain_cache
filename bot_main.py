import os
import logging
from datetime import datetime
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from google import genai
from google.genai import types
import requests
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
import pytz
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

# 強制設定台灣時區
TAIPEI_TZ = pytz.timezone('Asia/Taipei')

def get_system_instruction():
    """動態生成包含當前正確時間的系統指令"""
    current_date_str = datetime.now(TAIPEI_TZ).strftime("%Y年%m月%d日")
    return f"""
你是一個專為主人 Vincent 服務的智慧大腦快取秘書。
【重要時間校正】：今天是 {current_date_str}（台灣時間）。

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
    await update.message.reply_text(f"🤖 雲端大腦快取黑盒子：秒數倒數 JobQueue 提醒架構已上線！\n（你的 Chat ID: `{chat_id}`）", parse_mode="Markdown")

# JobQueue 觸發時執行的回呼函數
async def alarm_callback(context: ContextTypes.DEFAULT_TYPE):
    job = context.job
    chat_id = job.chat_id
    message = job.data
    try:
        await context.bot.send_message(chat_id=chat_id, text=f"🔔【大腦快取主動提醒】：{message}")
        logging.info(f"成功發送主動提醒給 {chat_id}: {message}")
    except Exception as e:
        logging.error(f"主動推播失敗: {e}")

async def handle_inbox(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_message = update.message.text
    chat_id = update.effective_chat.id
    if not user_message:
        return

    await update.message.reply_chat_action(action="typing")
    history_context = read_supabase_history()
    
    now_taipei = datetime.now(TAIPEI_TZ)
    current_time_str = now_taipei.strftime("%Y-%m-%d %H:%M:%S")
    current_date_date_str = now_taipei.strftime("%Y-%m-%d")

    # 絕對時間解析提示詞
    parsing_prompt = f"""
    現在台灣時間是: {current_time_str} (日期是: {current_date_date_str})
    請分析主人說的一句話：『{user_message}』
    
    請判斷這是否包含「指定某個絕對時間點提醒」的需求（例如：「15:30 提醒我開會」、「晚上8點叫我吃藥」）。
    請嚴格回傳一個合法的 JSON 格式（不要加上任何 markdown 標籤如 ```json，直接回傳純文字 JSON）：
    {{
      "is_reminder": true 或 false,
      "target_time": "YYYY-MM-DD HH:MM:SS" (請務必將主人口述的時間轉換為絕對時間點。例如今天 15:30 就填 "{current_date_date_str} 15:30:00"。若不是絕對時間填 ""),
      "reminder_content": "要提醒的具體事情內容",
      "intent": "QUERY" 或 "WRITE" 或 "REMINDER"
    }}
    """
    
    try:
        parse_res = ai_client.models.generate_content(
            model='gemini-2.5-flash',
            contents=parsing_prompt
        )
        raw_res = parse_res.text.strip()
        clean_text = re.sub(r'^```(?:json)?\s*([\s\S]*?)\s*```$', r'\1', raw_res)
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
            target_t = parsed_data.get("target_time", "")
            input_content = f"主人設定了一個絕對時間提醒：『{user_message}』（預定時間：{target_t}）。\n請依據系統指令給予幽默吐槽，並在核心提煉中條列：1. 設定絕對時間提醒。 2. 預定時間：{target_t}。 3. 提醒內容：{content_desc}。"
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

    # 3. 如果是絕對時間提醒，計算秒數並透過 JobQueue 排程
    if is_reminder:
        reminder_content = parsed_data.get("reminder_content", user_message)
        target_time_str = parsed_data.get("target_time", "")

        run_date = None
        if target_time_str:
            try:
                naive_dt = datetime.strptime(target_time_str, "%Y-%m-%d %H:%M:%S")
                run_date = TAIPEI_TZ.localize(naive_dt)
            except Exception as ex:
                logging.error(f"時間轉換錯誤: {ex}")

        if run_date and run_date > now_taipei:
            seconds_to_wait = (run_date - now_taipei).total_seconds()
            context.job_queue.run_once(
                alarm_callback,
                when=seconds_to_wait,
                chat_id=chat_id,
                data=reminder_content
            )
            logging.info(f"成功透過 JobQueue 排程，將於 {seconds_to_wait:.1f} 秒後推播: {reminder_content}")

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
    global SUPABASE_HEADERS
    if not TELEGRAM_BOT_TOKEN or not GEMINI_API_KEY or not SUPABASE_URL or not SUPABASE_KEY:
        logging.error("環境變數缺失，請檢查設定！")
        return
    
    SUPABASE_HEADERS = {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
        "Prefer": "return=minimal"
    }
    
    server_thread = threading.Thread(target=run_dummy_server, daemon=True)
    server_thread.start()

    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_inbox))
    
    logging.info("Telegram Bot 內建 JobQueue (秒數倒數版) 啟動中...")
    app.run_polling()

if __name__ == "__main__":
    main()