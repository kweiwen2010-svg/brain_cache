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
import urllib.parse
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
    await update.message.reply_text(f"🤖 雲端大腦快取黑盒子：Supabase 雲端排程架構已上線！\n（你的 Chat ID: `{chat_id}`）", parse_mode="Markdown")

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

    # 1. 鐵律：用正規表達式精準捕捉時間（支援 12:23、9:26 等格式）
    extracted_target_time = ""
    reminder_content = user_message
    
    time_match = re.search(r'(\d{1,2})\s*[:：]\s*(\d{1,2})', user_message)
    if time_match:
        hour, minute = time_match.groups()
        extracted_target_time = f"{current_date_str} {int(hour):02d}:{int(minute):02d}:00"
        
        # 自動把時間字串從提醒內容中濾掉，讓內容更乾淨（例如把 "12:23發音提醒 我動一動" 轉成 "動一動"）
        cleaned_content = re.sub(r'\d{1,2}\s*[:：]\s*\d{1,2}', '', user_message)
        cleaned_content = re.sub(r'發音提醒|提醒我|提醒|我', '', cleaned_content).strip()
        if cleaned_content:
            reminder_content = cleaned_content

    # 2. 如果成功抓到時間，直接強制判定為提醒
    if extracted_target_time:
        is_reminder = True
        target_time_str = extracted_target_time
        is_asking_history = False
        input_content = f"主人設定了一個絕對時間提醒：『{user_message}』（預定時間：{target_time_str}，內容：{reminder_content}）。\n請依據系統指令給予幽默吐槽，並在核心提煉中條列：1. 設定絕對時間提醒。 2. 預定時間：{target_time_str}。 3. 提醒內容：{reminder_content}。"
    else:
        # 如果沒抓到時間，交給 AI 判斷是歷史查詢還是普通記錄
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
            clean_text = re.sub(r'^```(?:json)?\s*([\s\S]*?)\s*```$', r'\1', raw_res)
            parsed_data = json.loads(clean_text)
            is_asking_history = (parsed_data.get("intent") == "QUERY")
        except Exception:
            is_asking_history = False
            
        is_reminder = False
        target_time_str = ""

    # 3. 處理 AI 回覆內容生成
    try:
        if is_asking_history:
            input_content = f"主人正在查詢歷史，提問如下：\n『{user_message}』\n\n【以下是為您調出的歷史黑盒子檔案】:\n{history_context}\n\n請根據檔案內容，精確回答主人的問題。"
        elif not is_reminder:
            input_content = f"主人正在記錄新日常：\n『{user_message}』\n\n請依據系統指令進行排毒與提煉。"
            
        response = ai_client.models.generate_content(
            model='gemini-2.5-flash',
            contents=input_content,
            config=types.GenerateContentConfig(system_instruction=get_system_instruction(), temperature=0.3)
        )
        ai_reply = response.text
    except Exception as e:
        ai_reply = f"❌ AI 腦袋卡住：({str(e)})"

    # 4. 如果是提醒，寫入 Supabase 的 reminders 資料表
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

    # 5. 寫入 Supabase 歷史聊天資料庫
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
