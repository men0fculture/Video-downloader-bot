import os
import subprocess
import logging
import time
import yt_dlp
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

# Logging setup
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)

# --- CONFIGURATION ---
TOKEN = os.environ.get("TELEGRAM_TOKEN", "YOUR_TOKEN_HERE")
INSTAGRAM = "@workaholic_mohit"
DOWNLOAD_DIR = "/tmp"
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

def download_video(url):
    ydl_opts = {
        # 1080p priority logic
        'format': 'bestvideo[height<=1080][ext=mp4]+bestaudio[ext=m4a]/best[height<=1080][ext=mp4]/best',
        'outtmpl': f'{DOWNLOAD_DIR}/%(title)s.%(ext)s',
        'quiet': True,
        'merge_output_format': 'mp4',
        'noplaylist': True,
        # Anti-Bot / No-Cookie Settings
        'nocheckcertificate': True,
        'impersonate': 'safari', 
        'extractor_args': {
            'youtube': {
                'player_client': ['android', 'web'],
                'skip': ['dash', 'hls']
            }
        },
        'user_agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)
        filename = ydl.prepare_filename(info)
        # Ensure merged file extension is correct
        if not os.path.exists(filename):
            filename = os.path.splitext(filename)[0] + ".mp4"
        return filename, info.get('title', 'Video'), info.get('uploader', 'Unknown')

def process_and_split(input_path, clip_duration=30):
    # Get total duration using ffprobe
    cmd = ['ffprobe', '-v', 'error', '-show_entries', 'format=duration', '-of', 'default=noprint_wrappers=1:nokey=1', input_path]
    total_seconds = float(subprocess.check_output(cmd, text=True).strip())
    
    clips = []
    for i, start in enumerate(range(0, int(total_seconds), clip_duration)):
        output_clip = f"{DOWNLOAD_DIR}/clip_{i+1}.mp4"
        
        # Re-encoding to ensure stability and no "white screen"
        # Using original aspect ratio as requested (removed portrait zoom)
        cmd = [
            'ffmpeg', '-ss', str(start), '-t', str(clip_duration),
            '-i', input_path,
            '-c:v', 'libx264', '-preset', 'ultrafast', '-crf', '23',
            '-c:a', 'aac', '-b:a', '128k',
            output_clip, '-y'
        ]
        subprocess.run(cmd, capture_output=True)
        if os.path.exists(output_clip):
            clips.append(output_clip)
    return clips

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"🎬 **Shivay Spares Hub Bot**\nOwner: {INSTAGRAM}\n\nSend a YouTube link to begin.")

async def handle_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    url = update.message.text
    if 'youtube' not in url and 'youtu.be' not in url:
        return

    status = await update.message.reply_text("⏳ Initializing Download...")
    try:
        # Step 1: Download
        video_path, title, uploader = download_video(url)
        
        # Step 2: Split
        await status.edit_text("✂️ Splitting into 30s clips...")
        clips = process_and_split(video_path)
        
        # Step 3: Upload
        for i, clip in enumerate(clips):
            await status.edit_text(f"📤 Uploading Part {i+1}/{len(clips)}...")
            with open(clip, 'rb') as f:
                caption = (
                    f"🎥 **{title}**\n"
                    f"👤 {uploader}\n"
                    f"📌 Part {i+1}/{len(clips)}\n\n"
                    f"✨ Managed by: {INSTAGRAM}"
                )
                await update.message.reply_video(
                    video=f, 
                    caption=caption, 
                    parse_mode='Markdown',
                    supports_streaming=True
                )
            os.remove(clip)
        
        os.remove(video_path)
        await status.delete()
        
    except Exception as e:
        logging.error(f"Error: {e}")
        await update.message.reply_text(f"❌ Error occurred: {str(e)}")

def main():
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_link))
    print("🤖 Bot is running...")
    app.run_polling()

if __name__ == "__main__":
    main()
