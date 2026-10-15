FROM python:3.12-slim
RUN useradd -m rv
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY railvision ./railvision
COPY web ./web
COPY config ./config
USER rv
ENV PYTHONUNBUFFERED=1
EXPOSE 8000
CMD ["uvicorn", "railvision.api.fastapi_app:asgi", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers"]
