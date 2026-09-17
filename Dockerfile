FROM python:3.12-slim

WORKDIR /app

# Install deps first for Docker cache
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Create data dir for SQLite
RUN mkdir -p /app/data

CMD ["python", "bot.py"]
