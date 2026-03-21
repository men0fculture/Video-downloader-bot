import os
import subprocess
import logging
import time
import re
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes, CallbackQueryHandler
import yt_dlp
import google.generativeai as genai

logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)

TOKEN = os.environ.get("TELEGRAM_TOKEN", "YOUR_TOKEN_HERE")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
DOWNLOAD_DIR = "/tmp"
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

# Configure Gemini if key exists
if GEMINI_API_KEY:
    genai.configure(api_key=GEMINI_API_KEY)
    model = genai.GenerativeModel('gemini-1.5-flash')
else:
    model = None
    logging.warning("GEMINI_API_KEY not set. AI summaries disabled.")

# ---------- Helper functions ----------
def is_valid_video(path):
    if not os.path.exists(path) or os.path.getsize(path) < 10000:
        return False
    cmd = ['ffprobe', '-v', 'error', '-select_streams', 'v:0', '-show_entries', 'stream=codec_type', '-of', 'default=noprint_wrappers=1:nokey=1', path]
    try:
        out = subprocess.check_output(cmd, stderr=subprocess.STDOUT).decode().strip()
        return out == 'video'
    except:
        return False

def get_available_formats(url):
    """Fetch available video formats (height + ext). Returns list sorted by height."""
    ydl_opts = {'quiet': True}
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            formats = info.get('formats', [])
            video_formats = []
            for f in formats:
                if f.get('vcodec') != 'none' and f.get('height') is not None:
                    has_audio = f.get('acodec') != 'none'
                    video_formats.append({
                        'format_id': f['format_id'],
                        'height': f['height'],
                        'ext': f['ext'],
                        'has_audio': has_audio,
                    })
            # Remove duplicates per height (keep highest quality per height)
            unique = {}
            for f in video_formats:
                height = f['height']
                if height not in unique:
                    unique[height] = f
            return sorted(unique.values(), key=lambda x: x['height'], reverse=True)
    except Exception as e:
        logging.error(f"Format fetch error: {e}")
        return None

def download_video_with_format(url, format_id):
    """Download video using specified format ID."""
    ydl_opts = {
        'format': format_id,
        'outtmpl': f'{DOWNLOAD_DIR}/%(title)s.%(ext)s',
        'quiet': True,
        'postprocessors': [{'key': 'FFmpegVideoConvertor', 'preferedformat': 'mp4'}],
        'writesubtitles': True,
        'writeautomaticsub': True,
        'subtitlesformat': 'srt',
        'subtitleslangs': ['en', 'hi'],
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)
        filename = ydl.prepare_filename(info)
        if not filename.endswith('.mp4'):
            base = os.path.splitext(filename)[0]
            if os.path.exists(base + '.mp4'):
                filename = base + '.mp4'
        # subtitle file
        subtitle_file = None
        for ext in ['.en.srt', '.hi.srt', '.srt']:
            possible = os.path.splitext(filename)[0] + ext
            if os.path.exists(possible):
                subtitle_file = possible
                break
        title = info.get('title', 'Video')
        description = info.get('description', '')
        uploader = info.get('uploader', 'Unknown')
        tags = info.get('tags', [])
        views = info.get('view_count', 0)
        likes = info.get('like_count', 0)
        duration = info.get('duration', 0)
        return filename, title, description, uploader, tags, views, likes, duration, subtitle_file

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
            clips.append((clip_path, start, end))
    return clips, int(duration)

def extract_subtitles_for_segment(srt_file, start_sec, end_sec):
    if not srt_file or not os.path.exists(srt_file):
        return ""
    with open(srt_file, 'r', encoding='utf-8') as f:
        content = f.read()
    pattern = re.compile(r'(\d+)\n(\d{2}:\d{2}:\d{2},\d{3}) --> (\d{2}:\d{2}:\d{2},\d{3})\n(.*?)(?=\n\n|\Z)', re.DOTALL)
    texts = []
    def to_seconds(t):
        h, m, s = t.split(':')
        s, ms = s.split(',')
        return int(h)*3600 + int(m)*60 + int(s) + int(ms)/1000
    for match in pattern.finditer(content):
        start_str = match.group(2)
        end_str = match.group(3)
        text = match.group(4).replace('\n', ' ')
        s_start = to_seconds(start_str)
        s_end = to_seconds(end_str)
        if s_end >= start_sec and s_start <= end_sec:
            texts.append(text)
    return ' '.join(texts)

def get_ai_summary(text):
    if not model or not text:
        return None
    try:
        prompt = f"Summarize the following text in 1-2 short sentences:\n\n{text}"
        response = model.generate_content(prompt)
        return response.text.strip()
    except Exception as e:
        logging.error(f"Gemini error: {e}")
        return None

def generate_caption(title, description, uploader, tags, views, likes, part, total, summary=None):
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
    caption += f"👤 **{uploader}**\n👁️ {views:,} views | ❤️ {likes:,} likes\n\n"
    if summary:
        caption += f"✨ **Summary:** {summary}\n\n"
    caption += f"{' '.join(hashtags)}\n\n📌 **Part {part}/{total}**"
    return caption

# ---------- Telegram Handlers ----------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 **Mohit**\n\n"
        "📸 Instagram: [@workaholic_mohit](https://instagram.com/workaholic_mohit)\n\n"
        "🎬 **Resolution Selector + AI Summary Bot**\n\n"
        "Send any YouTube link. I'll show available resolutions.\n"
        "Choose one, then I'll download, split into 30s clips, and add AI summary per clip (if Gemini key is set).\n\n"
        "✅ Works with or without Gemini – summary optional."
    )

async def handle_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    url = update.message.text
    if 'youtube.com' not in url and 'youtu.be' not in url:
        await update.message.reply_text("❌ Send a valid YouTube link.")
        return

    status = await update.message.reply_text("🔍 Fetching available resolutions...")
    formats = get_available_formats(url)
    if not formats:
        await status.edit_text("❌ Could not fetch resolutions. The video may be private or region-restricted.")
        return

    keyboard = []
    for fmt in formats[:10]:  # limit to 10
        label = f"{fmt['height']}p ({fmt['ext']})"
        if fmt['has_audio']:
            label += " 🔊"
        else:
            label += " (video only)"
        keyboard.append([InlineKeyboardButton(label, callback_data=f"{url}|{fmt['format_id']}")])
    reply_markup = InlineKeyboardMarkup(keyboard)
    await status.edit_text("Select a resolution:", reply_markup=reply_markup)
    context.user_data['original_url'] = url

async def format_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data.split('|')
    if len(data) != 2:
        await query.edit_message_text("❌ Invalid selection.")
        return
    url, format_id = data[0], data[1]

    await query.edit_message_text(f"📥 Downloading video (format {format_id})...")
    try:
        video_path, title, desc, uploader, tags, views, likes, duration, subtitle_file = download_video_with_format(url, format_id)
        if not is_valid_video(video_path):
            await query.edit_message_text("❌ Download failed (invalid video).")
            return
    except Exception as e:
        await query.edit_message_text(f"❌ Download error: {e}")
        return

    await query.edit_message_text("✂️ Splitting into 30-second clips...")
    clips_with_times, total_duration = split_video(video_path, clip_duration=30)
    if not clips_with_times:
        await query.edit_message_text("❌ Could not split video.")
        os.remove(video_path)
        return

    total = len(clips_with_times)
    await query.edit_message_text(f"🎬 {total} clips. Generating summaries...")

    for i, (clip_path, start, end) in enumerate(clips_with_times):
        # Extract subtitle text for this segment
        segment_text = extract_subtitles_for_segment(subtitle_file, start, end)
        summary = get_ai_summary(segment_text) if model else None

        caption = generate_caption(title, desc, uploader, tags, views, likes, i+1, total, summary)

        with open(clip_path, 'rb') as f:
            await query.message.reply_video(video=f, caption=caption, supports_streaming=True)

        os.remove(clip_path)

    # Cleanup
    if subtitle_file and os.path.exists(subtitle_file):
        os.remove(subtitle_file)
    os.remove(video_path)
    await query.edit_message_text("✅ All done! Sent all clips.")

def main():
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_link))
    app.add_handler(CallbackQueryHandler(format_callback))
    print("🤖 Resolution selector bot with AI summary is running...")
    app.run_polling()

if __name__ == "__main__":
    main()
