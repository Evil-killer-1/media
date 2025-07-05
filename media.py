import logging
import re
from urllib.parse import quote
from typing import Optional, Tuple
from pathlib import Path
import tempfile

import requests
from bs4 import BeautifulSoup
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    filters,
    ContextTypes,
)
import yt_dlp

# Configure logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# Constants
MAX_FILE_SIZE = 50 * 1024 * 1024  # Telegram's file size limit (50MB)
DOWNLOAD_TIMEOUT = 30  # Seconds to wait for download services

# Supported platforms and their downloader services
PLATFORMS = {
    "youtube": {
        "name": "YouTube",
        "regex": r"(youtube\.com|youtu\.be)",
        "downloader": "https://ssyoutube.com/watch?v={}",
    },
    "tiktok": {
        "name": "TikTok",
        "regex": r"tiktok\.com",
        "downloader": "https://snaptik.app/{}",
    },
    "facebook": {
        "name": "Facebook",
        "regex": r"facebook\.com",
        "downloader": "https://snapsave.app/{}",
    },
    "instagram": {
        "name": "Instagram",
        "regex": r"instagram\.com",
        "downloader": "https://instafinsta.com/{}",
    },
}

class DownloadError(Exception):
    """Custom exception for download failures"""
    pass

def detect_platform(url: str) -> Optional[str]:
    """Detect which platform the URL belongs to"""
    for platform, data in PLATFORMS.items():
        if re.search(data["regex"], url, re.IGNORECASE):
            return platform
    return None

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Send a welcome message when the command /start is issued."""
    user = update.effective_user
    await update.message.reply_text(
        f"Hi {user.first_name}!\n\n"
        "I can download videos/audio from YouTube, TikTok, Facebook, and Instagram.\n\n"
        "Just send me a link and I'll handle the rest!\n\n"
        "Use /help to see available commands."
    )

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Send a help message when the command /help is issued."""
    help_text = (
        "📌 <b>How to use this bot:</b>\n\n"
        "1. Send me a link from:\n"
        "   - YouTube\n"
        "   - TikTok\n"
        "   - Facebook\n"
        "   - Instagram\n\n"
        "2. I'll ask if you want audio or video\n"
        "3. Wait while I process your request\n\n"
        "⚠️ Note: Some services may have size limits\n\n"
        "Commands:\n"
        "/start - Welcome message\n"
        "/help - This message"
    )
    await update.message.reply_text(help_text, parse_mode="HTML")

async def handle_url(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle the URL sent by the user."""
    url = update.message.text.strip()
    platform = detect_platform(url)

    if not platform:
        await update.message.reply_text(
            "❌ Unsupported platform. I only support YouTube, TikTok, Facebook, and Instagram."
        )
        return

    # Store the URL in context for later use
    context.user_data["url"] = url
    context.user_data["platform"] = platform

    # Create transparent buttons (no emoji)
    keyboard = [
        [
            InlineKeyboardButton("Audio", callback_data="audio"),
            InlineKeyboardButton("Video", callback_data="video"),
        ]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    # Send message with buttons
    message = await update.message.reply_text(
        f"🔗 {PLATFORMS[platform]['name']} link detected.\n"
        "Please choose the format you want to download:",
        reply_markup=reply_markup,
    )
    
    # Store message ID for later deletion
    context.user_data["format_message_id"] = message.message_id

async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle the button press for format selection."""
    query = update.callback_query
    await query.answer()

    # Delete the format selection message
    try:
        format_message_id = context.user_data.get("format_message_id")
        if format_message_id:
            await context.bot.delete_message(
                chat_id=query.message.chat_id,
                message_id=format_message_id
            )
    except Exception as e:
        logger.warning(f"Couldn't delete format message: {e}")

    format_choice = query.data  # 'audio' or 'video'
    url = context.user_data.get("url")
    platform = context.user_data.get("platform")

    if not url or not platform:
        await query.edit_message_text("❌ Error: URL not found. Please try again.")
        return

    # Edit the original callback message to show processing status
    await query.edit_message_text(
        f"⏳ Processing your {format_choice} request for {PLATFORMS[platform]['name']}..."
    )

    try:
        # Try to download using the external service first
        file_path, file_type = await download_via_service(
            url, platform, format_choice, context
        )
    except DownloadError as e:
        logger.warning(f"Service download failed: {e}. Trying yt-dlp fallback...")
        try:
            # Fall back to yt-dlp if service fails
            file_path, file_type = await download_via_ytdlp(url, format_choice, context)
        except Exception as e:
            logger.error(f"yt-dlp download failed: {e}")
            await query.edit_message_text(
                "❌ Failed to download media. Please try again later."
            )
            return

    # Check file size
    file_size = file_path.stat().st_size
    if file_size > MAX_FILE_SIZE:
        await query.edit_message_text(
            f"⚠️ File is too large ({file_size/1024/1024:.1f}MB). "
            f"Telegram's limit is {MAX_FILE_SIZE/1024/1024}MB."
        )
        try:
            file_path.unlink()  # Delete the file
        except:
            pass
        return

    # Send the file to the user
    try:
        if file_type == "audio":
            await context.bot.send_audio(
                chat_id=query.message.chat_id,
                audio=open(file_path, "rb"),
                caption="Here's your audio file!",
            )
        else:
            await context.bot.send_video(
                chat_id=query.message.chat_id,
                video=open(file_path, "rb"),
                caption="Here's your video file!",
            )
        await query.edit_message_text("✅ Download complete!")
    except Exception as e:
        logger.error(f"Failed to send file: {e}")
        await query.edit_message_text("❌ Failed to send file. Please try again later.")
    finally:
        try:
            file_path.unlink()  # Clean up
        except:
            pass

async def download_via_service(
    url: str, platform: str, format_choice: str, context: ContextTypes.DEFAULT_TYPE
) -> Tuple[Path, str]:
    """
    Download media using external services (ssyoutube, snaptik, etc.)
    Returns: (file_path, file_type)
    """
    service_url = PLATFORMS[platform]["downloader"].format(quote(url))
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"
    }

    try:
        # First request to get the download page
        response = requests.get(
            service_url, headers=headers, timeout=DOWNLOAD_TIMEOUT
        )
        response.raise_for_status()
    except requests.RequestException as e:
        raise DownloadError(f"Failed to access download service: {e}")

    soup = BeautifulSoup(response.text, "html.parser")

    # Try to find download links - this will vary by service
    download_url = None
    if platform == "youtube":
        # For ssyoutube, look for download buttons
        for link in soup.find_all("a", href=True):
            if "download" in link["href"].lower() and format_choice in link.text.lower():
                download_url = link["href"]
                break
    elif platform in ["tiktok", "facebook", "instagram"]:
        # These services typically have direct download links
        for link in soup.find_all("a", href=True):
            if "download" in link["href"].lower() or "video" in link["href"].lower():
                download_url = link["href"]
                break

    if not download_url:
        raise DownloadError("Could not find download link on service page")

    # Download the actual file
    try:
        response = requests.get(
            download_url, headers=headers, stream=True, timeout=DOWNLOAD_TIMEOUT
        )
        response.raise_for_status()
    except requests.RequestException as e:
        raise DownloadError(f"Failed to download from service: {e}")

    # Determine file extension from Content-Type or URL
    content_type = response.headers.get("Content-Type", "")
    if "audio" in content_type or format_choice == "audio":
        ext = ".mp3"
        file_type = "audio"
    else:
        ext = ".mp4"
        file_type = "video"

    # Save to temporary file
    temp_dir = Path(tempfile.gettempdir())
    file_path = temp_dir / f"download_{context.bot.token[-6:]}{ext}"

    with open(file_path, "wb") as f:
        for chunk in response.iter_content(chunk_size=8192):
            f.write(chunk)

    return file_path, file_type

async def download_via_ytdlp(
    url: str, format_choice: str, context: ContextTypes.DEFAULT_TYPE
) -> Tuple[Path, str]:
    """
    Download media using yt-dlp as a fallback
    Returns: (file_path, file_type)
    """
    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "outtmpl": str(
            Path(tempfile.gettempdir()) / f"ytdlp_{context.bot.token[-6:]}%(ext)s"
        ),
    }

    if format_choice == "audio":
        ydl_opts.update(
            {
                "format": "bestaudio/best",
                "postprocessors": [
                    {
                        "key": "FFmpegExtractAudio",
                        "preferredcodec": "mp3",
                        "preferredquality": "192",
                    }
                ],
            }
        )
        file_type = "audio"
    else:
        ydl_opts["format"] = "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best"
        file_type = "video"

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            file_path = Path(ydl.prepare_filename(info))
            
            # For audio downloads, yt-dlp changes the extension
            if format_choice == "audio":
                file_path = file_path.with_suffix(".mp3")
                
        return file_path, file_type
    except Exception as e:
        raise DownloadError(f"yt-dlp failed: {e}")

def main() -> None:
    """Start the bot."""
    # Create the Application and pass it your bot's token.
    application = Application.builder().token("7587526861:AAHOeB_IvfC_qKJ4V1xsSTw4lgcHV8QYy_o").build()

    # Add handlers
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, handle_url)
    )
    application.add_handler(CallbackQueryHandler(button_callback))

    # Run the bot until the user presses Ctrl-C
    application.run_polling()

if __name__ == "__main__":
    main()