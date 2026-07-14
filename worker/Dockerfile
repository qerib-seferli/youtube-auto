FROM python:3.12-slim
RUN apt-get update && apt-get install -y ffmpeg fonts-dejavu-core ca-certificates && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY worker.py .
RUN mkdir -p /app/work /app/music
CMD ["python","-u","worker.py"]
