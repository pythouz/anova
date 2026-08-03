"""
Universal Media Downloader — Telegram Bot
يحمل فيديو/صوت من الروابط المدعومة عن طريق yt-dlp.
"""

import os
import json
import time
import logging
import asyncio
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import yt_dlp
from flask import Flask
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ConversationHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# ────────────────────────────── الإعدادات ──────────────────────────────

@dataclass(frozen=True)
class Config:
    api_token: str
    download_dir: Path = Path("downloads")
    users_file: Path = Path("bot_users.json")
    max_file_size_mb: int = 50          # حد تليجرام لإرسال الملفات عن طريق البوت
    keep_alive_port: int = 8080
    supported_qualities: tuple = ("144p", "240p", "360p", "480p", "720p", "1080p")

    @classmethod
    def from_env(cls) -> "Config":
        token = os.getenv("API_TOKEN")
        if not token:
            raise ValueError("API_TOKEN is not set in environment variables.")
        return cls(api_token=token)


logging.basicConfig(
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    level=logging.INFO,
)
logging.getLogger("httpx").setLevel(logging.WARNING)  # تقليل ضوضاء اللوج
logger = logging.getLogger("media_bot")

# حالات المحادثة
CHOOSING_TYPE, CHOOSING_QUALITY = range(2)

# بادئات callback_data
CB_AUDIO = "type:audio"
CB_VIDEO = "type:video"
CB_QUALITY_PREFIX = "quality:"
CB_CANCEL = "cancel"


# ────────────────────────────── تخزين المستخدمين ──────────────────────────────

class UserStore:
    """تخزين بسيط وآمن (thread-safe) لبيانات المستخدمين في ملف JSON."""

    def __init__(self, path: Path):
        self._path = path
        self._lock = threading.Lock()

    def _load(self) -> dict:
        if not self._path.exists():
            return {"users": [], "profiles": {}}
        try:
            with self._path.open("r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("تعذّرت قراءة ملف المستخدمين، هيتعمل ملف جديد: %s", exc)
            return {"users": [], "profiles": {}}

    def _save(self, data: dict) -> None:
        tmp_path = self._path.with_suffix(".tmp")
        with tmp_path.open("w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        tmp_path.replace(self._path)  # كتابة atomic لتجنب تلف الملف

    def track(self, user_id: int, username: Optional[str], first_name: Optional[str]) -> int:
        with self._lock:
            data = self._load()
            uid = str(user_id)
            if uid not in data["users"]:
                data["users"].append(uid)
            data["profiles"][uid] = {
                "username": username or "",
                "first_name": first_name or "",
                "last_activity": time.strftime("%Y-%m-%d %H:%M:%S"),
            }
            self._save(data)
            return len(data["users"])

    def count(self) -> int:
        with self._lock:
            return len(self._load()["users"])


# ────────────────────────────── منطق التحميل ──────────────────────────────

class DownloadError(Exception):
    """خطأ متوقع أثناء التحميل، رسالته آمنة للعرض على المستخدم مباشرة."""


class MediaDownloader:
    def __init__(self, config: Config):
        self.config = config
        config.download_dir.mkdir(parents=True, exist_ok=True)

    def _pick_target_height(self, url: str, requested: Optional[str]) -> Optional[int]:
        if not requested:
            return None
        with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True}) as ydl:
            info = ydl.extract_info(url, download=False)

        heights = sorted({f.get("height") for f in info.get("formats", []) if f.get("height")})
        if not heights:
            return None

        requested_h = int(requested.replace("p", ""))
        higher = [h for h in heights if h >= requested_h]
        return min(higher) if higher else max(heights)

    def _build_opts(self, media_type: str, target_height: Optional[int], timestamp: str) -> dict:
        outtmpl = str(self.config.download_dir / f"{media_type}_{timestamp}.%(ext)s")
        opts = {
            "outtmpl": outtmpl,
            "noplaylist": True,
            "quiet": True,
            "no_warnings": True,
            "http_headers": {
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"
                )
            },
        }

        if media_type == "audio":
            opts["format"] = "bestaudio/best"
            opts["postprocessors"] = [
                {"key": "FFmpegExtractAudio", "preferredcodec": "mp3"}
            ]
        else:
            height_filter = f"[height<={target_height}]" if target_height else ""
            opts["format"] = f"bestvideo{height_filter}+bestaudio/best{height_filter}"

        return opts

    def download(self, url: str, media_type: str, quality: Optional[str] = None) -> tuple[str, Path]:
        """
        يحمل الميديا ويرجّع (العنوان, مسار الملف).
        يرفع DownloadError برسالة مفهومة للمستخدم لو حصلت مشكلة متوقعة.
        """
        timestamp = time.strftime("%Y%m%d-%H%M%S")
        try:
            target_height = self._pick_target_height(url, quality) if media_type == "video" else None
            opts = self._build_opts(media_type, target_height, timestamp)

            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=True)
                file_path = Path(ydl.prepare_filename(info))

            if media_type == "audio":
                file_path = file_path.with_suffix(".mp3")

            if not file_path.exists():
                raise DownloadError("الملف مفيش بعد التحميل، جرّب تاني.")

            return info.get("title", "Unknown"), file_path

        except yt_dlp.utils.DownloadError as exc:
            msg = str(exc)
            if "Unsupported URL" in msg or "is not a valid URL" in msg:
                raise DownloadError("❌ الرابط غير مدعوم أو غير صالح. تأكد منه وحاول تاني.") from exc
            raise DownloadError("❌ تعذّر تحميل هذا الرابط. قد يكون خاص أو محذوف.") from exc
        except Exception as exc:  # أي خطأ غير متوقع
            logger.exception("خطأ غير متوقع أثناء التحميل")
            raise DownloadError("❌ حصل خطأ غير متوقع أثناء التحميل، حاول تاني بعد شوية.") from exc

    def file_size_mb(self, path: Path) -> float:
        return path.stat().st_size / (1024 * 1024)


# ────────────────────────────── واجهة تليجرام ──────────────────────────────

class MediaBot:
    def __init__(self, config: Config):
        self.config = config
        self.users = UserStore(config.users_file)
        self.downloader = MediaDownloader(config)

    # ---------- لوحات المفاتيح (inline) ----------

    @staticmethod
    def _type_keyboard() -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup([
            [
                InlineKeyboardButton("🎧 صوت", callback_data=CB_AUDIO),
                InlineKeyboardButton("🎬 فيديو", callback_data=CB_VIDEO),
            ],
            [InlineKeyboardButton("❌ إلغاء", callback_data=CB_CANCEL)],
        ])

    def _quality_keyboard(self) -> InlineKeyboardMarkup:
        qualities = self.config.supported_qualities
        rows = [
            [
                InlineKeyboardButton(f"🎥 {q}", callback_data=f"{CB_QUALITY_PREFIX}{q}")
                for q in qualities[i:i + 2]
            ]
            for i in range(0, len(qualities), 2)
        ]
        rows.append([InlineKeyboardButton("❌ إلغاء", callback_data=CB_CANCEL)])
        return InlineKeyboardMarkup(rows)

    # ---------- أوامر ----------

    async def start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        user = update.effective_user
        self.users.track(user.id, user.username, user.first_name)
        context.user_data.clear()
        await update.message.reply_text(
            f"أهلاً بيك يا {user.first_name}! 👋\n\n"
            "ابعتلي رابط الفيديو أو الصوت اللي عايز تحمّله."
        )
        return ConversationHandler.END  # هيدخل في المحادثة تاني مع أول رابط

    async def stats(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await update.message.reply_text(f"📊 عدد المستخدمين: {self.users.count()}")

    async def cancel(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        context.user_data.clear()
        query = update.callback_query
        if query:
            await query.answer()
            await query.edit_message_text("تم الإلغاء. ابعت رابط جديد للبدء تاني.")
        else:
            await update.message.reply_text("تم الإلغاء. ابعت رابط جديد للبدء تاني.")
        return ConversationHandler.END

    # ---------- خطوات المحادثة ----------

    async def receive_url(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        user = update.effective_user
        self.users.track(user.id, user.username, user.first_name)

        url = update.message.text.strip()
        if not url.lower().startswith(("http://", "https://")):
            await update.message.reply_text("محتاج رابط صحيح يبدأ بـ http:// أو https://")
            return ConversationHandler.END

        context.user_data["url"] = url
        await update.message.reply_text("اختار نوع التحميل:", reply_markup=self._type_keyboard())
        return CHOOSING_TYPE

    async def choose_type(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        query = update.callback_query
        await query.answer()

        if query.data == CB_AUDIO:
            await self._download_and_send(update, context, media_type="audio")
            return ConversationHandler.END

        if query.data == CB_VIDEO:
            await query.edit_message_text("اختار الجودة:", reply_markup=self._quality_keyboard())
            return CHOOSING_QUALITY

        # ما ينفعش يوصل هنا عمليًا لأن الـ pattern بيفلتر مسبقًا، بس للأمان
        await query.edit_message_text("اختيار غير معروف، ابعت رابط جديد للبدء تاني.")
        return ConversationHandler.END

    async def choose_quality(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        query = update.callback_query
        await query.answer()

        quality = query.data.removeprefix(CB_QUALITY_PREFIX)
        if quality not in self.config.supported_qualities:
            await query.edit_message_text("جودة غير معروفة، ابعت رابط جديد للبدء تاني.")
            return ConversationHandler.END

        await self._download_and_send(update, context, media_type="video", quality=quality)
        return ConversationHandler.END

    # ---------- التحميل والإرسال ----------

    async def _download_and_send(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
        media_type: str,
        quality: Optional[str] = None,
    ) -> None:
        query = update.callback_query
        chat = update.effective_chat
        url = context.user_data.get("url")

        await query.edit_message_text("⏳ جاري التحميل... ممكن ياخد شوية وقت حسب حجم الملف.")

        file_path: Optional[Path] = None
        try:
            # التحميل بيبلوكينج، فبنشغله في executor عشان مايوقفش البوت عن استقبال رسائل تانية
            loop = asyncio.get_running_loop()
            title, file_path = await loop.run_in_executor(
                None, self.downloader.download, url, media_type, quality
            )

            size_mb = self.downloader.file_size_mb(file_path)
            if size_mb > self.config.max_file_size_mb:
                await query.edit_message_text(
                    f"❌ الملف حجمه {size_mb:.1f}MB وده أكبر من حد تليجرام "
                    f"({self.config.max_file_size_mb}MB). جرّب جودة أقل."
                )
                return

            await query.edit_message_text(f"✅ خلص: {title}\nجاري الرفع...")
            with file_path.open("rb") as f:
                if media_type == "audio":
                    await chat.send_audio(f, title=title)
                else:
                    await chat.send_video(f, caption=title, supports_streaming=True)

        except DownloadError as exc:
            await query.edit_message_text(str(exc))
        except Exception:
            logger.exception("خطأ غير متوقع أثناء الإرسال")
            await query.edit_message_text("❌ حصل خطأ غير متوقع. حاول تاني.")
        finally:
            context.user_data.clear()
            if file_path and file_path.exists():
                try:
                    file_path.unlink()
                except OSError as exc:
                    logger.warning("تعذّر حذف الملف المؤقت %s: %s", file_path, exc)

    # ---------- معالج أخطاء عام ----------

    async def on_error(self, update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
        logger.error("Exception أثناء معالجة تحديث %s", update, exc_info=context.error)


# ────────────────────────────── سيرفر keep-alive (Replit + UptimeRobot) ──────────────────────────────

def start_keep_alive_server(port: int) -> None:
    app = Flask(__name__)

    @app.route("/")
    def home():
        return "Bot is alive!"

    def run():
        app.run(host="0.0.0.0", port=port)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()


# ────────────────────────────── نقطة التشغيل ──────────────────────────────

def build_application(config: Config) -> Application:
    bot = MediaBot(config)
    app = Application.builder().token(config.api_token).build()

    cancel_handler = CallbackQueryHandler(bot.cancel, pattern=f"^{CB_CANCEL}$")

    conversation = ConversationHandler(
        entry_points=[MessageHandler(filters.TEXT & ~filters.COMMAND, bot.receive_url)],
        states={
            CHOOSING_TYPE: [
                cancel_handler,
                CallbackQueryHandler(bot.choose_type, pattern=f"^(?:{CB_AUDIO}|{CB_VIDEO})$"),
            ],
            CHOOSING_QUALITY: [
                cancel_handler,
                CallbackQueryHandler(bot.choose_quality, pattern=f"^{CB_QUALITY_PREFIX}"),
            ],
        },
        fallbacks=[
            CommandHandler("cancel", bot.cancel),
            cancel_handler,
        ],
    )

    app.add_handler(CommandHandler("start", bot.start))
    app.add_handler(CommandHandler("stats", bot.stats))
    app.add_handler(conversation)
    app.add_error_handler(bot.on_error)

    return app


def main() -> None:
    config = Config.from_env()
    start_keep_alive_server(config.keep_alive_port)

    application = build_application(config)
    logger.info("Bot started!")
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
