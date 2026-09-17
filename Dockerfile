FROM python:3.11-slim

# Prevent python from writing pyc files and buffering stdout/stderr
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Install system dependencies for OpenCV and network tools
RUN apt-get update && apt-get install -y --no-install-recommends \
    libgl1 \
    libglib2.0-0 \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Install python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application files
COPY . .

# Ensure instance and upload directories exist
RUN mkdir -p /app/instance /app/static/uploads

EXPOSE 5000

CMD ["gunicorn", "-c", "gunicorn_config.py", "wsgi:app"]
