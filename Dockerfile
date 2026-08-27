FROM python:3.13-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8080

COPY bot.py ./bot.py

EXPOSE 8080

CMD ["python", "bot.py"]