FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
RUN apt-get update && apt-get install -y ffmpeg && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir yt-dlp
COPY app/ .
ENTRYPOINT ["python3", "prepare_music.py"]
CMD ["/music"]
