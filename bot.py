import os
import time
import json
import yt_dlp
from telegram import Update, ReplyKeyboardMarkup, ReplyKeyboardRemove
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

# Set up download directory
DOWNLOAD_PATH = os.path.join(os.getcwd(), 'downloads')
if not os.path.exists(DOWNLOAD_PATH):
    os.makedirs(DOWNLOAD_PATH)

# User data storage
USERS_FILE = 'bot_users.json'

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

def download_media(url, media_type='video', video_quality=None):
    """Download media from URL with specified quality options"""
    timestamp = time.strftime("%Y%m%d-%H%M%S")
    try:
        # Extract available formats to determine the best match for the requested quality
        with yt_dlp.YoutubeDL({'quiet': True}) as ydl:
            info = ydl.extract_info(url, download=False)
            formats = info.get('formats', [])
            available_heights = sorted(set(f.get('height', 0) for f in formats if f.get('height')))
            
            # Parse the requested quality (e.g., "720p" -> 720)
            requested_height = int(video_quality.replace('p', '')) if video_quality else None
            
            # Find the closest available height to the requested quality
            if requested_height:
                higher_qualities = [h for h in available_heights if h >= requested_height]
                lower_qualities = [h for h in available_heights if h <= requested_height]
                
                if higher_qualities:
                    target_height = min(higher_qualities)
                elif lower_qualities:
                    target_height = max(lower_qualities)
                else:
                    target_height = available_heights[0]  # Default to the lowest available quality
            else:
                target_height = max(available_heights)  # Default to the highest available quality

        # Define yt-dlp options based on the selected quality
        ydl_opts = {
            'format': f'bestvideo[height<={target_height}]+bestaudio/best[height<={target_height}]',
            'outtmpl': os.path.join(DOWNLOAD_PATH, f'{media_type}_{timestamp}.%(ext)s'),
            'noplaylist': True,
            'quiet': True,
            'http_headers': {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
            }
        }

        # Handle audio post-processing
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

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Start command handler"""
    user = update.effective_user
    track_user(user.id, user.username, user.first_name)
    
    context.user_data.clear()
    await update.message.reply_text(
        f"Welcome to the Universal Media Downloader, {user.first_name}! 👋\n\n"
        "Please enter the URL of the media you want to download:",
        reply_markup=ReplyKeyboardRemove()
    )

async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin command to show bot statistics"""
    user_count = get_user_count()
    await update.message.reply_text(
        f"📊 Bot Statistics\n\n"
        f"Total Users: {user_count}"
    )

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle incoming messages"""
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
            
            message, file_path = download_media(user_data['url'], media_type='audio')
            
            if "Error" in message:
                await status_message.edit_text(f"❌ {message}")
            else:
                await status_message.edit_text(f"✅ {message}")
                if file_path and os.path.exists(file_path):
                    with open(file_path, 'rb') as file:
                        await update.message.reply_audio(file)
                    os.remove(file_path)
                else:
                    await update.message.reply_text("❌ File not found after download. Please try again.")
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
            await update.message.reply_text("Select video quality:", reply_markup=reply_markup)
        
        else:
            await update.message.reply_text("Invalid choice. Please choose '🎧 Audio' or '🎬 Video'.")
    
    elif 'video_quality' not in user_data:
        supported_qualities = ["144p", "240p", "360p", "480p", "720p", "1080p"]
        if text in [f"🎥 {q}" for q in supported_qualities]:
            selected_quality = text.replace("🎥 ", "")
            user_data['video_quality'] = selected_quality
            
            status_message = await update.message.reply_text("⏳ Downloading video... Please wait.")
            
            message, file_path = download_media(
                user_data['url'], 
                media_type='video', 
                video_quality=selected_quality
            )
            
            if "Error" in message:
                await status_message.edit_text(f"❌ {message}")
            else:
                await status_message.edit_text(f"✅ {message}")
                if file_path and os.path.exists(file_path):
                    with open(file_path, 'rb') as file:
                        await update.message.reply_video(file)
                    os.remove(file_path)
                else:
                    await status_message.edit_text("❌ File not found after download. Please try again.")
            context.user_data.clear()
        
        else:
            await update.message.reply_text("Invalid video quality choice. Please select a valid option.")

def main():
    """Run the bot"""
    API_TOKEN = os.getenv('API_TOKEN')
    if not API_TOKEN:
        raise ValueError("API_TOKEN is not set in environment variables.")

    application = Application.builder().token(API_TOKEN).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("stats", stats))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    
    print("Bot started!")
    application.run_polling()

if __name__ == '__main__':
    main()
