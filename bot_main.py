import os
import logging
from datetime import datetime, timezone, timedelta
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from google import genai
from google.genai import types
import requests
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
import urllib.parse
import json
import re
import traceback

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

# 強制設定台灣時區 (UTC+8)
TAIPEI_TZ = timezone(timedelta(hours=8))

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
2. 【網頁摘要模式】：當主人貼上網頁連結並要求解讀時，請結合你抓取到的網頁實際內容，為主人提供精準、有條理的摘要，並維持你一貫幽默風趣的秘書口吻。
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

def fetch_web_page_content(url: str) -> str:
    """自動抓取網頁文字內容"""
    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        }
        response = requests.get(url, headers=headers, timeout=10)
        if response.status_code != 200:
            return f"[無法讀取網頁，HTTP 狀態碼: {response.status_code}]"
        
        html_text = response.text
        clean_text = re.sub(r'<script.*?>.*?</script>', '', html_text, flags=re.DOTALL)
        clean_text = re.sub(r'<style.*?>.*?</style>', '', html_text, flags=re.DOTALL)
        clean_text = re.sub(r'<[^>]+>', ' ', clean_text)
        clean_text = re.sub(r'\s+', ' ', clean_text).strip()
        
        if len(clean_text) > 4000:
            clean_text = clean_text[:4000] + "...(內容過長已截斷)"
        return clean_text
    except Exception as e:
        return f"[網頁抓取發生錯誤: {str(e)}]"

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    await update.message.reply_text(f"🤖 雲端大腦快取黑盒子升級版：支援自動網頁爬蟲與摘要！\n（你的 Chat ID: `{chat_id}`）", parse_mode="Markdown")

async def handle_inbox(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_message = update.message.text
    chat_id = update.effective_chat.id
    if not user_message:
        return

    await update.message.reply_chat_action(action="typing")
    history_context = read_supabase_history()
    
    now_taipei = datetime.now(TAIPEI_TZ)
    current_time_str = now_taipei.strftime("%Y-%m-%d %H:%M:%S")
    current_date_str = now_taipei.strftime("%Y-%m-%d")

    # 1. 檢查訊息中是否包含網址
    url_match = re.search(r'https?://[^\s]+', user_message)
    scraped_content = ""
    is_url_request = False

    if url_match:
        target_url = url_match.group(0)
        is_url_request = True
        logging.info(f"偵測到網址，開始爬取: {target_url}")
        scraped_content = fetch_web_page_content(target_url)

    # 2. 時間捕捉（提醒設定）
    extracted_target_time = ""
    reminder_content = user_message
    
    time_match = re.search(r'(\d{1,2})\s*[:：]\s*(\d{1,2})', user_message)
    if time_match and not is_url_request:
        hour, minute = time_match.groups()
        extracted_target_time = f"{current_date_str} {int(hour):02d}:{int(minute):02d}:00"
        
        cleaned_content = re.sub(r'\d{1,2}\s*[:：]\s*\d{1,2}', '', user_message)
        cleaned_content = re.sub(r'發音提醒|提醒我|提醒|我', '', cleaned_content).strip()
        if cleaned_content:
            reminder_content = cleaned_content

    # 3. 意圖判斷
    if extracted_target_time:
        is_reminder = True
        target_time_str = extracted_target_time
        is_asking_history = False
        input_content = f"主人設定了一個絕對時間提醒：『{user_message}』（預定時間：{target_time_str}，內容：{reminder_content}）。\n請依據系統指令給予幽默吐槽，並在核心提煉中條列：1. 設定絕對時間提醒。 2. 預定時間：{target_time_str}。 3. 提醒內容：{reminder_content}。"
    elif is_url_request:
        is_reminder = False
        target_time_str = ""
        is_asking_history = False
        input_content = f"主人貼出了一個網址，並附帶訊息：『{user_message}』。\n\n以下是系統自動幫忙爬取到的網頁實際內容：\n{scraped_content}\n\n請根據網頁內容與主人的訊息，給予幽默的吐槽或回應，並在【核心提煉】中條列出這篇網頁的重點摘要！"
    else:
        parsing_prompt = f"""
        現在台灣時間是: {current_time_str} (日期是: {current_date_str})
        請分析主人說的一句話：『{user_message}』
        請嚴格回傳一個合法的 JSON 格式（不要加上任何 markdown 標籤，直接回傳純文字 JSON）：
        {{
          "intent": "QUERY" 或 "WRITE"
        }}
        """
        try:
            parse_res = ai_client.models.generate_content(
                model='gemini-2.5-flash',
                contents=parsing_prompt
            )
            raw_res = parse_res.text.strip()
            clean_text = raw_res
            if "{" in clean_text and "}" in clean_text:
                start_idx = clean_text.find("{")
                end_idx = clean_text.rfind("}") + 1
                clean_text = clean_text[start_idx:end_idx]
            parsed_data = json.loads(clean_text)
            is_asking_history = (parsed_data.get("intent") == "QUERY")
        except Exception:
            is_asking_history = False
            
        is_reminder = False
        target_time_str = ""

    # 4. 處理 AI 回覆內容生成
    try:
        if is_asking_history:
            input_content = f"主人正在查詢歷史，提問如下：\n『{user_message}』\n\n【以下是為您調出的歷史黑盒子檔案】:\n{history_context}\n\n請根據檔案內容，精確回答主人的問題。"
        elif not is_reminder and not is_url_request:
            input_content = f"主人正在記錄新日常：\n『{user_message}』\n\n請依據系統指令進行排毒與提煉。"
            
        response = ai_client.models.generate_content(
            model='gemini-2.5-flash',
            contents=input_content,
            config=types.GenerateContentConfig(system_instruction=get_system_instruction(), temperature=0.3)
        )
        ai_reply = response.text
    except Exception as e:
        ai_reply = f"❌ AI 腦袋卡住：({str(e)})"

    # 5. 如果是提醒，寫入 Supabase 的 reminders 資料表
    if is_reminder and target_time_str:
        try:
            url = f"{SUPABASE_URL}/rest/v1/reminders"
            payload = {
                "chat_id": str(chat_id),
                "target_time": target_time_str,
                "content": reminder_content,
                "is_sent": False
            }
            res = requests.post(url, headers=SUPABASE_HEADERS, json=payload)
            if res.status_code in [200, 201]:
                logging.info(f"成功將提醒存入 Supabase 雲端: {target_time_str} - {reminder_content}")
            else:
                logging.error(f"存入 Supabase 提醒失敗: {res.text}")
        except Exception as e:
            logging.error(f"存入 Supabase 提醒例外: {e}")

    # 6. 寫入 Supabase 歷史聊天資料庫
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

# 支援 /check 路由的 HTTP 伺服器
class SimpleHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        parsed_path = urllib.parse.urlparse(self.path)
        if parsed_path.path == "/check":
            triggered_count = check_and_send_reminders()
            response_text = f"Checked reminders. Triggered: {triggered_count}".encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(response_text)))
            self.end_headers()
            self.wfile.write(response_text)
        else:
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

def check_and_send_reminders() -> int:
    """檢查 Supabase 中時間已到且未發送的提醒"""
    try:
        now_taipei = datetime.now(TAIPEI_TZ)
        now_str = now_taipei.strftime("%Y-%m-%d %H:%M:%S")
        
        url = f"{SUPABASE_URL}/rest/v1/reminders?is_sent=eq.false&target_time=lte.{now_str}&select=*"
        res = requests.get(url, headers=SUPABASE_HEADERS)
        if res.status_code != 200:
            return 0
            
        reminders = res.json()
        count = 0
        for rem in reminders:
            rem_id = rem.get("id")
            chat_id = rem.get("chat_id")
            content = rem.get("content")
            
            tg_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
            # 加上 disable_notification=False 確保一定有音效與彈出通知
            payload = {
                "chat_id": chat_id,
                "text": f"🚨 **【大腦快取緊急呼叫】** 🚨\n\n{content}",
                "parse_mode": "Markdown",
                "disable_notification": False
            }
            tg_res = requests.post(tg_url, json=payload, timeout=10)
            if tg_res.status_code == 200:
                update_url = f"{SUPABASE_URL}/rest/v1/reminders?id=eq.{rem_id}"
                requests.patch(update_url, headers=SUPABASE_HEADERS, json={"is_sent": True})
                count += 1
                logging.info(f"成功透過 /check 觸發強效推播給 {chat_id}: {content}")
            else:
                logging.error(f"推播失敗: {tg_res.text}")
        return count
    except Exception as e:
        logging.error(f"檢查提醒例外錯誤: {e}")
        return 0

def run_dummy_server():
    try:
        port = int(os.environ.get("PORT", 10000))
        server = HTTPServer(("0.0.0.0", port), SimpleHandler)
        logging.info(f"HTTP 伺服器已在連接埠 {port} 啟動")
        server.serve_forever()
    except Exception as e:
        logging.error(f"HTTP 伺服器啟動失敗: {e}")

def main():
    try:
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
        
        logging.info("Telegram Bot 雲端 Supabase 提醒與網頁爬蟲架構啟動中...")
        app.run_polling()
    except Exception as e:
        logging.error(f"主程式啟動發生重大例外: {e}")
        traceback.print_exc()

if __name__ == "__main__":
    main()