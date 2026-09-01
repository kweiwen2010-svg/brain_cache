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

# Supabase 專用的 HTTP Headers（直接使用 REST API，繞過 SDK 驗證限制）
SUPABASE_HEADERS = {}

def get_system_instruction():
    """動態生成包含當前正確時間的系統指令，徹底解決 AI 的時空錯亂問題"""
    current_date_str = datetime.now().strftime("%Y年%m月%d日")
    return f"""
你是一個專為主人 Vincent 服務的智慧大腦快取秘書。
【重要時間校正】：今天是 {current_date_str}。請記住現在就是 2026 年，絕對不要把 2026 年的任何歷史紀錄誤認為是『未來』！

你的核心任務有兩個：
1. 【日常碎碎念提煉（寫入模式）】：當主人向你傾倒生活瑣碎、情緒或待辦時，幫他進行『大腦排毒與提煉』。
   格式必須包含：
   - 一句幽默、輕鬆調侃但絕對忠誠的垃圾話吐槽或安慰。
   - 【✨ 核心提煉】：將重點用極簡條列式整理（不超過3條）。

2. 【歷史黑盒子查詢（查詢模式）】：當主人的提問是在翻舊帳、詢問過去某天做了什麼、或是找之前提過的事物時（例如問：某天做了啥、哪一天清理神明廳、翻一下以前的紀錄等）。
   - 你必須仔細閱讀系統附帶給你的『歷史黑盒子紀錄』。
   - 幫主人精確找出他想要的答案與當時的『紀錄時間』。如果檔案裡真的有，不准裝傻說不知道或說那是未來！
"""

def read_supabase_history() -> str:
    """從 Supabase 雲端（透過 REST API）讀取歷史紀錄，組合成文字 Context 給 AI 參考"""
    try:
        url = f"{SUPABASE_URL}/rest/v1/brain_box?select=*&order=created_at.desc&limit=50"
        response = requests.get(url, headers=SUPABASE_HEADERS)
        
        if response.status_code != 200:
            return f"[無法從雲端讀取歷史紀錄: HTTP {response.status_code} - {response.text}]"
            
        records = response.json()
        if not records:
            return "[目前沒有任何歷史紀錄]"
        
        # 讓時間由舊到新排序，方便 AI 閱讀脈絡
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
    await update.message.reply_text("🤖 雲端大腦快取黑盒子：Supabase REST API 架構已全面上線！")

async def handle_inbox(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_message = ""
    await update.message.reply_chat_action(action="typing")
    
    # 從雲端 Supabase 讀取完整的歷史紀錄庫
    history_context = read_supabase_history()
    
    # 1. 語音訊息處理
    if update.message.voice:
        voice_file = await context.bot.get_file(update.message.voice.file_id)
        temp_voice_path = "temp_voice.ogg"
        await voice_file.download_to_drive(temp_voice_path)
        
        try:
            with open(temp_voice_path, 'rb') as f:
                audio_bytes = f.read()
            
            full_prompt = f"請聽這段語音。如果內容是查詢過去的事，請從下方的歷史紀錄庫找出答案；如果是日常交代，請進行提煉。\n\n【歷史黑盒子紀錄如下】:\n{history_context}"
            
            response = ai_client.models.generate_content(
                model='gemini-2.5-flash',
                contents=[types.Part.from_bytes(data=audio_bytes, mime_type="audio/ogg"), full_prompt],
                config=types.GenerateContentConfig(system_instruction=get_system_instruction(), temperature=0.2)
            )
            ai_reply = response.text
            
            is_asking_history = False 
            user_message = "[語音訊息紀錄]"
            
        except Exception as e:
            ai_reply = f"❌ 語音解析出錯：({str(e)})"
            is_asking_history = True
        finally:
            if os.path.exists(temp_voice_path):
                os.remove(temp_voice_path)
                
    # 2. 文字訊息處理
    else:
        user_message = update.message.text
        
        # 雙層 AI 決策機制
        intent_prompt = f"""
        請幫我判定下面這句主人說的話，他的真正意圖是什麼？
        主人說：『{user_message}』
        
        如果他是想【查詢過去的歷史、問某天做了什麼、找以前記過的事情、問哪一天完成了某事】，請精確回覆：QUERY
        如果他只是在【碎碎念、倒垃圾、交代日常、記錄現在剛發生的事、新增待辦事項】，請精確回覆：WRITE
        
        注意：只能回覆 QUERY 或 WRITE，不要有任何其他字眼。
        """
        try:
            intent_res = ai_client.models.generate_content(
                model='gemini-2.5-flash',
                contents=intent_prompt
            )
            intent = intent_res.text.strip().upper()
            is_asking_history = ("QUERY" in intent)
        except Exception:
            is_asking_history = False 
            
        try:
            if is_asking_history:
                input_content = f"主人正在查詢歷史，提問如下：\n『{user_message}』\n\n【以下是為您調出的歷史黑盒子檔案（包含精確時間戳記）】:\n{history_context}\n\n請根據檔案內容，精確回答主人的問題。"
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
            is_asking_history = True

    # 3. 唯有在判定為寫入模式時，才將對話透過 REST API 寫入 Supabase 雲端資料庫
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


# 建立一個極輕量的假伺服器來應付 Render 的 Port 檢測
class SimpleHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Bot is running!")

def run_dummy_server():
    port = int(os.environ.get("PORT", 10000))
    server = HTTPServer(("0.0.0.0", port), SimpleHandler)
    server.serve_forever()

def main():
    global SUPABASE_HEADERS
    if not TELEGRAM_BOT_TOKEN or not GEMINI_API_KEY or not SUPABASE_URL or not SUPABASE_KEY:
        logging.error("環境變數缺失（包含 Supabase 網址或金鑰），請檢查 .env 檔案！")
        return
    
    # 設定好 REST API 用的 Headers
    SUPABASE_HEADERS = {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
        "Prefer": "return=minimal"
    }
    
    # 在背景啟動假 HTTP 伺服器以滿足 Render Web Service 的 Port 需求
    server_thread = threading.Thread(target=run_dummy_server, daemon=True)
    server_thread.start()
    
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT | filters.VOICE, handle_inbox))
    app.run_polling()

if __name__ == "__main__":
    main()