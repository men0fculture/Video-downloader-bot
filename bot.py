import os
import subprocess
import logging
import random
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
            if os.path.exists(base+'.mp4'):
                filename = base+'.mp4'
        title = info.get('title', 'Video')
        desc = info.get('description', '')
        uploader = info.get('uploader', 'Unknown')
        tags = info.get('tags', [])
        views = info.get('view_count', 0)
        likes = info.get('like_count', 0)
        duration = info.get('duration', 0)
        return filename, title, desc, uploader, tags, views, likes, duration

def split_video(input_path, clip_duration=30):
    cmd = ['ffprobe', '-v', 'error', '-show_entries', 'format=duration', '-of', 'default=noprint_wrappers=1:nokey=1', input_path]
    duration = float(subprocess.check_output(cmd, text=True).strip())
    clips = []
    for i, start in enumerate(range(0, int(duration), clip_duration)):
        end = min(start + clip_duration, duration)
        clip_path = f"{DOWNLOAD_DIR}/clip_{i:02d}.mp4"
        cmd = ['ffmpeg', '-i', input_path, '-ss', str(start), '-to', str(end), '-c', 'copy', clip_path, '-y']
        subprocess.run(cmd, capture_output=True)
        if is_valid_video(clip_path):
            clips.append(clip_path)
    return clips, int(duration)

def get_brightness(video_path):
    cmd = ['ffmpeg', '-i', video_path, '-vf', 'signalstats', '-f', 'null', '-']
    result = subprocess.run(cmd, capture_output=True, text=True)
    for line in result.stderr.split('\n'):
        if 'YAvg' in line:
            try:
                return int(line.split('YAvg:')[1].strip())
            except:
                pass
    return 128

def process_clip(input_path, output_path):
    brightness = get_brightness(input_path)
    if brightness < 100:
        bright, cont = 1.1, 1.05
    elif brightness > 150:
        bright, cont = 0.9, 0.95
    else:
        bright, cont = 1.0, 1.0
    speed = round(random.uniform(0.98, 1.02), 3)
    filter_str = (
        f"setpts={1/speed}*PTS,"
        f"eq=brightness={bright}:contrast={cont},"
        f"scale=1080:1920:force_original_aspect_ratio=decrease,"
        f"pad=1080:1920:(ow-iw)/2:(oh-ih)/2"
    )
    cmd = [
        'ffmpeg', '-i', input_path,
        '-vf', filter_str,
        '-af', f"atempo={speed}",
        '-c:v', 'libx264', '-c:a', 'aac',
        '-preset', 'ultrafast',
        output_path, '-y'
    ]
    subprocess.run(cmd, capture_output=True)
    return output_path

def generate_caption(title, desc, uploader, tags, views, likes, part, total):
    hashtags = [f"#{tag.replace(' ', '').replace('-', '')}" for tag in tags[:3]] if tags else ["#viral", "#trending", "#shorts"]
    if uploader and uploader != "Unknown":
        hashtags.append(f"#{uploader.replace(' ', '').replace('-', '')}")
    caption = f"🎥 **{title[:100]}**\n\n"
    if desc:
        caption += f"{' '.join(desc.split())[:200]}...\n\n"
    caption += f"👤 **{uploader}**\n👁️ {views:,} views | ❤️ {likes:,} likes\n\n{' '.join(hashtags)}\n\n📌 **Part {part}/{total}**"
    return caption

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🎬 Bot is alive! Send a YouTube link.")

async def handle_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    url = update.message.text
    if 'youtube.com' not in url and 'youtu.be' not in url:
        await update.message.reply_text("❌ Send a YouTube link.")
        return
    status = await update.message.reply_text("📥 Downloading...")
    try:
        video_path, title, desc, uploader, tags, views, likes, duration = download_video(url)
        if not is_valid_video(video_path):
            await status.edit_text("❌ Download failed.")
            return
    except Exception as e:
        await status.edit_text(f"❌ Error: {e}")
        return
    await status.edit_text("✂️ Splitting...")
    clips, total_dur = split_video(video_path, 30)
    if not clips:
        await status.edit_text("❌ Splitting failed.")
        os.remove(video_path)
        return
    total = len(clips)
    await status.edit_text(f"🎬 {total} clips. Processing...")
    start_time = time.time()
    for i, clip in enumerate(clips):
        if i > 0:
            avg = (time.time()-start_time)/i
            eta = f" ~{int(avg*(total-i))}s left"
        else:
            eta = ""
        await status.edit_text(f"🎬 Clip {i+1}/{total}{eta}")
        final = clip.replace('.mp4', '_final.mp4')
        process_clip(clip, final)
        caption = generate_caption(title, desc, uploader, tags, views, likes, i+1, total)
        with open(final, 'rb') as f:
            await update.message.reply_video(video=f, caption=caption, supports_streaming=True)
        os.remove(clip)
        if os.path.exists(final):
            os.remove(final)
    os.remove(video_path)
    await status.delete()

def main():
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_link))
    print("🤖 Streaming video bot is running...")
    app.run_polling()

if __name__ == "__main__":
    main()
