FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY prepare_music.py .

ENTRYPOINT ["python3", "prepare_music.py"]
CMD ["/music"]
