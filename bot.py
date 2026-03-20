import os
import subprocess
import logging
import random
import json
import time
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
import yt_dlp

# Logging
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)

TOKEN = os.environ.get("TELEGRAM_TOKEN", "YOUR_TOKEN_HERE")
DOWNLOAD_DIR = "/tmp"
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

# ---------- Helper: Check if file is valid video ----------
def is_valid_video(path):
    if not os.path.exists(path):
        return False
    if os.path.getsize(path) < 10000:  # at least 10KB
        return False
    # Check for video stream
    cmd = ['ffprobe', '-v', 'error', '-select_streams', 'v:0', '-show_entries', 'stream=codec_type', '-of', 'default=noprint_wrappers=1:nokey=1', path]
    try:
        out = subprocess.check_output(cmd, stderr=subprocess.STDOUT).decode().strip()
        return out == 'video'
    except:
        return False

# ---------- Download via yt-dlp (no cookies) ----------
def download_video(url):
    ydl_opts = {
        'format': 'best[ext=mp4]/best',  # prefer mp4
        'outtmpl': f'{DOWNLOAD_DIR}/%(title)s.%(ext)s',
        'quiet': True,
        'writethumbnail': False,
        'embedmetadata': True,
        'postprocessors': [{'key': 'FFmpegVideoConvertor', 'preferedformat': 'mp4'}],
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)
        filename = ydl.prepare_filename(info)
        # Fix extension if needed
        if not filename.endswith('.mp4'):
            base = os.path.splitext(filename)[0]
            if os.path.exists(base + '.mp4'):
                filename = base + '.mp4'
        if not is_valid_video(filename):
            # fallback: download best without restrictions
            ydl_opts2 = {
                'format': 'best',
                'outtmpl': f'{DOWNLOAD_DIR}/%(title)s.%(ext)s',
                'quiet': True,
                'postprocessors': [{'key': 'FFmpegVideoConvertor', 'preferedformat': 'mp4'}],
            }
            with yt_dlp.YoutubeDL(ydl_opts2) as ydl2:
                info2 = ydl2.extract_info(url, download=True)
                filename2 = ydl2.prepare_filename(info2)
                if not filename2.endswith('.mp4'):
                    base2 = os.path.splitext(filename2)[0]
                    if os.path.exists(base2 + '.mp4'):
                        filename2 = base2 + '.mp4'
                filename = filename2
        title = info.get('title', 'Video')
        description = info.get('description', '')
        uploader = info.get('uploader', 'Unknown')
        tags = info.get('tags', [])
        view_count = info.get('view_count', 0)
        like_count = info.get('like_count', 0)
        duration = info.get('duration', 0)
        return filename, title, description, uploader, tags, view_count, like_count, duration

# ---------- Clip splitting ----------
def split_video(input_path, clip_duration=30):
    # Get duration
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
        else:
            print(f"Clip {i} invalid, skipping")
    return clips, int(duration)

# ---------- Audio extraction ----------
def extract_audio(video_path, audio_path):
    cmd = ['ffmpeg', '-i', video_path, '-q:a', '0', '-map', 'a', audio_path, '-y']
    subprocess.run(cmd, capture_output=True)
    return audio_path if os.path.exists(audio_path) and os.path.getsize(audio_path) > 0 else None

# ---------- Subtitle generation ----------
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
            if not subtitles:
                return None
            with open(srt_path, 'w') as f:
                for i, text in enumerate(subtitles, 1):
                    start = (i-1) * 3
                    end = i * 3
                    f.write(f"{i}\n00:00:{start:02d},000 --> 00:00:{end:02d},000\n{text}\n\n")
        return srt_path
    except Exception as e:
        print(f"Subtitle error: {e}")
        return None

# ---------- Brightness analysis ----------
def get_video_brightness(video_path):
    cmd = ['ffmpeg', '-i', video_path, '-vf', 'signalstats', '-f', 'null', '-']
    result = subprocess.run(cmd, capture_output=True, text=True)
    for line in result.stderr.split('\n'):
        if 'YAvg' in line:
            try:
                return int(line.split('YAvg:')[1].strip())
            except:
                pass
    return 128

# ---------- Process a single clip ----------
def process_clip(input_path, output_path, title):
    # Analyze brightness
    brightness = get_video_brightness(input_path)
    if brightness < 100:
        bright, cont = 1.1, 1.05
    elif brightness > 150:
        bright, cont = 0.9, 0.95
    else:
        bright, cont = 1.0, 1.0
    speed = round(random.uniform(0.98, 1.02), 3)

    # Generate subtitles (optional)
    audio_path = input_path.replace('.mp4', '_audio.wav')
    audio_file = extract_audio(input_path, audio_path)
    srt_path = None
    if audio_file:
        srt_path = input_path.replace('.mp4', '.srt')
        srt_path = generate_subtitles(audio_file, srt_path)

    # Build filter: speed + brightness/contrast + portrait
    filter_str = (
        f"setpts={1/speed}*PTS,"
        f"eq=brightness={bright}:contrast={cont},"
        f"scale=1080:1920:force_original_aspect_ratio=decrease,"
        f"pad=1080:1920:(ow-iw)/2:(oh-ih)/2"
    )
    if srt_path and os.path.exists(srt_path):
        # escape for ffmpeg filter
        filter_str += f",subtitles='{srt_path}':force_style='FontName=Arial,FontSize=20,PrimaryColour=&H00FFFFFF,OutlineColour=&H00000000,MarginV=50'"

    cmd = [
        'ffmpeg', '-i', input_path,
        '-vf', filter_str,
        '-af', f"atempo={speed}",
        '-c:v', 'libx264', '-c:a', 'aac',
        '-preset', 'ultrafast',
        output_path, '-y'
    ]
    subprocess.run(cmd, capture_output=True)

    # Fallback: if output invalid, retry without subtitles
    if not is_valid_video(output_path):
        print(f"Failed with subtitles, retrying without for {input_path}")
        filter_str2 = (
            f"setpts={1/speed}*PTS,"
            f"eq=brightness={bright}:contrast={cont},"
            f"scale=1080:1920:force_original_aspect_ratio=decrease,"
            f"pad=1080:1920:(ow-iw)/2:(oh-ih)/2"
        )
        cmd2 = [
            'ffmpeg', '-i', input_path,
            '-vf', filter_str2,
            '-af', f"atempo={speed}",
            '-c:v', 'libx264', '-c:a', 'aac',
            '-preset', 'ultrafast',
            output_path, '-y'
        ]
        subprocess.run(cmd2, capture_output=True)

    # Cleanup temp files
    if audio_file and os.path.exists(audio_file):
        os.remove(audio_file)
    if srt_path and os.path.exists(srt_path):
        os.remove(srt_path)
    return output_path

# ---------- Generate caption ----------
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

# ---------- Telegram Handlers ----------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🎬 **Video Processor**\n\n"
        "Send any YouTube link. I'll:\n"
        "• Download video (yt-dlp)\n"
        "• Create 30-second clips\n"
        "• Fix brightness/contrast\n"
        "• Convert to portrait (9:16)\n"
        "• Add subtitles (speech-to-text)\n"
        "• Send clips one by one with ETA\n\n"
        "✅ No cookies needed – works with public videos."
    )

async def handle_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    url = update.message.text
    if 'youtube.com' not in url and 'youtu.be' not in url:
        await update.message.reply_text("❌ Please send a valid YouTube link.")
        return

    status = await update.message.reply_text("📥 Downloading video...")
    try:
        video_path, title, desc, uploader, tags, views, likes, duration = download_video(url)
        if not video_path or not is_valid_video(video_path):
            await status.edit_text("❌ Download failed – video not playable.")
            return
    except Exception as e:
        await status.edit_text(f"❌ Download error: {str(e)}")
        return

    await status.edit_text("✂️ Splitting into 30-second clips...")
    clips, total_duration = split_video(video_path, clip_duration=30)
    if not clips:
        await status.edit_text("❌ Could not split video.")
        os.remove(video_path)
        return

    total = len(clips)
    await status.edit_text(f"🎬 {total} clips created. Processing (approx {total*12}s)...")

    start_time = time.time()
    for i, clip in enumerate(clips):
        elapsed = time.time() - start_time
        if i > 0:
            avg = elapsed / i
            remaining = avg * (total - i)
            eta = f"⏱️ ~{int(remaining)}s left"
        else:
            eta = "⏱️ estimating..."
        await status.edit_text(f"🎬 Processing clip {i+1}/{total}... {eta}")

        final_path = clip.replace('.mp4', '_final.mp4')
        try:
            process_clip(clip, final_path, title)
            if not is_valid_video(final_path):
                # if still invalid, try a simple copy fallback
                fallback_path = clip.replace('.mp4', '_fallback.mp4')
                cmd = ['ffmpeg', '-i', clip, '-c', 'copy', fallback_path, '-y']
                subprocess.run(cmd, capture_output=True)
                if is_valid_video(fallback_path):
                    final_path = fallback_path
                else:
                    raise Exception("Processing failed")
        except Exception as e:
            await update.message.reply_text(f"⚠️ Clip {i+1} failed: {e}, skipping.")
            os.remove(clip)
            continue

        caption = generate_caption(title, desc, uploader, tags, views, likes, i+1, total)
        with open(final_path, 'rb') as f:
            await update.message.reply_video(video=f, caption=caption, supports_streaming=True)
        os.remove(clip)
        if os.path.exists(final_path):
            os.remove(final_path)

    os.remove(video_path)
    await status.delete()

def main():
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_link))
    print("🤖 Bot is running...")
    app.run_polling()

if __name__ == "__main__":
    main()
