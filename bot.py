import os
import subprocess
import logging
import time
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
import yt_dlp

logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)

TOKEN = os.environ.get("TELEGRAM_TOKEN", "YOUR_TOKEN_HERE")
DOWNLOAD_DIR = "/tmp"
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

def is_valid_video(path):
    if not os.path.exists(path) or os.path.getsize(path) < 10000:
        return False
    cmd = ['ffprobe', '-v', 'error', '-select_streams', 'v:0', '-show_entries', 'stream=codec_type', '-of', 'default=noprint_wrappers=1:nokey=1', path]
    try:
        out = subprocess.check_output(cmd, stderr=subprocess.STDOUT).decode().strip()
        return out == 'video'
    except:
        return False

def download_video(url):
    ydl_opts = {
        'format': 'best[ext=mp4]/best',
        'outtmpl': f'{DOWNLOAD_DIR}/%(title)s.%(ext)s',
        'quiet': True,
        'postprocessors': [{'key': 'FFmpegVideoConvertor', 'preferedformat': 'mp4'}],
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)
        filename = ydl.prepare_filename(info)
        if not filename.endswith('.mp4'):
            base = os.path.splitext(filename)[0]
            if os.path.exists(base + '.mp4'):
                filename = base + '.mp4'
        title = info.get('title', 'Video')
        description = info.get('description', '')
        uploader = info.get('uploader', 'Unknown')
        tags = info.get('tags', [])
        views = info.get('view_count', 0)
        likes = info.get('like_count', 0)
        duration = info.get('duration', 0)
        return filename, title, description, uploader, tags, views, likes, duration

def split_video(input_path, clip_duration=30):
    cmd = ['ffprobe', '-v', 'error', '-show_entries', 'format=duration', '-of', 'default=noprint_wrappers=1:nokey=1', input_path]
    try:
        duration = float(subprocess.check_output(cmd, text=True).strip())
    except:
        return [], 0
    clips = []
    for i, start in enumerate(range(0, int(duration), clip_duration)):
        end = min(start + clip_duration, duration)
        clip_path = f"{DOWNLOAD_DIR}/clip_{i:02d}.mp4"
        cmd = ['ffmpeg', '-i', input_path, '-ss', str(start), '-to', str(end), '-c', 'copy', clip_path, '-y']
        subprocess.run(cmd, capture_output=True)
        if is_valid_video(clip_path):
            clips.append(clip_path)
    return clips, int(duration)

def generate_caption(title, description, uploader, tags, views, likes, part, total):
    hashtags = []
    if tags:
        hashtags = [f"#{tag.replace(' ', '').replace('-', '')}" for tag in tags[:3]]
    else:
        hashtags = ["#viral", "#trending", "#shorts"]
    if uploader and uploader != "Unknown":
        hashtags.append(f"#{uploader.replace(' ', '').replace('-', '')}")
    caption = f"🎥 **{title[:100]}**\n\n"
    if description:
        clean_desc = ' '.join(description.split())[:200]
        caption += f"{clean_desc}...\n\n"
    caption += f"👤 **{uploader}**\n👁️ {views:,} views | ❤️ {likes:,} likes\n\n{' '.join(hashtags)}\n\n📌 **Part {part}/{total}**"
    return caption

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🎬 **Video Clipper Bot**\n\n"
        "Send any YouTube link. I'll:\n"
        "• Download video\n"
        "• Create 30-second clips\n"
        "• Send each clip with metadata and hashtags\n\n"
        "✅ No modifications, just split and send."
    )

async def handle_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    url = update.message.text
    if 'youtube.com' not in url and 'youtu.be' not in url:
        await update.message.reply_text("❌ Send a valid YouTube link.")
        return

    status = await update.message.reply_text("📥 Downloading video...")
    try:
        video_path, title, desc, uploader, tags, views, likes, duration = download_video(url)
        if not is_valid_video(video_path):
            await status.edit_text("❌ Download failed.")
            return
    except Exception as e:
        await status.edit_text(f"❌ Error: {e}")
        return

    await status.edit_text("✂️ Splitting into 30-second clips...")
    clips, total_duration = split_video(video_path, clip_duration=30)
    if not clips:
        await status.edit_text("❌ Could not split video.")
        os.remove(video_path)
        return

    total = len(clips)
    await status.edit_text(f"🎬 {total} clips. Sending...")

    for i, clip in enumerate(clips):
        caption = generate_caption(title, desc, uploader, tags, views, likes, i+1, total)
        with open(clip, 'rb') as f:
            await update.message.reply_video(video=f, caption=caption, supports_streaming=True)
        os.remove(clip)

    os.remove(video_path)
    await status.delete()

def main():
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_link))
    print("🤖 Simple Video Clipper Bot is running...")
    app.run_polling()

if __name__ == "__main__":
    main()
