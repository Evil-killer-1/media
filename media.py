import logging
import re
import asyncio
from pathlib import Path
import tempfile
from urllib.parse import quote
from typing import Optional, Tuple
import random

import requests
from bs4 import BeautifulSoup
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes,
)
import yt_dlp

# Configure logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Constants
MAX_FILE_SIZE = 50 * 1024 * 1024  # 50MB Telegram limit
DOWNLOAD_TIMEOUT = 60  # Increased timeout for downloads

# Loading animation components
LOADING_STEPS = [
    "🔄 Connecting to {platform}...",
    "🔄 Analyzing link...",
    "🔄 Finding best quality...",
    "🔄 Preparing download...",
    "🔄 Downloading media... {progress}",
    "🔄 Almost done...",
]

# Supported platforms
PLATFORMS = {
    "youtube": {
        "name": "YouTube",
        "regex": r"(youtube\.com|youtu\.be)",
        "downloader": "https://ssyoutube.com/watch?v={}"
    },
    "tiktok": {
        "name": "TikTok",
        "regex": r"tiktok\.com",
        "downloader": "https://snaptik.app/{}"
    },
    "facebook": {
        "name": "Facebook",
        "regex": r"facebook\.com",
        "downloader": "https://snapsave.app/{}"
    },
    "instagram": {
        "name": "Instagram",
        "regex": r"instagram\.com",
        "downloader": "https://instafinsta.com/{}"
    }
}

class DownloadError(Exception):
    pass

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "🌟 Welcome to Media Downloader Bot!\n\n"
        "Send me a link from:\n"
        "- YouTube\n- TikTok\n- Facebook\n- Instagram\n\n"
        "I'll download it for you!"
    )

async def animate_loading(update: Update, context: ContextTypes.DEFAULT_TYPE, platform: str, msg_id: int):
    """Show animated loading progress"""
    progress_bars = ["▰▱▱▱▱", "▰▰▱▱▱", "▰▰▰▱▱", "▰▰▰▰▱", "▰▰▰▰▰"]
    
    for step in LOADING_STEPS:
        for i, bar in enumerate(progress_bars, 1):
            progress = f"{i*20}% {bar}"
            text = step.format(platform=platform, progress=progress)
            
            try:
                await context.bot.edit_message_text(
                    chat_id=update.message.chat_id,
                    message_id=msg_id,
                    text=text
                )
                await asyncio.sleep(0.5 + random.random()*0.5)  # Random delay
            except:
                return  # Stop if message was deleted

async def handle_url(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    url = update.message.text.strip()
    platform = detect_platform(url)
    
    if not platform:
        await update.message.reply_text("❌ Unsupported platform")
        return

    # Send initial message and start animation
    platform_name = PLATFORMS[platform]["name"]
    msg = await update.message.reply_text(f"🔄 Starting {platform_name} download...")
    animation_task = asyncio.create_task(
        animate_loading(update, context, platform_name, msg.message_id)
    )

    try:
        # Try external service first
        try:
            file_path, file_type = await download_via_service(url, platform, context)
        except DownloadError as e:
            logger.warning(f"Service failed: {e}")
            await msg.edit_text("🔄 Falling back to direct download...")
            file_path, file_type = await download_via_ytdlp(url, context)

        # Check file size
        file_size = file_path.stat().st_size
        if file_size > MAX_FILE_SIZE:
            await msg.edit_text(
                f"⚠️ File too large ({file_size/1024/1024:.1f}MB > {MAX_FILE_SIZE/1024/1024}MB)"
            )
            file_path.unlink(missing_ok=True)
            return

        # Upload the file with final status
        await msg.edit_text("✅ Uploading your file...")
        if file_type == "audio":
            await update.message.reply_audio(
                audio=open(file_path, "rb"),
                caption="🎵 Your audio is ready!"
            )
        else:
            await update.message.reply_video(
                video=open(file_path, "rb"),
                caption="🎬 Your video is ready!"
            )
        
        await msg.delete()

    except Exception as e:
        logger.error(f"Download failed: {e}")
        await msg.edit_text("❌ Download failed. Please try again.")
    finally:
        animation_task.cancel()
        file_path.unlink(missing_ok=True)

async def download_via_service(url: str, platform: str, context: ContextTypes.DEFAULT_TYPE) -> Tuple[Path, str]:
    """Download using external services"""
    service_url = PLATFORMS[platform]["downloader"].format(quote(url))
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36",
        "Referer": service_url
    }

    try:
        # Get download page
        response = requests.get(service_url, headers=headers, timeout=DOWNLOAD_TIMEOUT)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "html.parser")

        # Find download link - this is service-specific
        download_url = None
        for link in soup.find_all("a", href=True):
            href = link["href"].lower()
            if "download" in href or "video" in href or "mp4" in href:
                if not download_url or "hd" in href or "high" in href:  # Prefer higher quality
                    download_url = link["href"]
                    if not download_url.startswith("http"):
                        download_url = f"https://{PLATFORMS[platform]['downloader'].split('/')[2]}{download_url}"

        if not download_url:
            raise DownloadError("No download link found")

        # Download the file
        response = requests.get(download_url, headers=headers, stream=True, timeout=DOWNLOAD_TIMEOUT)
        response.raise_for_status()

        # Determine file type
        content_type = response.headers.get("Content-Type", "")
        ext = ".mp3" if "audio" in content_type else ".mp4"
        file_type = "audio" if ext == ".mp3" else "video"

        # Save to temp file
        file_path = Path(tempfile.gettempdir()) / f"dl_{context.bot.token[-6:]}{ext}"
        with open(file_path, "wb") as f:
            for chunk in response.iter_content(chunk_size=8192):
                f.write(chunk)

        return file_path, file_type

    except requests.RequestException as e:
        raise DownloadError(f"Service error: {e}")

async def download_via_ytdlp(url: str, context: ContextTypes.DEFAULT_TYPE) -> Tuple[Path, str]:
    """Fallback download using yt-dlp"""
    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "outtmpl": str(Path(tempfile.gettempdir()) / f"ytdlp_{context.bot.token[-6:]}%(ext)s"),
        "format": "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            file_path = Path(ydl.prepare_filename(info))
            return file_path, "video"
    except Exception as e:
        raise DownloadError(f"yt-dlp error: {e}")

def detect_platform(url: str) -> Optional[str]:
    """Detect which platform the URL belongs to"""
    for platform, data in PLATFORMS.items():
        if re.search(data["regex"], url, re.IGNORECASE):
            return platform
    return None

def main() -> None:
    application = Application.builder().token("7587526861:AAHOeB_IvfC_qKJ4V1xsSTw4lgcHV8QYy_o").build()
    
    application.add_handler(CommandHandler("start", start))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_url))
    
    application.run_polling()

if __name__ == "__main__":
    main()
