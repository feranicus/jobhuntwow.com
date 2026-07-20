# Candidate agent — Cassandra-style. Tiny by design: no browser, no terminal, no agent framework.
FROM python:3.12-slim
RUN pip install --no-cache-dir httpx
WORKDIR /app
COPY jhw_bot.py /app/jhw_bot.py
CMD ["python3","/app/jhw_bot.py"]
