# Use an official Python image
FROM python:3.11-slim

# Install minimal tools used during dependency setup.
RUN apt-get update && apt-get install -y \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Set working directory
WORKDIR /app

# Copy requirements first for better caching
COPY app/requirements.txt .

# Install Python packages
RUN pip install --no-cache-dir -r requirements.txt \
    && scrapling install

# Copy project files
COPY app/ .

# Run the startup script
CMD ["python", "startup.py"]
