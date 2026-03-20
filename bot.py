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
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

# ---------- Helper: Get available formats ----------
def get_available_formats(url):
    ydl_opts = {
        'listformats': True,
        'quiet': True,
        'cookiefile': '/app/cookies.txt',
    }
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            formats = info.get('formats', [])
            
            # Filter to video formats with height and preferably with audio
            video_formats = []
            for f in formats:
                if f.get('vcodec') != 'none' and f.get('height') is not None:
                    # prefer formats that have audio (acodec not 'none')
                    has_audio = f.get('acodec') != 'none'
                    video_formats.append({
                        'format_id': f['format_id'],
                        'height': f['height'],
                        'ext': f['ext'],
                        'has_audio': has_audio,
                        'fps': f.get('fps', ''),
                        'tbr': f.get('tbr', '')
                    })
            
            # Sort by height descending
            video_formats.sort(key=lambda x: x['height'], reverse=True)
            
            # Remove duplicates: keep only highest quality per height
            unique = {}
            for f in video_formats:
                if f['height'] not in unique:
                    unique[f['height']] = f
            return list(unique.values())
    except Exception as e:
        print(f"Format fetch error: {e}")
        return None

# ---------- Download with selected format ----------
def download_video_with_format(url, format_id):
    ydl_opts = {
        'format': format_id,
        'outtmpl': f'{DOWNLOAD_DIR}/%(title)s.%(ext)s',
        'quiet': True,
        'cookiefile': '/app/cookies.txt',
        'writethumbnail': False,
        'embedmetadata': True,
        'postprocessors': [{
            'key': 'FFmpegVideoConvertor',
            'preferedformat': 'mp4',
        }],
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)
        filename = ydl.prepare_filename(info)
        
        # Extract metadata
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
    # Create hashtags
    hashtags = []
    if tags:
        hashtags = [f"#{tag.replace(' ', '').replace('-', '')}" for tag in tags[:3]]
    else:
        hashtags = ["#viral", "#trending", "#shorts"]
    
    if uploader and uploader != "Unknown":
        hashtags.append(f"#{uploader.replace(' ', '').replace('-', '')}")
    
    hashtag_str = " ".join(hashtags)
    
    # Create caption
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
        "Send me any YouTube link, I'll show available formats.\n"
        "Choose the quality, then I'll download and process.\n\n"
        "✅ **100% Free | Copyright-Free**"
    )

async def handle_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    url = update.message.text
    if 'youtube.com' not in url and 'youtu.be' not in url:
        await update.message.reply_text("❌ Please send a valid YouTube link")
        return
    
    status = await update.message.reply_text("🔍 Fetching available formats...")
    
    # Get available formats
    formats = get_available_formats(url)
    if not formats:
        await status.edit_text("❌ Could not fetch formats. Maybe the video is private or region-locked.")
        return
    
    # Build inline keyboard buttons (limit to top 10)
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
    # Store the original URL in context for later use
    context.user_data['original_url'] = url

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
        # Download using selected format
        video_path, title, description, uploader, tags, views, likes, duration = download_video_with_format(url, format_id)
        await query.edit_message_text("✅ Downloaded!\n✂️ Creating clips...")
        
        # Make clips (30 seconds each)
        clips = make_clips(video_path, clip_duration=30)
        
        await query.edit_message_text(f"🎬 Created {len(clips)} clips. Processing...")
        
        # Process each clip
        for i, clip in enumerate(clips):
            # Process clip
            final_path = clip.replace('.mp4', '_final.mp4')
            process_clip(clip, final_path, title)
            
            # Generate caption for this clip
            caption = generate_caption(
                title, description, uploader, tags, 
                views, likes, i+1, len(clips)
            )
            
            # Send
            with open(final_path, 'rb') as f:
                await query.message.reply_video(
                    video=f,
                    caption=caption,
                    supports_streaming=True
                )
            
            # Cleanup
            os.remove(clip)
            if os.path.exists(final_path):
                os.remove(final_path)
        
        # Cleanup original
        os.remove(video_path)
        await query.edit_message_text("✅ All done! Sent all parts.")
        
    except Exception as e:
        await query.edit_message_text(f"❌ Error: {str(e)}")

def main():
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_link))
    app.add_handler(CallbackQueryHandler(format_callback))
    print("🤖 Format-selectable video bot is running...")
    app.run_polling()

if __name__ == "__main__":
    main()
