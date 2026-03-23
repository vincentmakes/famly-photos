FROM python:3.12-slim

WORKDIR /app

COPY pyproject.toml .
COPY famly-photos.png .
COPY src/ ./src/

# Install dependencies
RUN pip install --no-cache-dir \
    "fastapi>=0.115.0" \
    "uvicorn[standard]>=0.32.0" \
    "requests>=2.32.0" \
    "jinja2>=3.1.0" \
    "apscheduler>=3.10.0" \
    "pydantic>=2.10.0" \
    "pydantic-settings>=2.6.0" \
    "Pillow>=10.0.0" \
    "python-multipart>=0.0.9"

# Data + photo mount points
RUN mkdir -p /appdata/data /photos
VOLUME ["/appdata/data", "/photos"]

EXPOSE 8811

WORKDIR /app/src
CMD ["python", "main.py"]
