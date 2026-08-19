import os
import time
import json
import asyncio
import threading
import subprocess
import yt_dlp
from telegram import Update, ReplyKeyboardMarkup, ReplyKeyboardRemove
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

# Set up download directory
DOWNLOAD_PATH = os.path.join(os.getcwd(), 'downloads')
if not os.path.exists(DOWNLOAD_PATH):
    os.makedirs(DOWNLOAD_PATH)

# User data storage
USERS_FILE = 'bot_users.json'

# حد تليجرام الأقصى لإرسال الملفات عن طريق البوت (بالميجابايت)
MAX_FILE_SIZE_MB = 50

# آي دي الأدمن
ADMIN_ID = os.getenv('ADMIN_ID')

# إعدادات تجاوز فحص يوتيوب
YOUTUBE_EXTRACTOR_ARGS = {'youtube': {'player_client': ['android', 'web']}}

def load_users():
    """Load users from the JSON file"""
    if os.path.exists(USERS_FILE):
        try:
            with open(USERS_FILE, 'r') as f:
                return json.load(f)
        except json.JSONDecodeError:
            return {'users': [], 'total_count': 0}
    return {'users': [], 'total_count': 0}

def save_users(users_data):
    """Save users to the JSON file"""
    with open(USERS_FILE, 'w') as f:
        json.dump(users_data, f)

def track_user(user_id, username, first_name):
    """Track a user who interacted with the bot"""
    users_data = load_users()
    
    if str(user_id) not in users_data['users']:
        users_data['users'].append(str(user_id))
        users_data['total_count'] = len(users_data['users'])
    
    user_info = {
        'username': username or '',
        'first_name': first_name or '',
        'last_activity': time.strftime("%Y-%m-%d %H:%M:%S")
    }
    users_data[str(user_id)] = user_info
    
    save_users(users_data)
    return users_data['total_count']

def get_user_count():
    """Get the total number of unique users"""
    users_data = load_users()
    return users_data['total_count']

def _run_git(*args):
    """يشغّل أمر git ويرجع True لو نجح"""
    try:
        subprocess.run(['git', *args], check=True, capture_output=True, text=True)
        return True
    except Exception as exc:
        print(f"⚠️ git {' '.join(args)} فشل: {exc}")
        return False

def sync_users_file_loop():
    """حفظ وتزامن بيانات المستخدمين كل 5 دقائق"""
    while True:
        time.sleep(300)
        if not os.path.exists(USERS_FILE):
            continue
        _run_git('add', USERS_FILE)
        committed = _run_git('commit', '-m', 'Auto-update users data [skip ci]')
        if committed:
            _run_git('push')

def start_users_sync():
    thread = threading.Thread(target=sync_users_file_loop, daemon=True)
    thread.start()

def get_users_list():
    """Get a formatted list of all users"""
    users_data = load_users()
    lines = []
    for uid in users_data['users']:
        info = users_data.get(uid, {})
        username = info.get('username', '')
        first_name = info.get('first_name', 'Unknown')
        last_activity = info.get('last_activity', '-')
        tag = f"@{username}" if username else f"id:{uid}"
        lines.append(f"• {first_name} ({tag}) — آخر نشاط: {last_activity}")
    return lines

def download_media(url, media_type='video', video_quality=None):
    """Download media from URL with specified quality options"""
    timestamp = time.strftime("%Y%m%d-%H%M%S")
    try:
        probe_opts = {'quiet': True, 'extractor_args': YOUTUBE_EXTRACTOR_ARGS}
        with yt_dlp.YoutubeDL(probe_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            formats = info.get('formats', [])
            available_heights = sorted(set(f.get('height', 0) for f in formats if f.get('height')))
            
            requested_height = int(video_quality.replace('p', '')) if video_quality else None
            
            if requested_height:
                higher_qualities = [h for h in available_heights if h >= requested_height]
                lower_qualities = [h for h in available_heights if h <= requested_height]
                
                if higher_qualities:
                    target_height = min(higher_qualities)
                elif lower_qualities:
                    target_height = max(lower_qualities)
                else:
                    target_height = available_heights[0]
            else:
                target_height = max(available_heights) if available_heights else 720

        ydl_opts = {
            'format': f'bestvideo[height<={target_height}]+bestaudio/best[height<={target_height}]',
            'outtmpl': os.path.join(DOWNLOAD_PATH, f'{media_type}_{timestamp}.%(ext)s'),
            'noplaylist': True,
            'quiet': True,
            'extractor_args': YOUTUBE_EXTRACTOR_ARGS,
            'http_headers': {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
            }
        }

        if media_type == 'audio':
            ydl_opts.update({
                'postprocessors': [{
                    'key': 'FFmpegExtractAudio',
                    'preferredcodec': 'mp3',
                }]
            })

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info_dict = ydl.extract_info(url, download=True)
            file_name = ydl.prepare_filename(info_dict)
            
            if media_type == 'audio':
                converted_file = os.path.splitext(file_name)[0] + '.mp3'
                if os.path.exists(converted_file):
                    return f"Successfully downloaded audio: {info_dict.get('title', 'Unknown')}", converted_file
                return "Error: Audio conversion failed.", None
            
            return f"Successfully downloaded: {info_dict.get('title', 'Unknown')}", file_name

    except Exception as e:
        error_message = str(e)
        if "is not a valid URL" in error_message or "Unsupported URL" in error_message:
            return "❌ تحقق من الرابط. يبدو أن الرابط الذي أدخلته غير صالح.", None
        return f"❌ Error during download: {error_message}", None

def get_file_size_mb(file_path):
    return os.path.getsize(file_path) / (1024 * 1024)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    track_user(user.id, user.username, user.first_name)
    
    context.user_data.clear()
    await update.message.reply_text(
        f"Welcome to the Universal Media Downloader, {user.first_name}! 👋\n\n"
        "Please enter the URL of the media you want to download:",
        reply_markup=ReplyKeyboardRemove()
    )

async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_count = get_user_count()
    await update.message.reply_text(
        f"📊 Bot Statistics\n\n"
        f"Total Users: {user_count}"
    )

async def myid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"الآي دي بتاعك هو: {update.effective_user.id}")

async def users_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not ADMIN_ID or str(user.id) != str(ADMIN_ID):
        await update.message.reply_text("❌ الأمر ده للأدمن بس.")
        return

    lines = get_users_list()
    if not lines:
        await update.message.reply_text("مفيش مستخدمين مسجلين لسه.")
        return

    header = f"👥 عدد المشتركين: {len(lines)}\n\n"
    body = "\n".join(lines)
    full_message = header + body

    for i in range(0, len(full_message), 4000):
        await update.message.reply_text(full_message[i:i + 4000])

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    track_user(user.id, user.username, user.first_name)
    
    text = update.message.text.strip()

    if text.lower() in ['cancel', 'close', '❌ cancel']:
        context.user_data.clear()
        await update.message.reply_text(
            "Operation canceled. Please enter a new URL to start again.",
            reply_markup=ReplyKeyboardRemove()
        )
        return

    user_data = context.user_data
    if 'url' not in user_data:
        user_data['url'] = text
        keyboard = [["🎧 Audio", "🎬 Video"], ["❌ Cancel"]]
        reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True, one_time_keyboard=True)
        await update.message.reply_text("Choose media type:", reply_markup=reply_markup)
    
    elif 'media_type' not in user_data:
        if text.lower() in ['🎧 audio', 'audio']:
            user_data['media_type'] = 'audio'
            status_message = await update.message.reply_text("⏳ Downloading audio... Please wait.")
            
            # تشغيل التحميل في Thread منفصل لمنع تجميد البوت
            message, file_path = await asyncio.to_thread(
                download_media, user_data['url'], media_type='audio'
            )
            
            if "Error" in message or "❌" in message:
                await status_message.edit_text(f"{message}")
            elif file_path and os.path.exists(file_path):
                size_mb = get_file_size_mb(file_path)
                if size_mb > MAX_FILE_SIZE_MB:
                    await status_message.edit_text(
                        f"❌ حجم الملف {size_mb:.1f}MB وده أكبر من حد تليجرام ({MAX_FILE_SIZE_MB}MB).\n"
                        "جرّب فيديو أقصر."
                    )
                    os.remove(file_path)
                else:
                    await status_message.edit_text(f"✅ {message}\nجاري الرفع...")
                    with open(file_path, 'rb') as file:
                        await update.message.reply_audio(file)
                    os.remove(file_path)
            else:
                await status_message.edit_text("❌ File not found after download. Please try again.")
            context.user_data.clear()
        
        elif text.lower() in ['🎬 video', 'video']:
            user_data['media_type'] = 'video'
            keyboard = [
                ["🎥 144p", "🎥 240p"],
                ["🎥 360p", "🎥 480p"],
                ["🎥 720p", "🎥 1080p"],
                ["❌ Cancel"]
            ]
            reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True, one_time_keyboard=True)
            await update.message.reply_text(
                "Select video quality:\n"
                "⚠️ ملحوظة: الفيديوهات الطويلة أو بجودة عالية ممكن يتعدى حجمها 50MB "
                "(حد تليجرام)، فينصح تختار جودة أقل للفيديوهات الطويلة.",
                reply_markup=reply_markup
            )
        else:
            await update.message.reply_text("Invalid choice. Please choose '🎧 Audio' or '🎬 Video'.")
    
    elif 'video_quality' not in user_data:
        supported_qualities = ["144p", "240p", "360p", "480p", "720p", "1080p"]
        if text in [f"🎥 {q}" for q in supported_qualities]:
            selected_quality = text.replace("🎥 ", "")
            user_data['video_quality'] = selected_quality
            
            status_message = await update.message.reply_text("⏳ Downloading video... Please wait.")
            
            # تشغيل التحميل في Thread منفصل لمنع تجميد البوت
            message, file_path = await asyncio.to_thread(
                download_media,
                user_data['url'], 
                media_type='video', 
                video_quality=selected_quality
            )
            
            if "Error" in message or "❌" in message:
                await status_message.edit_text(f"{message}")
            elif file_path and os.path.exists(file_path):
                size_mb = get_file_size_mb(file_path)
                if size_mb > MAX_FILE_SIZE_MB:
                    await status_message.edit_text(
                        f"❌ حجم الملف {size_mb:.1f}MB وده أكبر من حد تليجرام ({MAX_FILE_SIZE_MB}MB).\n"
                        "جرّب جودة أقل (144p أو 240p) خصوصًا لو الفيديو طويل."
                    )
                    os.remove(file_path)
                else:
                    await status_message.edit_text(f"✅ {message}\nجاري الرفع...")
                    with open(file_path, 'rb') as file:
                        await update.message.reply_video(file)
                    os.remove(file_path)
            else:
                await status_message.edit_text("❌ File not found after download. Please try again.")
            context.user_data.clear()
        else:
            await update.message.reply_text("Invalid video quality choice. Please select a valid option.")

def main():
    API_TOKEN = os.getenv('API_TOKEN')
    if not API_TOKEN:
        raise ValueError("API_TOKEN is not set in environment variables.")

    application = Application.builder().token(API_TOKEN).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("stats", stats))
    application.add_handler(CommandHandler("myid", myid))
    application.add_handler(CommandHandler("users", users_list))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    
    start_users_sync()
    print("Bot started!")
    application.run_polling()

if __name__ == '__main__':
    main()
