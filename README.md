# AutoEq Service

Spring Boot backend için lokal mikroservis. Source ve target kulaklık ID'leri
verilir, Web Audio API uyumlu parametric EQ filtreleri döndürür.

## API

### `GET /health`

```json
{
  "status": "ok",
  "measurements_root": "/app/measurements",
  "measurements_exists": true
}
```

### `POST /equalize`

**Request:**
```json
{
  "source_id": "oratory1990/over-ear/Sennheiser HD 650",
  "target_id": "oratory1990/over-ear/Sennheiser HD 800",
  "fs": 44100,
  "max_gain_db": 12
}
```

`fs` ve `max_gain_db` opsiyoneldir.

**Response:**
```json
{
  "source_id": "oratory1990/over-ear/Sennheiser HD 650",
  "target_id": "oratory1990/over-ear/Sennheiser HD 800",
  "fs": 44100,
  "preamp_db": -3.5,
  "filters": [
    {"type": "LOW_SHELF", "fc": 80.0, "q": 0.7, "gain": -1.5},
    {"type": "PEAKING",   "fc": 200,  "q": 1.4, "gain": 2.0},
    ...
  ]
}
```

## ID formatı

AutoEq measurements dosya yolundan türetilir, ama `data/` segmenti
**yazılmaz** (servis otomatik ekler):

```
Gerçek dosya: measurements/oratory1990/data/over-ear/Sennheiser HD 800.csv
ID'si:        oratory1990/over-ear/Sennheiser HD 800
```

Hangi kulaklıkların var olduğunu görmek için:
https://github.com/jaakkopasanen/AutoEq/tree/master/measurements

## Lokal çalıştırma (Docker)

```bash
# Tek başına
docker compose up --build

# Test
curl http://localhost:8000/health   # ← yalnızca compose'da 'ports' tanımlarsan çalışır
                                    #   default olarak DIŞA KAPALI
```

Compose dosyasında `ports` yok, sadece `expose` var — yani sadece aynı
Docker network'ündeki diğer servisler erişebilir. Host'tan test etmek
için geçici olarak `ports: ["8000:8000"]` ekle.

## Lokal çalıştırma (Docker'sız, geliştirme için)

```bash
python3.11 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# Measurements'ı manuel indir
git clone --depth 1 --filter=blob:none --sparse https://github.com/jaakkopasanen/AutoEq.git /tmp/AutoEq
cd /tmp/AutoEq && git sparse-checkout set measurements && cd -

# Path'i göster ve çalıştır
export MEASUREMENTS_ROOT=/tmp/AutoEq/measurements
uvicorn app.main:app --reload --port 8000
```

## Test

```bash
pip install -r requirements.txt
pytest tests/ -v
```

Testler sahte CSV üretip kullandığı için gerçek measurements klasörüne
gerek yoktur.

## Spring Boot tarafı

`AUTOEQ_URL` env'iyle servise erişin (Docker DNS):

```yaml
environment:
  AUTOEQ_URL: http://autoeq:8000
```

Java tarafında basit bir WebClient çağrısı:

```java
record EqualizeRequest(String source_id, String target_id) {}
record BiquadFilter(String type, double fc, double q, double gain) {}
record EqualizeResponse(String source_id, String target_id, int fs,
                        double preamp_db, List<BiquadFilter> filters) {}

EqualizeResponse response = webClient.post()
    .uri(autoeqUrl + "/equalize")
    .bodyValue(new EqualizeRequest(userHeadphoneId, product.getAutoeqId()))
    .retrieve()
    .bodyToMono(EqualizeResponse.class)
    .block();
```