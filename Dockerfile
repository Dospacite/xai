# Use an official Python image
FROM python:3.11-slim

# Install system dependencies for Selenium + Chrome
RUN apt-get update && apt-get install -y \
    wget unzip curl gnupg2 \
    chromium chromium-driver \
    && rm -rf /var/lib/apt/lists/*

# Set working directory
WORKDIR /app

# Copy requirements first for better caching
COPY app/requirements.txt .

# Install Python packages
RUN pip install --no-cache-dir -r requirements.txt

# Copy project files
COPY app/ .

# Create data directory for screenshots
RUN mkdir -p /app/data/screenshots

# Run the startup script
CMD ["python", "startup.py"]
