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
    """自動抓取網頁文字內容（純內建/requests處理，不依賴外部套件）"""
    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        }
        response = requests.get(url, headers=headers, timeout=10)
        if response.status_code != 200:
            return f"[無法讀取網頁，HTTP 狀態碼: {response.status_code}]"
        
        html_text = response.text
        # 用正則表達式簡單去除 HTML 標籤抓取文字
        clean_text = re.sub(r'<script.*?>.*?</script>', '', html_text, flags=re.DOTALL)
        clean_text = re.sub(r'<style.*?>.*?</style>', '', clean_text, flags=re.DOTALL)
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
            clean_text = re.sub(r'^```(?:json)?\s*([\s\S]*?)\s*