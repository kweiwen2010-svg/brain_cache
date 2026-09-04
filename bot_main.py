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
        clean_text = re.sub(r'^