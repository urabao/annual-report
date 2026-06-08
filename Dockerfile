FROM python:3.12-slim

WORKDIR /app

# Install system dependencies needed for PyMuPDF and pdfplumber
RUN apt-get update && apt-get install -y \
    build-essential \
    libffi-dev \
    libmupdf-dev \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements and install
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY . .

# Expose port 8000
EXPOSE 8000

# Start Uvicorn server
CMD ["uvicorn", "backend.app:app", "--host", "0.0.0.0", "--port", "8000"]
