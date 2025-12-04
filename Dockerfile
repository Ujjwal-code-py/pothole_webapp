# Use Python 3.10 (recommended for Torch CPU stability)
FROM python:3.10-slim

# Prevent prompts
ENV DEBIAN_FRONTEND=noninteractive

# Install system dependencies (YOLO + OpenCV needs these)
RUN apt-get update && apt-get install -y \
    ffmpeg \
    libsm6 \
    libxext6 \
    libgl1 \
    && rm -rf /var/lib/apt/lists/*

# Set app directory
WORKDIR /app

# Copy requirements first (better caching)
COPY requirements.txt .

# Upgrade pip + install dependencies
RUN pip install --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# Copy all app files
COPY . .

# Expose app port
EXPOSE 8000

# Run server
CMD ["gunicorn", "--bind", "0.0.0.0:8000", "app:app"]
