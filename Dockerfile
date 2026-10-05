FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

# pydub needs ffmpeg for mp3 decode at startup.
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg && rm -rf /var/lib/apt/lists/*

COPY backend/requirements-render.txt /app/backend/requirements-render.txt
RUN pip install --no-cache-dir -r /app/backend/requirements-render.txt

COPY . /app

EXPOSE 5050

CMD ["python", "backend/main.py"]
