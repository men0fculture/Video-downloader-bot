FROM python:3.10-slim

RUN apt-get update && apt-get install -y \
    ffmpeg \
    wget \
    unzip \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Download Vosk Hindi model (small)
RUN wget https://alphacephei.com/vosk/models/vosk-model-small-hi-0.22.zip && \
    unzip vosk-model-small-hi-0.22.zip -d /tmp/ && \
    rm vosk-model-small-hi-0.22.zip

COPY . .

CMD ["python", "bot.py"]]
