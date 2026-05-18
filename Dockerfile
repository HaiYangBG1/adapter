FROM python:3.12-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    ADAPTER_HOST=0.0.0.0 \
    ADAPTER_PORT=8000 \
    ADAPTER_ENABLE_OFFICE_RENDER=1 \
    ADAPTER_LIBREOFFICE_BIN=/usr/bin/soffice

RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends \
      ca-certificates \
      curl \
      fontconfig \
      fonts-noto-cjk \
      fonts-wqy-zenhei \
      libreoffice-calc \
      libreoffice-impress \
      libreoffice-writer \
      libreoffice-core \
      python3-uno; \
    rm -rf /var/lib/apt/lists/*; \
    fc-cache -f

WORKDIR /app

COPY requirements.txt /app/requirements.txt
RUN python -m pip install -r /app/requirements.txt

COPY adapter.py /app/adapter.py

EXPOSE 8000

CMD ["python", "/app/adapter.py"]
