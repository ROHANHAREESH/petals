FROM mcr.microsoft.com/playwright/python:v1.48.0-jammy
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r requirements.txt
COPY tool_api.py form_filler_tool.py /app/
ENV PORT=8000
EXPOSE 8000
CMD ["bash","-lc","uvicorn tool_api:app --host 0.0.0.0 --port $PORT"]
