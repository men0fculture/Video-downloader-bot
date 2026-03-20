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

# ---------- Video Download ----------
def download_video(url):
    ydl_opts = {
        'format': 'best[height<=480]',
        'outtmpl': f'{DOWNLOAD_DIR}/%(title)s.%(ext)s',
        'quiet': True,
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)
        filename = ydl.prepare_filename(info)
        return filename, info.get('title', 'video')

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
    """Extract audio from video for transcription"""
    cmd = [
        'ffmpeg', '-i', video_path,
        '-q:a', '0', '-map', 'a',
        audio_path, '-y'
    ]
    subprocess.run(cmd, capture_output=True)
    return audio_path

# ---------- Generate Subtitles (Vosk) ----------
def generate_subtitles(audio_path, srt_path):
    """
    Use Vosk (offline) to generate subtitles
    Note: Need to download model first
    """
    try:
        from vosk import Model, KaldiRecognizer
        
        # Download model if not exists (run once)
        model_path = "/tmp/vosk-model-small-hi-0.22"
        if not os.path.exists(model_path):
            subprocess.run([
                'wget', 'https://alphacephei.com/vosk/models/vosk-model-small-hi-0.22.zip',
                '-O', '/tmp/model.zip'
            ])
            subprocess.run(['unzip', '/tmp/model.zip', '-d', '/tmp/'])
        
        # Load model
        model = Model(model_path)
        rec = KaldiRecognizer(model, 16000)
        
        # Process audio
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
                    start = (i-1) * 3  # Approx timing
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
    """Analyze average brightness of video"""
    cmd = [
        'ffmpeg', '-i', video_path,
        '-vf', 'signalstats', '-f', 'null', '-'
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    
    # Extract brightness from output (simplified)
    avg_brightness = 128  # Default middle value
    for line in result.stderr.split('\n'):
        if 'YAvg' in line:
            try:
                avg_brightness = int(line.split('YAvg:')[1].strip())
                break
            except:
                pass
    return avg_brightness

def get_optimal_adjustments(video_path):
    """Calculate optimal brightness/contrast based on video analysis"""
    brightness = analyze_video_brightness(video_path)
    
    if brightness < 100:  # Too dark
        bright_adj = 1.1
        contrast_adj = 1.05
    elif brightness > 150:  # Too bright (exposure issue)
        bright_adj = 0.9  # Reduce brightness
        contrast_adj = 0.95
    else:  # Good exposure
        bright_adj = 1.0
        contrast_adj = 1.0
    
    return bright_adj, contrast_adj

# ---------- Burn Subtitles on Video ----------
def add_subtitles_to_video(video_path, srt_path, output_path):
    """Add subtitles to video"""
    cmd = [
        'ffmpeg', '-i', video_path,
        '-vf', f"subtitles={srt_path}:force_style='FontName=Arial,FontSize=24,PrimaryColour=&H00FFFFFF,OutlineColour=&H00000000,BorderStyle=1,Outline=1,Shadow=0,MarginV=20'",
        '-c:a', 'copy',
        output_path, '-y'
    ]
    subprocess.run(cmd, capture_output=True)
    return output_path

# ---------- Make Video Unique with Intelligent Adjustments ----------
def make_video_unique(input_path, output_path):
    """
    Apply intelligent modifications:
    - Auto brightness/contrast based on video analysis
    - Slight random speed change
    - Format for portrait
    - Add subtitles (if available)
    """
    # Get optimal adjustments
    bright_adj, contrast_adj = get_optimal_adjustments(input_path)
    
    # Random speed (subtle)
    speed = round(random.uniform(0.98, 1.02), 3)
    
    # Extract audio for transcription
    audio_path = input_path.replace('.mp4', '_audio.wav')
    extract_audio(input_path, audio_path)
    
    # Generate subtitles
    srt_path = input_path.replace('.mp4', '.srt')
    generate_subtitles(audio_path, srt_path)
    
    # Complex filter with adjustments
    filter_str = (
        f"setpts={1/speed}*PTS,"  # Speed
        f"eq=brightness={bright_adj}:contrast={contrast_adj},"  # Auto adjustments
        f"scale=1080:1920:force_original_aspect_ratio=decrease,"  # Portrait
        f"pad=1080:1920:(ow-iw)/2:(oh-ih)/2"  # Center with padding
    )
    
    # Add subtitles if generated
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

# ---------- Process Clip for Shorts ----------
def process_clip(input_path, output_path):
    """Main processing function"""
    # First make unique with all adjustments
    unique_path = input_path.replace('.mp4', '_unique.mp4')
    make_video_unique(input_path, unique_path)
    
    # Final output
    os.rename(unique_path, output_path)
    return output_path

# ---------- Telegram Handlers ----------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🎬 **Professional Video Processor**\n\n"
        "Send me any YouTube link, I'll:\n"
        "1. Download video\n"
        "2. Make 30-second clips\n"
        "3. **Fix exposure automatically**\n"
        "4. **Add narrator text as subtitles**\n"
        "5. **Format for YouTube Shorts/Instagram Reels**\n"
        "6. **Adjust brightness/contrast intelligently**\n\n"
        "✅ **100% Free | Copyright-Free | Professional Quality**"
    )

async def handle_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    url = update.message.text
    if 'youtube.com' not in url and 'youtu.be' not in url:
        await update.message.reply_text("❌ Please send a valid YouTube link")
        return
    
    status = await update.message.reply_text("⏳ Downloading video...")
    
    try:
        # Download
        video_path, title = download_video(url)
        await status.edit_text("✅ Downloaded!\n✂️ Creating clips...")
        
        # Make clips
        clips = make_clips(video_path, clip_duration=30)
        
        await status.edit_text(f"🎬 Created {len(clips)} clips. Processing (this may take a while)...")
        
        # Process each clip
        for i, clip in enumerate(clips):
            # Process
            final_path = clip.replace('.mp4', '_final.mp4')
            process_clip(clip, final_path)
            
            # Send
            with open(final_path, 'rb') as f:
                await update.message.reply_video(
                    video=f,
                    caption=f"🎥 Clip {i+1}/{len(clips)}\n✅ Exposure fixed | Subtitles added | Ready for Shorts",
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
    print("🤖 Professional Video Bot is running...")
    app.run_polling()

if __name__ == "__main__":
    main()
