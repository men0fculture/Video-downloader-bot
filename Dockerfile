FROM python:3.10-slim

# Install ffmpeg for video processing and splitting
[span_1](start_span)RUN apt-get update && apt-get install -y ffmpeg && rm -rf /var/lib/apt/lists/*[span_1](end_span)

WORKDIR /app

# Copy and install dependencies
COPY requirements.txt .
[span_2](start_span)RUN pip install --no-cache-dir -r requirements.txt[span_2](end_span)

# Copy the bot code
COPY . .

# Run the bot
CMD ["python", "bot.py"]
