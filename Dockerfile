FROM python:3.12-slim

# Install system dependencies required by discord.py
RUN apt-get update && apt-get install -y --no-install-recommends \
    libopus0 \
    libffi-dev \
    libnacl-dev \
    git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

CMD ["python", "antiraid_bot.py"]
