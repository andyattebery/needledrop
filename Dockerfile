FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends \
    alsa-utils ffmpeg && rm -rf /var/lib/apt/lists/*
COPY . /src
RUN pip install --no-cache-dir /src && rm -rf /src
ENTRYPOINT ["python", "-m", "needledrop"]
CMD ["--config", "/etc/needledrop/config.yaml"]
