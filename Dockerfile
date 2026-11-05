FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PORT=8822
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
RUN mkdir -p /data
EXPOSE 8822
CMD ["gunicorn","--workers","2","--threads","8","--timeout","60","--bind","0.0.0.0:8822","app:app"]
