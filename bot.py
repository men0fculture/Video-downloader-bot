import os
import asyncio
import subprocess
import logging
import glob
import random
import json
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
import yt_dlp

# Logging
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)

TOKEN = os.environ.get("TELEGRAM_TOKEN", "YOUR_TOKEN_HERE")
DOWNLOAD_DIR = "/tmp/downloads"
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

# ---------- Helper Functions ----------
def download_video(url):
    """Download video using yt-dlp, return filename and title."""
    ydl_opts = {
        'format': 'best[height<=720]',
        'outtmpl': f'{DOWNLOAD_DIR}/%(title)s.%(ext)s',
        'quiet': True,
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)
        filename = ydl.prepare_filename(info)
        return filename, info.get('title', 'video')

def detect_silence(input_path, threshold=-30, min_duration=1.0):
    """
    Detect silent parts in audio using FFmpeg's silencedetect filter.
    Returns list of [start, end] times for silences.
    """
    cmd = [
        'ffmpeg', '-i', input_path,
        '-af', f'silencedetect=noise={threshold}dB:d={min_duration}',
        '-f', 'null', '-'
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    
    silences = []
    lines = result.stderr.split('\n')
    start = None
    for line in lines:
        if 'silence_start' in line:
            start = float(line.split('silence_start: ')[1])
        elif 'silence_end' in line and start is not None:
            end = float(line.split('silence_end: ')[1].split(' ')[0])
            silences.append([start, end])
            start = None
    return silences

def detect_scene_changes(input_path, threshold=0.3):
    """
    Detect scene changes using FFmpeg's select filter.
    Returns list of timestamps where scenes change.
    """
    cmd = [
        'ffmpeg', '-i', input_path,
        '-vf', f"select='gt(scene\,{threshold})',metadata=print:file=-",
        '-f', 'null', '-'
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    
    scenes = []
    lines = result.stderr.split('\n')
    for line in lines:
        if 'pts_time:' in line:
            time = float(line.split('pts_time:')[1].strip())
            scenes.append(time)
    return scenes

def get_interesting_segments(input_path, min_segment_duration=10, max_segment_duration=30):
    """
    Combine silence and scene detection to find interesting segments.
    Returns list of [start, end] times for segments to keep.
    """
    # Get video duration
    cmd = ['ffprobe', '-v', 'error', '-show_entries', 'format=duration', '-of', 'default=noprint_wrappers=1:nokey=1', input_path]
    duration = float(subprocess.run(cmd, capture_output=True, text=True).stdout.strip())
    
    # Detect silences (parts to remove)
    silences = detect_silence(input_path)
    
    # Detect scene changes (natural cut points)
    scenes = detect_scene_changes(input_path)
    
    # Build keep segments (inverse of silences)
    keep_segments = []
    current_start = 0
    
    for silence_start, silence_end in silences:
        if silence_start - current_start >= min_segment_duration:
            # Find nearest scene change as end point
            end_time = silence_start
            for scene in scenes:
                if current_start < scene < silence_start:
                    end_time = scene
            keep_segments.append([current_start, end_time])
        current_start = silence_end
    
    # Last segment
    if duration - current_start >= min_segment_duration:
        keep_segments.append([current_start, duration])
    
    # Limit segment length
    final_segments = []
    for start, end in keep_segments:
        while end - start > max_segment_duration:
            final_segments.append([start, start + max_segment_duration])
            start += max_segment_duration
        if end - start >= min_segment_duration:
            final_segments.append([start, end])
    
    return final_segments

def extract_segment(input_path, output_path, start_time, end_time):
    """Extract a segment from video using FFmpeg."""
    cmd = [
        'ffmpeg', '-i', input_path,
        '-ss', str(start_time), '-to', str(end_time),
        '-c', 'copy', output_path, '-y'
    ]
    subprocess.run(cmd, capture_output=True)

def make_video_unique(input_path, output_path, text="Sample Text"):
    """
    Apply multiple transformations to make video non-copyrightable:
    - Slight speed change
    - Brightness/contrast adjustment
    - Add teleprompter text (scrolling)
    - Optional: Add background music
    """
    speed = random.uniform(0.97, 1.03)
    brightness = random.uniform(0.98, 1.02)
    contrast = random.uniform(0.98, 1.02)
    
    filter_str = (
        f"setpts={1/speed}*PTS,"
        f"eq=brightness={brightness}:contrast={contrast},"
        f"drawtext=text='{text}':fontcolor=white:fontsize=24:box=1:boxcolor=black@0.5:"
        f"boxborderw=5:x='w-text_w-mod(t*50\,w+text_w)':y=h-text_h-20"
    )
    filter_str = filter_str.replace("'", r"\'")
    
    cmd = [
        'ffmpeg', '-i', input_path,
        '-vf', filter_str,
        '-af', f"atempo={speed}",
        '-c:v', 'libx264', '-c:a', 'aac',
        output_path, '-y'
    ]
    subprocess.run(cmd, capture_output=True)
    return output_path

# ---------- Telegram Handlers ----------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Welcome! Send me a YouTube video link.\n"
        "I'll intelligently cut it into interesting 30-second parts (removing silence and boring parts), "
        "make them unique, and add teleprompter text."
    )

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    url = update.message.text
    if 'youtube.com' not in url and 'youtu.be' not in url:
        await update.message.reply_text("❌ Please send a valid YouTube link.")
        return

    status_msg = await update.message.reply_text("⏳ Downloading video...")
    try:
        # Download
        video_path, title = download_video(url)
        await status_msg.edit_text("✅ Download complete.\n🔍 Analyzing video for interesting parts...")

        # Get interesting segments
        segments = get_interesting_segments(video_path)
        if not segments:
            await status_msg.edit_text("❌ No interesting segments found.")
            return

        await status_msg.edit_text(f"🎬 Found {len(segments)} interesting segments. Processing...")

        # Process each segment
        for i, (start, end) in enumerate(segments):
            # Extract segment
            seg_path = f"{DOWNLOAD_DIR}/seg_{i:03d}.mp4"
            extract_segment(video_path, seg_path, start, end)
            
            # Make unique
            out_path = seg_path.replace('.mp4', '_final.mp4')
            make_video_unique(seg_path, out_path, text=f"{title[:50]}... Part {i+1}")
            
            # Send
            with open(out_path, 'rb') as f:
                await update.message.reply_video(
                    video=f,
                    caption=f"Part {i+1}: {start:.1f}s - {end:.1f}s | {title[:50]}...",
                    supports_streaming=True
                )
            
            # Cleanup
            os.remove(seg_path)
            if os.path.exists(out_path):
                os.remove(out_path)

        # Cleanup original
        os.remove(video_path)
        await status_msg.edit_text("✅ All done! Sent all interesting parts.")

    except Exception as e:
        await status_msg.edit_text(f"❌ Error: {str(e)}")

def main():
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    print("🤖 Intelligent Video Cutter Bot started...")
    app.run_polling()

if __name__ == "__main__":
    main()
