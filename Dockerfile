# =============================================================================
# Stage 1: AutoEq measurements'ı sparse-checkout ile çek
# Sadece measurements/ klasörünü istiyoruz, tüm repo (~600MB) değil
# =============================================================================
FROM alpine/git:latest AS measurements-fetcher

WORKDIR /fetch
RUN git clone --depth 1 --filter=blob:none --sparse \
    https://github.com/jaakkopasanen/AutoEq.git && \
    cd AutoEq && \
    git sparse-checkout set measurements

# =============================================================================
# Stage 2: Asıl servis imajı
# Python 3.11 — AutoEq 3.12'yi desteklemiyor (pyproject.toml: <3.12)
# =============================================================================
FROM python:3.11-slim

# libsndfile: AutoEq'nin soundfile bağımlılığı için zorunlu sistem kütüphanesi
# build-essential bazı bağımlılıkların wheel'i yoksa fallback için
RUN apt-get update && apt-get install -y --no-install-recommends \
    libsndfile1 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Python bağımlılıkları — önce kopyalayıp install et ki layer cache çalışsın
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Measurements klasörünü stage 1'den al
COPY --from=measurements-fetcher /fetch/AutoEq/measurements /app/measurements

# Uygulama kodu
COPY app /app/app

# Production mode: --reload YOK
# Tek worker yeterli (CPU-bound iş zaten, uvicorn workers GIL nedeniyle yardım etmez;
# yatay ölçekleme gerekirse compose'da replicas artırılır)
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]