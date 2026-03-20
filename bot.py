import os
import subprocess
import logging
import random
import json
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
import yt_dlp

# Logging
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)

TOKEN = os.environ.get("TELEGRAM_TOKEN", "YOUR_TOKEN_HERE")
DOWNLOAD_DIR = "/tmp"
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

# ---------- Video Download with Cookies and Metadata ----------
def download_video(url):
    ydl_opts = {
        'format': 'best[height<=480]',
        'outtmpl': f'{DOWNLOAD_DIR}/%(title)s.%(ext)s',
        'quiet': True,
        'cookiefile': '/app/cookies.txt',  # Cookies file for authentication
        'writethumbnail': False,
        'embedmetadata': True,
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)
        filename = ydl.prepare_filename(info)
        
        # Extract all metadata
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
    """
    Apply all modifications:
    - Exposure fix
    - Speed change
    - Portrait mode
    - Subtitles
    """
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
        # Clean description - remove extra spaces and newlines
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
        "Send me any YouTube link, I'll:\n"
        "1. Download video with cookies\n"
        "2. Make 30-second clips\n"
        "3. Fix exposure automatically\n"
        "4. Add narrator text as subtitles\n"
        "5. Format for YouTube Shorts/Instagram Reels\n"
        "6. Adjust brightness/contrast intelligently\n"
        "7. Send with full caption, description & hashtags\n\n"
        "✅ **100% Free | Copyright-Free | Professional Quality**"
    )

async def handle_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    url = update.message.text
    if 'youtube.com' not in url and 'youtu.be' not in url:
        await update.message.reply_text("❌ Please send a valid YouTube link")
        return
    
    status = await update.message.reply_text("⏳ Downloading video...")
    
    try:
        # Download with all metadata
        video_path, title, description, uploader, tags, views, likes, duration = download_video(url)
        await status.edit_text("✅ Downloaded!\n✂️ Creating clips...")
        
        # Make clips (30 seconds each)
        clips = make_clips(video_path, clip_duration=30)
        
        await status.edit_text(f"🎬 Created {len(clips)} clips. Processing (this may take a while)...")
        
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
                await update.message.reply_video(
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
        await status.delete()
        
    except Exception as e:
        await status.edit_text(f"❌ Error: {str(e)}")

def main():
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_link))
    print("🤖 Professional Video Bot with Cookies & Captions is running...")
    app.run_polling()

if __name__ == "__main__":
    main()
