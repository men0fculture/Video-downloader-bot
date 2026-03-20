import os
import subprocess
import logging
import random
import json
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes, CallbackQueryHandler
import yt_dlp

# Logging
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)

TOKEN = os.environ.get("TELEGRAM_TOKEN", "YOUR_TOKEN_HERE")
DOWNLOAD_DIR = "/tmp"
COOKIE_FILE = "/app/cookies.txt"
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

# ---------- Helper: Check if cookies exist and are valid ----------
def cookies_valid():
    if not os.path.exists(COOKIE_FILE):
        return False
    try:
        ydl_opts = {
            'quiet': True,
            'cookiefile': COOKIE_FILE,
            'extract_flat': True,
        }
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.extract_info("https://youtu.be/dQw4w9WgXcQ", download=False)
        return True
    except Exception:
        return False

# ---------- Get available formats ----------
def get_available_formats(url, use_cookies=True):
    ydl_opts = {'quiet': True}
    if use_cookies and os.path.exists(COOKIE_FILE):
        ydl_opts['cookiefile'] = COOKIE_FILE
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
                        'fps': f.get('fps', ''),
                        'tbr': f.get('tbr', '')
                    })
            unique = {}
            for f in video_formats:
                if f['height'] not in unique:
                    unique[f['height']] = f
            return list(unique.values())
    except Exception as e:
        print(f"Format fetch error: {e}")
        return None

# ---------- Download with selected format ----------
def download_video_with_format(url, format_id, use_cookies=True):
    ydl_opts = {
        'format': format_id,
        'outtmpl': f'{DOWNLOAD_DIR}/%(title)s.%(ext)s',
        'quiet': True,
        'writethumbnail': False,
        'embedmetadata': True,
        'postprocessors': [{
            'key': 'FFmpegVideoConvertor',
            'preferedformat': 'mp4',
        }],
    }
    if use_cookies and os.path.exists(COOKIE_FILE):
        ydl_opts['cookiefile'] = COOKIE_FILE

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
        cmd = [
            'ffmpeg', '-i', input_path,
            '-ss', str(start), '-to', str(end),
            '-c', 'copy', clip_path, '-y'
        ]
        subprocess.run(cmd, capture_output=True)
        clips.append(clip_path)
    
    return clips

# ---------- Extract Audio ----------
def extract_audio(video_path, audio_path):
    cmd = [
        'ffmpeg', '-i', video_path,
        '-q:a', '0', '-map', 'a',
        audio_path, '-y'
    ]
    subprocess.run(cmd, capture_output=True)
    return audio_path

# ---------- Generate Subtitles (Vosk) ----------
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
            
            # Write SRT file
            with open(srt_path, 'w') as f:
                for i, text in enumerate(subtitles, 1):
                    start = (i-1) * 3
                    end = i * 3
                    f.write(f"{i}\n")
                    f.write(f"00:00:{start:02d},000 --> 00:00:{end:02d},000\n")
                    f.write(f"{text}\n\n")
        
        return srt_path
    except Exception as e:
        print(f"Subtitle error: {e}")
        return None

# ---------- Intelligent Color Correction ----------
def analyze_video_brightness(video_path):
    cmd = [
        'ffmpeg', '-i', video_path,
        '-vf', 'signalstats', '-f', 'null', '-'
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    
    avg_brightness = 128
    for line in result.stderr.split('\n'):
        if 'YAvg' in line:
            try:
                avg_brightness = int(line.split('YAvg:')[1].strip())
                break
            except:
                pass
    return avg_brightness

def get_optimal_adjustments(video_path):
    brightness = analyze_video_brightness(video_path)
    
    if brightness < 100:
        bright_adj = 1.1
        contrast_adj = 1.05
    elif brightness > 150:
        bright_adj = 0.9
        contrast_adj = 0.95
    else:
        bright_adj = 1.0
        contrast_adj = 1.0
    
    return bright_adj, contrast_adj

# ---------- Process Clip ----------
def process_clip(input_path, output_path, title):
    bright_adj, contrast_adj = get_optimal_adjustments(input_path)
    speed = round(random.uniform(0.98, 1.02), 3)
    
    audio_path = input_path.replace('.mp4', '_audio.wav')
    extract_audio(input_path, audio_path)
    srt_path = input_path.replace('.mp4', '.srt')
    generate_subtitles(audio_path, srt_path)
    
    filter_str = (
        f"setpts={1/speed}*PTS,"
        f"eq=brightness={bright_adj}:contrast={contrast_adj},"
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
        if os.path.exists(f):
            os.remove(f)
    
    return output_path

# ---------- Generate Caption ----------
def generate_caption(title, description, uploader, tags, views, likes, part_num, total_parts):
    hashtags = []
    if tags:
        hashtags = [f"#{tag.replace(' ', '').replace('-', '')}" for tag in tags[:3]]
    else:
        hashtags = ["#viral", "#trending", "#shorts"]
    if uploader and uploader != "Unknown":
        hashtags.append(f"#{uploader.replace(' ', '').replace('-', '')}")
    hashtag_str = " ".join(hashtags)
    
    caption = f"🎥 **{title[:100]}**\n\n"
    if description:
        clean_desc = ' '.join(description.split())[:200]
        caption += f"{clean_desc}...\n\n"
    caption += f"👤 **{uploader}**\n"
    caption += f"👁️ {views:,} views | ❤️ {likes:,} likes\n\n"
    caption += f"{hashtag_str}\n\n"
    caption += f"📌 **Part {part_num}/{total_parts}**"
    return caption

# ---------- Telegram Handlers ----------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🎬 **Professional Video Processor**\n\n"
        "Send me any YouTube link, I'll show available formats.\n\n"
        "**Commands:**\n"
        "/cookies – Upload new cookies (send file)\n"
        "/login – Step-by-step guide to refresh cookies\n"
        "/testcookies – Check if cookies are valid\n"
        "/clearcookies – Remove stored cookies\n\n"
        "✅ **No cookies required for public videos**"
    )

async def login_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    instructions = (
        "🔑 **How to refresh YouTube cookies**\n\n"
        "1. Install this extension: [Get cookies.txt LOCALLY](https://chrome.google.com/webstore/detail/get-cookiestxt-locally/cclelndahbckbenkjhflpdbgdldlbecc)\n"
        "2. Go to [YouTube](https://youtube.com) and log in.\n"
        "3. Click the extension icon → **Export as Netscape format**.\n"
        "4. Save the file and send it to me using `/cookies` command.\n\n"
        "Your cookies will be saved and used for future downloads."
    )
    await update.message.reply_text(instructions, parse_mode='Markdown')

async def cookies_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Send me your cookies file in **Netscape format** as a text file or paste the content.\n\n"
        "You can export cookies using **Cookie-Editor** extension (Netscape format)."
    )
    context.user_data['awaiting_cookies'] = True

async def testcookies_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if cookies_valid():
        await update.message.reply_text("✅ Cookies are valid and working.")
    else:
        await update.message.reply_text("❌ Cookies are invalid or missing. Use /login to refresh.")

async def clearcookies_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if os.path.exists(COOKIE_FILE):
        os.remove(COOKIE_FILE)
        await update.message.reply_text("🗑️ Cookies removed. Now using no cookies.")
    else:
        await update.message.reply_text("No cookies file found.")

async def handle_cookie_upload(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.user_data.get('awaiting_cookies'):
        return
    if update.message.document:
        file = await update.message.document.get_file()
        await file.download_to_drive(COOKIE_FILE)
        await update.message.reply_text("✅ Cookies saved. Use /testcookies to verify.")
    elif update.message.text:
        content = update.message.text
        with open(COOKIE_FILE, 'w') as f:
            f.write(content)
        await update.message.reply_text("✅ Cookies saved. Use /testcookies to verify.")
    else:
        await update.message.reply_text("Please send a text file or paste the cookie content.")
    context.user_data['awaiting_cookies'] = False

async def handle_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    url = update.message.text
    if 'youtube.com' not in url and 'youtu.be' not in url:
        await update.message.reply_text("❌ Please send a valid YouTube link")
        return
    
    status = await update.message.reply_text("🔍 Fetching available formats...")
    
    formats = get_available_formats(url, use_cookies=cookies_valid())
    if not formats:
        formats = get_available_formats(url, use_cookies=False)
    
    if formats:
        keyboard = []
        for f in formats[:10]:
            label = f"{f['height']}p ({f['ext']})"
            if f['has_audio']:
                label += " 🔊"
            else:
                label += " (video only)"
            keyboard.append([InlineKeyboardButton(label, callback_data=f"{url}|{f['format_id']}")])
        reply_markup = InlineKeyboardMarkup(keyboard)
        await status.edit_text("Select a format:", reply_markup=reply_markup)
        context.user_data['original_url'] = url
    else:
        await status.edit_text("⚠️ Could not fetch formats. Downloading with best quality...")
        try:
            video_path, title, description, uploader, tags, views, likes, duration = download_video_with_format(url, 'best', use_cookies=False)
            await status.edit_text("✅ Downloaded!\n✂️ Creating clips...")
            clips = make_clips(video_path, clip_duration=30)
            await status.edit_text(f"🎬 Created {len(clips)} clips. Processing...")
            for i, clip in enumerate(clips):
                final_path = clip.replace('.mp4', '_final.mp4')
                process_clip(clip, final_path, title)
                caption = generate_caption(title, description, uploader, tags, views, likes, i+1, len(clips))
                with open(final_path, 'rb') as f:
                    await update.message.reply_video(video=f, caption=caption, supports_streaming=True)
                os.remove(clip)
                if os.path.exists(final_path):
                    os.remove(final_path)
            os.remove(video_path)
            await status.delete()
        except Exception as e:
            await status.edit_text(f"❌ Download failed: {str(e)}")

async def format_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data.split('|')
    if len(data) != 2:
        await query.edit_message_text("❌ Invalid format selection.")
        return
    url, format_id = data[0], data[1]
    
    await query.edit_message_text(f"⏳ Downloading video with format {format_id}...")
    try:
        use_cookies = cookies_valid()
        video_path, title, description, uploader, tags, views, likes, duration = download_video_with_format(url, format_id, use_cookies=use_cookies)
        await query.edit_message_text("✅ Downloaded!\n✂️ Creating clips...")
        clips = make_clips(video_path, clip_duration=30)
        await query.edit_message_text(f"🎬 Created {len(clips)} clips. Processing...")
        for i, clip in enumerate(clips):
            final_path = clip.replace('.mp4', '_final.mp4')
            process_clip(clip, final_path, title)
            caption = generate_caption(title, description, uploader, tags, views, likes, i+1, len(clips))
            with open(final_path, 'rb') as f:
                await query.message.reply_video(video=f, caption=caption, supports_streaming=True)
            os.remove(clip)
            if os.path.exists(final_path):
                os.remove(final_path)
        os.remove(video_path)
        await query.edit_message_text("✅ All done! Sent all parts.")
    except Exception as e:
        if "Sign in to confirm" in str(e) or "HTTP Error 400" in str(e):
            await query.edit_message_text("⚠️ Cookies may be expired. Retrying without cookies...")
            try:
                video_path, title, description, uploader, tags, views, likes, duration = download_video_with_format(url, format_id, use_cookies=False)
                await query.edit_message_text("✅ Downloaded!\n✂️ Creating clips...")
                clips = make_clips(video_path, clip_duration=30)
                await query.edit_message_text(f"🎬 Created {len(clips)} clips. Processing...")
                for i, clip in enumerate(clips):
                    final_path = clip.replace('.mp4', '_final.mp4')
                    process_clip(clip, final_path, title)
                    caption = generate_caption(title, description, uploader, tags, views, likes, i+1, len(clips))
                    with open(final_path, 'rb') as f:
                        await query.message.reply_video(video=f, caption=caption, supports_streaming=True)
                    os.remove(clip)
                    if os.path.exists(final_path):
                        os.remove(final_path)
                os.remove(video_path)
                await query.edit_message_text("✅ All done! Sent all parts.")
            except Exception as e2:
                await query.edit_message_text(f"❌ Error: {str(e2)}")
        else:
            await query.edit_message_text(f"❌ Error: {str(e)}")

def main():
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("login", login_command))
    app.add_handler(CommandHandler("cookies", cookies_command))
    app.add_handler(CommandHandler("testcookies", testcookies_command))
    app.add_handler(CommandHandler("clearcookies", clearcookies_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_link))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_cookie_upload))
    app.add_handler(CallbackQueryHandler(format_callback))
    print("🤖 Enhanced video bot with /login guide is running...")
    app.run_polling()

if __name__ == "__main__":
    main()    ydl_opts = {
        'quiet': True,
    }
    if use_cookies and os.path.exists(COOKIE_FILE):
        ydl_opts['cookiefile'] = COOKIE_FILE
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
                        'fps': f.get('fps', ''),
                        'tbr': f.get('tbr', '')
                    })
            # Remove duplicates per height
            unique = {}
            for f in video_formats:
                if f['height'] not in unique:
                    unique[f['height']] = f
            return list(unique.values())
    except Exception as e:
        print(f"Format fetch error (cookies={use_cookies}): {e}")
        return None

# ---------- Download with selected format (optionally with cookies) ----------
def download_video_with_format(url, format_id, use_cookies=True):
    ydl_opts = {
        'format': format_id,
        'outtmpl': f'{DOWNLOAD_DIR}/%(title)s.%(ext)s',
        'quiet': True,
        'writethumbnail': False,
        'embedmetadata': True,
        'postprocessors': [{
            'key': 'FFmpegVideoConvertor',
            'preferedformat': 'mp4',
        }],
    }
    if use_cookies and os.path.exists(COOKIE_FILE):
        ydl_opts['cookiefile'] = COOKIE_FILE

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
        cmd = [
            'ffmpeg', '-i', input_path,
            '-ss', str(start), '-to', str(end),
            '-c', 'copy', clip_path, '-y'
        ]
        subprocess.run(cmd, capture_output=True)
        clips.append(clip_path)
    
    return clips

# ---------- Extract Audio for Transcription ----------
def extract_audio(video_path, audio_path):
    cmd = [
        'ffmpeg', '-i', video_path,
        '-q:a', '0', '-map', 'a',
        audio_path, '-y'
    ]
    subprocess.run(cmd, capture_output=True)
    return audio_path

# ---------- Generate Subtitles (Vosk) ----------
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
            
            # Write SRT file
            with open(srt_path, 'w') as f:
                for i, text in enumerate(subtitles, 1):
                    start = (i-1) * 3
                    end = i * 3
                    f.write(f"{i}\n")
                    f.write(f"00:00:{start:02d},000 --> 00:00:{end:02d},000\n")
                    f.write(f"{text}\n\n")
        
        return srt_path
    except Exception as e:
        print(f"Subtitle error: {e}")
        return None

# ---------- Intelligent Color Correction ----------
def analyze_video_brightness(video_path):
    cmd = [
        'ffmpeg', '-i', video_path,
        '-vf', 'signalstats', '-f', 'null', '-'
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    
    avg_brightness = 128
    for line in result.stderr.split('\n'):
        if 'YAvg' in line:
            try:
                avg_brightness = int(line.split('YAvg:')[1].strip())
                break
            except:
                pass
    return avg_brightness

def get_optimal_adjustments(video_path):
    brightness = analyze_video_brightness(video_path)
    
    if brightness < 100:
        bright_adj = 1.1
        contrast_adj = 1.05
    elif brightness > 150:
        bright_adj = 0.9
        contrast_adj = 0.95
    else:
        bright_adj = 1.0
        contrast_adj = 1.0
    
    return bright_adj, contrast_adj

# ---------- Process Clip with All Features ----------
def process_clip(input_path, output_path, title):
    # Get optimal adjustments
    bright_adj, contrast_adj = get_optimal_adjustments(input_path)
    
    # Random speed
    speed = round(random.uniform(0.98, 1.02), 3)
    
    # Extract audio for transcription
    audio_path = input_path.replace('.mp4', '_audio.wav')
    extract_audio(input_path, audio_path)
    
    # Generate subtitles
    srt_path = input_path.replace('.mp4', '.srt')
    generate_subtitles(audio_path, srt_path)
    
    # Build filter
    filter_str = (
        f"setpts={1/speed}*PTS,"
        f"eq=brightness={bright_adj}:contrast={contrast_adj},"
        f"scale=1080:1920:force_original_aspect_ratio=decrease,"
        f"pad=1080:1920:(ow-iw)/2:(oh-ih)/2"
    )
    
    # Add subtitles if available
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
    
    # Cleanup
    for f in [audio_path, srt_path]:
        if os.path.exists(f):
            os.remove(f)
    
    return output_path

# ---------- Generate Caption with Metadata ----------
def generate_caption(title, description, uploader, tags, views, likes, part_num, total_parts):
    hashtags = []
    if tags:
        hashtags = [f"#{tag.replace(' ', '').replace('-', '')}" for tag in tags[:3]]
    else:
        hashtags = ["#viral", "#trending", "#shorts"]
    
    if uploader and uploader != "Unknown":
        hashtags.append(f"#{uploader.replace(' ', '').replace('-', '')}")
    
    hashtag_str = " ".join(hashtags)
    
    caption = f"🎥 **{title[:100]}**\n\n"
    
    if description:
        clean_desc = ' '.join(description.split())[:200]
        caption += f"{clean_desc}...\n\n"
    
    caption += f"👤 **{uploader}**\n"
    caption += f"👁️ {views:,} views | ❤️ {likes:,} likes\n\n"
    caption += f"{hashtag_str}\n\n"
    caption += f"📌 **Part {part_num}/{total_parts}**"
    
    return caption

# ---------- Telegram Command Handlers ----------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🎬 **Professional Video Processor**\n\n"
        "Send me any YouTube link, I'll show available formats.\n"
        "If format fetch fails, I'll try without cookies.\n\n"
        "**Commands:**\n"
        "/cookies – Upload new cookies (send file or paste content)\n"
        "/testcookies – Check if cookies are valid\n"
        "/clearcookies – Remove stored cookies\n\n"
        "✅ **No cookies required for public videos**"
    )

async def cookies_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Send me your cookies file in **Netscape format** as a text file or paste the content.\n\n"
        "You can export cookies using **Cookie-Editor** extension (Netscape format)."
    )
    context.user_data['awaiting_cookies'] = True

async def testcookies_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if cookies_valid():
        await update.message.reply_text("✅ Cookies are valid and working.")
    else:
        await update.message.reply_text("❌ Cookies are invalid or missing. Use /cookies to upload new ones.")

async def clearcookies_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if os.path.exists(COOKIE_FILE):
        os.remove(COOKIE_FILE)
        await update.message.reply_text("🗑️ Cookies removed. Now using no cookies.")
    else:
        await update.message.reply_text("No cookies file found.")

async def handle_cookie_upload(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.user_data.get('awaiting_cookies'):
        return
    # User might send a document or text
    if update.message.document:
        file = await update.message.document.get_file()
        await file.download_to_drive(COOKIE_FILE)
        await update.message.reply_text("✅ Cookies saved. Use /testcookies to verify.")
    elif update.message.text:
        content = update.message.text
        with open(COOKIE_FILE, 'w') as f:
            f.write(content)
        await update.message.reply_text("✅ Cookies saved. Use /testcookies to verify.")
    else:
        await update.message.reply_text("Please send a text file or paste the cookie content.")
    context.user_data['awaiting_cookies'] = False

# ---------- Video Download Handler ----------
async def handle_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    url = update.message.text
    if 'youtube.com' not in url and 'youtu.be' not in url:
        await update.message.reply_text("❌ Please send a valid YouTube link")
        return
    
    status = await update.message.reply_text("🔍 Fetching available formats...")
    
    # Try with cookies if they exist
    formats = get_available_formats(url, use_cookies=cookies_valid())
    if not formats:
        # Try without cookies
        formats = get_available_formats(url, use_cookies=False)
    
    if formats:
        # Build inline keyboard
        keyboard = []
        for f in formats[:10]:
            label = f"{f['height']}p ({f['ext']})"
            if f['has_audio']:
                label += " 🔊"
            else:
                label += " (video only)"
            keyboard.append([InlineKeyboardButton(label, callback_data=f"{url}|{f['format_id']}")])
        reply_markup = InlineKeyboardMarkup(keyboard)
        await status.edit_text("Select a format:", reply_markup=reply_markup)
        context.user_data['original_url'] = url
    else:
        # Fallback: download with best quality (no cookies)
        await status.edit_text("⚠️ Could not fetch formats. Downloading with best quality...")
        try:
            video_path, title, description, uploader, tags, views, likes, duration = download_video_with_format(url, 'best', use_cookies=False)
            await status.edit_text("✅ Downloaded!\n✂️ Creating clips...")
            
            clips = make_clips(video_path, clip_duration=30)
            await status.edit_text(f"🎬 Created {len(clips)} clips. Processing...")
            
            for i, clip in enumerate(clips):
                final_path = clip.replace('.mp4', '_final.mp4')
                process_clip(clip, final_path, title)
                caption = generate_caption(title, description, uploader, tags, views, likes, i+1, len(clips))
                with open(final_path, 'rb') as f:
                    await update.message.reply_video(video=f, caption=caption, supports_streaming=True)
                os.remove(clip)
                if os.path.exists(final_path):
                    os.remove(final_path)
            
            os.remove(video_path)
            await status.delete()
        except Exception as e:
            await status.edit_text(f"❌ Download failed: {str(e)}")

async def format_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data.split('|')
    if len(data) != 2:
        await query.edit_message_text("❌ Invalid format selection.")
        return
    url, format_id = data[0], data[1]
    
    await query.edit_message_text(f"⏳ Downloading video with format {format_id}...")
    
    try:
        # Try with cookies first, if valid
        use_cookies = cookies_valid()
        video_path, title, description, uploader, tags, views, likes, duration = download_video_with_format(url, format_id, use_cookies=use_cookies)
        await query.edit_message_text("✅ Downloaded!\n✂️ Creating clips...")
        
        clips = make_clips(video_path, clip_duration=30)
        await query.edit_message_text(f"🎬 Created {len(clips)} clips. Processing...")
        
        for i, clip in enumerate(clips):
            final_path = clip.replace('.mp4', '_final.mp4')
            process_clip(clip, final_path, title)
            caption = generate_caption(title, description, uploader, tags, views, likes, i+1, len(clips))
            with open(final_path, 'rb') as f:
                await query.message.reply_video(video=f, caption=caption, supports_streaming=True)
            os.remove(clip)
            if os.path.exists(final_path):
                os.remove(final_path)
        
        os.remove(video_path)
        await query.edit_message_text("✅ All done! Sent all parts.")
        
    except Exception as e:
        # If download failed with cookies, retry without cookies
        if "Sign in to confirm" in str(e) or "HTTP Error 400" in str(e):
            await query.edit_message_text("⚠️ Cookies may be expired. Retrying without cookies...")
            try:
                video_path, title, description, uploader, tags, views, likes, duration = download_video_with_format(url, format_id, use_cookies=False)
                # ... same processing ...
            except Exception as e2:
                await query.edit_message_text(f"❌ Error: {str(e2)}")
        else:
            await query.edit_message_text(f"❌ Error: {str(e)}")

def main():
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("cookies", cookies_command))
    app.add_handler(CommandHandler("testcookies", testcookies_command))
    app.add_handler(CommandHandler("clearcookies", clearcookies_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_link))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_cookie_upload))
    app.add_handler(CallbackQueryHandler(format_callback))
    print("🤖 Enhanced video bot (cookie-less with refresh) is running...")
    app.run_polling()

if __name__ == "__main__":
    main()
