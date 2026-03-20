import os
import subprocess
import logging
import random
import json
import requests
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes, CallbackQueryHandler
import yt_dlp

# Logging
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)

TOKEN = os.environ.get("TELEGRAM_TOKEN", "YOUR_TOKEN_HERE")
DOWNLOAD_DIR = "/tmp"
COOKIE_FILE = "/app/cookies.txt"
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

# ---------- Invidious Helper (No Cookies) ----------
def get_invidious_instance():
    """Fetch a working Invidious instance"""
    try:
        r = requests.get("https://api.invidious.io/instances.json?sort_by=type,users", timeout=5)
        instances = r.json()
        for inst in instances:
            url = inst[1]['uri']
            if inst[1]['type'] == 'https' and inst[1]['api']:
                return url.rstrip('/')
    except:
        pass
    return "https://invidious.snopyta.org"

def download_via_invidious(url):
    """Download video using Invidious API"""
    instance = get_invidious_instance()
    video_id = url.split('v=')[-1].split('&')[0] if 'v=' in url else url.split('/')[-1]
    api_url = f"{instance}/api/v1/videos/{video_id}"
    try:
        r = requests.get(api_url, timeout=10)
        data = r.json()
        # Find best 720p mp4 with audio
        formats = data.get('formatStreams', [])
        best_url = None
        for fmt in formats:
            if fmt.get('height') == 720 and fmt.get('type', '').startswith('video/mp4'):
                best_url = fmt['url']
                break
        if not best_url and formats:
            best_url = formats[0]['url']
        if not best_url:
            raise Exception("No video URL found")
        # Download
        video_data = requests.get(best_url, timeout=30)
        filename = f"{DOWNLOAD_DIR}/{video_id}.mp4"
        with open(filename, 'wb') as f:
            f.write(video_data.content)
        title = data.get('title', 'Video')
        description = data.get('description', '')
        uploader = data.get('author', 'Unknown')
        view_count = data.get('viewCount', 0)
        like_count = data.get('likeCount', 0)
        duration = data.get('lengthSeconds', 0)
        return filename, title, description, uploader, [], view_count, like_count, duration
    except Exception as e:
        print(f"Invidious error: {e}")
        return None, None, None, None, None, None, None, None

# ---------- yt-dlp Fallback (No Cookies) ----------
def download_via_ytdlp(url):
    ydl_opts = {
        'format': 'best',
        'outtmpl': f'{DOWNLOAD_DIR}/%(title)s.%(ext)s',
        'quiet': True,
        'writethumbnail': False,
        'embedmetadata': True,
        'postprocessors': [{'key': 'FFmpegVideoConvertor', 'preferedformat': 'mp4'}],
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)
        filename = ydl.prepare_filename(info)
        title = info.get('title', 'Video')
        description = info.get('description', '')
        uploader = info.get('uploader', 'Unknown')
        tags = info.get('tags', [])
        view_count = info.get('view_count', 0)
        like_count = info.get('like_count', 0)
        duration = info.get('duration', 0)
        return filename, title, description, uploader, tags, view_count, like_count, duration

# ---------- Make Clips ----------
def make_clips(input_path, clip_duration=30):
    base = os.path.splitext(input_path)[0]
    ext = os.path.splitext(input_path)[1]
    cmd = ['ffprobe', '-v', 'error', '-show_entries', 'format=duration', '-of', 'default=noprint_wrappers=1:nokey=1', input_path]
    duration = float(subprocess.run(cmd, capture_output=True, text=True).stdout.strip())
    clips = []
    for i, start in enumerate(range(0, int(duration), clip_duration)):
        end = min(start + clip_duration, duration)
        clip_path = f"{DOWNLOAD_DIR}/clip_{i:02d}{ext}"
        cmd = ['ffmpeg', '-i', input_path, '-ss', str(start), '-to', str(end), '-c', 'copy', clip_path, '-y']
        subprocess.run(cmd, capture_output=True)
        clips.append(clip_path)
    return clips

# ---------- Audio & Subtitles ----------
def extract_audio(video_path, audio_path):
    cmd = ['ffmpeg', '-i', video_path, '-q:a', '0', '-map', 'a', audio_path, '-y']
    subprocess.run(cmd, capture_output=True)
    return audio_path

def generate_subtitles(audio_path, srt_path):
    try:
        from vosk import Model, KaldiRecognizer
        model_path = "/tmp/vosk-model-small-hi-0.22"
        if not os.path.exists(model_path):
            return None
        model = Model(model_path)
        rec = KaldiRecognizer(model, 16000)
        with subprocess.Popen(['ffmpeg', '-loglevel', 'quiet', '-i', audio_path,
                               '-ar', '16000', '-ac', '1', '-f', 's16le', '-'],
                              stdout=subprocess.PIPE) as process:
            subtitles = []
            while True:
                data = process.stdout.read(4000)
                if len(data) == 0:
                    break
                if rec.AcceptWaveform(data):
                    result = json.loads(rec.Result())
                    if result.get('text'):
                        subtitles.append(result['text'])
            with open(srt_path, 'w') as f:
                for i, text in enumerate(subtitles, 1):
                    start = (i-1) * 3
                    end = i * 3
                    f.write(f"{i}\n00:00:{start:02d},000 --> 00:00:{end:02d},000\n{text}\n\n")
        return srt_path
    except Exception as e:
        print(f"Subtitle error: {e}")
        return None

# ---------- Video Processing ----------
def analyze_brightness(video_path):
    cmd = ['ffmpeg', '-i', video_path, '-vf', 'signalstats', '-f', 'null', '-']
    result = subprocess.run(cmd, capture_output=True, text=True)
    avg = 128
    for line in result.stderr.split('\n'):
        if 'YAvg' in line:
            try:
                avg = int(line.split('YAvg:')[1].strip())
                break
            except:
                pass
    return avg

def process_clip(input_path, output_path, title):
    brightness = analyze_brightness(input_path)
    if brightness < 100:
        bright, cont = 1.1, 1.05
    elif brightness > 150:
        bright, cont = 0.9, 0.95
    else:
        bright, cont = 1.0, 1.0
    speed = round(random.uniform(0.98, 1.02), 3)
    audio_path = input_path.replace('.mp4', '_audio.wav')
    extract_audio(input_path, audio_path)
    srt_path = input_path.replace('.mp4', '.srt')
    generate_subtitles(audio_path, srt_path)
    filter_str = (
        f"setpts={1/speed}*PTS,"
        f"eq=brightness={bright}:contrast={cont},"
        f"scale=1080:1920:force_original_aspect_ratio=decrease,"
        f"pad=1080:1920:(ow-iw)/2:(oh-ih)/2"
    )
    if srt_path and os.path.exists(srt_path):
        filter_str += f",subtitles={srt_path}:force_style='FontName=Arial,FontSize=20,PrimaryColour=&H00FFFFFF,OutlineColour=&H00000000,MarginV=50'"
    cmd = [
        'ffmpeg', '-i', input_path,
        '-vf', filter_str,
        '-af', f"atempo={speed}",
        '-c:v', 'libx264', '-c:a', 'aac',
        '-preset', 'ultrafast',
        output_path, '-y'
    ]
    subprocess.run(cmd, capture_output=True)
    for f in [audio_path, srt_path]:
        if os.path.exists(f): os.remove(f)
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

# ---------- Telegram Commands ----------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🎬 **No-Cookie Video Processor**\n\n"
        "Send YouTube link – I'll download via Invidious (no cookies).\n"
        "If that fails, I'll use yt-dlp.\n\n"
        "**Commands:**\n"
        "/cookies – Upload cookies (only for fallback if needed)\n"
        "/login – Guide to get cookies\n"
        "/testcookies – Check if cookies work\n"
        "/clearcookies – Remove cookies\n\n"
        "✅ **No cookies required for most videos**"
    )

async def login_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    instructions = (
        "🔑 **How to get cookies (only if needed)**\n\n"
        "1. Install extension: [Get cookies.txt LOCALLY](https://chrome.google.com/webstore/detail/get-cookiestxt-locally/cclelndahbckbenkjhflpdbgdldlbecc)\n"
        "2. Go to [YouTube](https://youtube.com) and log in.\n"
        "3. Click extension icon → **Export as Netscape format**.\n"
        "4. Send file with `/cookies`.\n\n"
        "Your bot will use them only if Invidious fails."
    )
    await update.message.reply_text(instructions, parse_mode='Markdown')

async def cookies_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Send cookies file (Netscape format).")
    context.user_data['awaiting_cookies'] = True

async def testcookies_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if os.path.exists(COOKIE_FILE):
        await update.message.reply_text("✅ Cookies file exists.")
    else:
        await update.message.reply_text("❌ No cookies file.")

async def clearcookies_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if os.path.exists(COOKIE_FILE):
        os.remove(COOKIE_FILE)
        await update.message.reply_text("🗑️ Cookies removed.")
    else:
        await update.message.reply_text("No cookies file found.")

async def handle_cookie_upload(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.user_data.get('awaiting_cookies'):
        return
    if update.message.document:
        file = await update.message.document.get_file()
        await file.download_to_drive(COOKIE_FILE)
        await update.message.reply_text("✅ Cookies saved.")
    elif update.message.text:
        with open(COOKIE_FILE, 'w') as f:
            f.write(update.message.text)
        await update.message.reply_text("✅ Cookies saved.")
    else:
        await update.message.reply_text("Send file or paste content.")
    context.user_data['awaiting_cookies'] = False

async def handle_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    url = update.message.text
    if 'youtube.com' not in url and 'youtu.be' not in url:
        await update.message.reply_text("❌ Send a YouTube link.")
        return

    status = await update.message.reply_text("🔍 Trying Invidious (no cookies)...")

    # Try Invidious first
    video_path, title, desc, uploader, tags, views, likes, duration = download_via_invidious(url)
    if video_path:
        await status.edit_text("✅ Downloaded via Invidious! Creating clips...")
    else:
        # Fallback to yt-dlp without cookies
        await status.edit_text("⚠️ Invidious failed. Trying yt-dlp (no cookies)...")
        try:
            video_path, title, desc, uploader, tags, views, likes, duration = download_via_ytdlp(url)
        except Exception as e:
            await status.edit_text(f"❌ Download failed: {str(e)}")
            return

    # Process video
    await status.edit_text("✂️ Creating clips...")
    clips = make_clips(video_path, clip_duration=30)
    await status.edit_text(f"🎬 {len(clips)} clips. Processing...")
    for i, clip in enumerate(clips):
        final = clip.replace('.mp4', '_final.mp4')
        process_clip(clip, final, title)
        caption = generate_caption(title, desc, uploader, tags, views, likes, i+1, len(clips))
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
    app.add_handler(CommandHandler("login", login_command))
    app.add_handler(CommandHandler("cookies", cookies_command))
    app.add_handler(CommandHandler("testcookies", testcookies_command))
    app.add_handler(CommandHandler("clearcookies", clearcookies_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_link))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_cookie_upload))
    print("🤖 No-cookie bot is running...")
    app.run_polling()

if __name__ == "__main__":
    main()
