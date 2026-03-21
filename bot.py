import os
import subprocess
import logging
import time
import yt_dlp
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)

# --- CONFIGURATION ---
TOKEN = os.environ.get("TELEGRAM_TOKEN", "YOUR_TOKEN_HERE")
[span_0](start_span)INSTAGRAM = "@workaholic_mohit" #[span_0](end_span)
DOWNLOAD_DIR = "/tmp"
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

def download_video(url):
    ydl_opts = {
        # 1080p fallback logic
        'format': 'bestvideo[height<=1080][ext=mp4]+bestaudio[ext=m4a]/best[height<=1080][ext=mp4]/best',
        'outtmpl': f'{DOWNLOAD_DIR}/%(title)s.%(ext)s',
        'quiet': True,
        'merge_output_format': 'mp4',
        'noplaylist': True,
        # ANTI-COOKIE / ANTI-BOT SETTINGS
        'nocheckcertificate': True,
        'ignoreerrors': True,
        # 'web_safari' ya 'android' use karne se 403 Forbidden kam aata hai
        'impersonate': 'safari', 
        'extractor_args': {
            'youtube': {
                'player_client': ['android', 'web'],
                'skip': ['dash', 'hls']
            }
        },
        'user_agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15'
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)
        filename = ydl.prepare_filename(info)
        if not os.path.exists(filename):
            filename = os.path.splitext(filename)[0] + ".mp4"
        return filename, info.get('title', 'Video'), info.get('uploader', 'Unknown')

def process_and_split(input_path, clip_duration=30):
    cmd = ['ffprobe', '-v', '-1', '-show_entries', 'format=duration', '-of', 'default=noprint_wrappers=1:nokey=1', input_path]
    total_seconds = float(subprocess.check_output(cmd, text=True).strip())
    
    clips = []
    for i, start in enumerate(range(0, int(total_seconds), clip_duration)):
        output_clip = f"{DOWNLOAD_DIR}/clip_{i+1}.mp4"
        # Pure Re-encoding (No filters to avoid white screen)
        # Standard 16:9 ya original ratio rakhega, koi stretch nahi
        cmd = [
            'ffmpeg', '-ss', str(start), '-t', str(clip_duration),
            '-i', input_path,
            '-c:v', 'libx264', '-preset', 'ultrafast', '-crf', '24',
            '-c:a', 'aac', '-b:a', '128k',
            output_clip, '-y'
        ]
        subprocess.run(cmd, capture_output=True)
        if os.path.exists(output_clip):
            clips.append(output_clip)
    return clips

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    [span_1](start_span)await update.message.reply_text(f"🚀 Shivay Spares Hub Bot is Active!\nOwner: Mohit ({INSTAGRAM})\n\nSend a YouTube link to start.") #[span_1](end_span)

async def handle_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    url = update.message.text
    if 'youtube' not in url and 'youtu.be' not in url:
        return

    status = await update.message.reply_text("⚡ Downloading (No-Cookie Mode)...")
    try:
        video_path, title, uploader = download_video(url)
        
        await status.edit_text("✂️ Splitting into 30s clips (High Stability)...")
        clips = process_and_split(video_path)
        
        for i, clip in enumerate(clips):
            await status.edit_text(f"📤 Sending Part {i+1}/{len(clips)}...")
            with open(clip, 'rb') as f:
                # Caption with your Branding
                caption = (
                    f"🎥 **{title}**\n"
                    f"👤 Channel: {uploader}\n"
                    f"📌 Part: {i+1}/{len(clips)}\n\n"
                    [span_2](start_span)f"✨ Edited by: {INSTAGRAM}" #[span_2](end_span)
                )
                await update.message.reply_video(video=f, caption=caption, parse_mode='Markdown', supports_streaming=True)
            os.remove(clip)
        
        os.remove(video_path)
        await status.delete()
        
    except Exception as e:
        await update.message.reply_text(f"❌ YouTube Error: {str(e)}\n\nTip: If it says 'Sign in', YouTube is blocking the server IP.")

def main():
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_link))
    print("🤖 Bot is running...")
    app.run_polling()

if __name__ == "__main__":
    main()
