# Use Python base image
FROM python:3.10-slim

# Set the working directory inside the container
WORKDIR /app

# Copy the requirements first to leverage Docker cache
COPY requirements.txt .

# Install dependencies
RUN pip install --no-cache-dir -r requirements.txt

# COPY EVERYTHING from your repo into the /app folder
COPY . .

# Run the bot
CMD ["python", "btcusd_institutional_bot.py"]
