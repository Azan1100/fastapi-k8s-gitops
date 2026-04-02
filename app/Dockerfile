FROM python:3.10-slim

WORKDIR /app
COPY . .

RUN pip install --no-cache-dir -r requirements.txt

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
  CMD python -c "import requests; requests.get('http://localhost:8000/health', timeout=1)"

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
