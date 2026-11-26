FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app
RUN apt-get update \
    && apt-get install -y --no-install-recommends nmap iputils-ping libpcap0.8 \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app
RUN mkdir -p /app/data

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
  CMD python -c "import base64,os,urllib.request; r=urllib.request.Request('http://127.0.0.1:8000/api/summary'); r.add_header('Authorization','Basic '+base64.b64encode((os.environ['DASHBOARD_USERNAME']+':'+os.environ['DASHBOARD_PASSWORD']).encode()).decode()); urllib.request.urlopen(r,timeout=4).close()"
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
