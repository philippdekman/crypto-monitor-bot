FROM python:3.12-slim

WORKDIR /app

# Install build tools needed for ed25519-blake2b (hdwallet dependency)
RUN apt-get update && apt-get install -y --no-install-recommends gcc build-essential \
    && rm -rf /var/lib/apt/lists/*

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Remove build tools to keep image small
RUN apt-get purge -y --auto-remove gcc build-essential

# Copy source code
COPY *.py .

# Run bot
CMD ["python", "bot.py"]
