FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app
RUN mkdir -p /app/data
ENV PYTHONUNBUFFERED=1
CMD uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-10000}
