# Use Python 3.12 → compatible with PyTorch
FROM python:3.12-slim

# Prevent prompts
ENV DEBIAN_FRONTEND=noninteractive

# Install system dependencies
RUN apt-get update && apt-get install -y \
    ffmpeg \
    libsm6 \
    libxext6 \
    && rm -rf /var/lib/apt/lists/*

# Create app folder
WORKDIR /app

# Copy requirements first to use Docker cache
COPY requirements.txt .

# Install dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Copy all project files
COPY . .

# Expose port for Railway
EXPOSE 8000

# Start using Gunicorn
CMD ["gunicorn", "--bind", "0.0.0.0:8000", "app:app"]
