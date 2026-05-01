FROM python:3.12-slim

WORKDIR /app

COPY copy_trader/central /app/copy_trader/central

RUN echo "" > /app/copy_trader/__init__.py \
 && mkdir -p /data

ENV COPY_TRADER_HUB_HOST=0.0.0.0 \
    COPY_TRADER_HUB_PORT=8080 \
    COPY_TRADER_HUB_STORE=/data/central_hub_signals.jsonl \
    PYTHONUNBUFFERED=1

EXPOSE 8080

CMD ["python", "-m", "copy_trader.central.hub_server"]
