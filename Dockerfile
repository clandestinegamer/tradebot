FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 TZ=Asia/Seoul
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app
COPY dashboard ./dashboard
COPY config*.toml ./
RUN useradd -m bot && mkdir -p /app/data && chown -R bot /app
USER bot
EXPOSE 8000
CMD ["uvicorn", "app.server:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
