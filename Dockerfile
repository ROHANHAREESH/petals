# Playwright image that ALREADY includes v1.55.0 browsers & python package
FROM mcr.microsoft.com/playwright/python:v1.55.0-jammy

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Install only your app deps (do NOT install playwright again)
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# App code
COPY tool_api.py form_filler_tool.py /app/

ENV PORT=8000
EXPOSE 8000

# Use $PORT from Render; default to 8000 locally
CMD ["bash","-lc","uvicorn tool_api:app --host 0.0.0.0 --port ${PORT:-8000}"]
