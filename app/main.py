"""
AutoEq mikroservisi.

Sadece iki endpoint:
  - GET  /health    → servis canlı mı kontrolü
  - POST /equalize  → source ve target kulaklık ID'leri verilir, Web Audio API
                      uyumlu parametric EQ filtre listesi döndürür.

Bu servis dış dünyaya kapalıdır; Docker compose üzerinden sadece Spring Boot
servisi erişebilir. Hiçbir veritabanı veya state tutmaz.
"""

import logging
import os
from pathlib import Path
from typing import List, Literal, Optional

from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field

# AutoEq kütüphanesi
from autoeq.frequency_response import FrequencyResponse
from autoeq.constants import PEQ_CONFIGS

# Lokal modüller
from app.search import HeadphoneIndex

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Konfigürasyon
# ---------------------------------------------------------------------------

# Measurements klasörünün kökü. Dockerfile bunu /app/measurements'a kopyalıyor.
# Override için MEASUREMENTS_ROOT env değişkeni kullanılabilir (test için).
MEASUREMENTS_ROOT = Path(os.environ.get("MEASUREMENTS_ROOT", "/app/measurements"))

# Default sample rate. Web Audio API context'i genelde 44100 veya 48000.
# 48000 daha modern, ama uyumluluk için 44100 default.
DEFAULT_FS = 44100

# Default parametric EQ konfigürasyonu. AutoEq'nin sunduğu hazır config'lerden.
# 8 peaking + low-shelf + high-shelf, autoeq.app'in de defaultu.
DEFAULT_PEQ_CONFIG = "8_PEAKING_WITH_SHELVES"

# Default max gain limiti — kullanıcı kulaklığı çok bozuksa bile +12dB'den
# fazla kazanç vermeyiz. AutoEq de bu değeri default kullanıyor.
DEFAULT_MAX_GAIN_DB = 12.0

# ---------------------------------------------------------------------------
# Pydantic modelleri
# ---------------------------------------------------------------------------


class EqualizeRequest(BaseModel):
    """Spring Boot'tan gelen istek payload'ı."""

    source_id: str = Field(
        ...,
        description=(
            "Kullanıcının kulaklığının AutoEq measurements yolu. "
            "Örnek: 'oratory1990/over-ear/Sennheiser HD 650'. "
            "Uzantı (.csv) ve 'data' segmenti yazılmaz."
        ),
        examples=["oratory1990/over-ear/Sennheiser HD 650"],
    )
    target_id: str = Field(
        ...,
        description="Hedef ürünün AutoEq measurements yolu (Product.autoeq_id).",
        examples=["oratory1990/over-ear/Sennheiser HD 800"],
    )
    fs: int = Field(
        DEFAULT_FS,
        description="Sample rate (Hz). Web Audio context'iyle uyumlu olmalı.",
        ge=8000,
        le=192000,
    )
    max_gain_db: float = Field(
        DEFAULT_MAX_GAIN_DB,
        description="Tek bir filtrenin maksimum kazancı (dB).",
        gt=0,
        le=24,
    )


class BiquadFilter(BaseModel):
    """
    Tek bir biquad filtresi. Web Audio API'nin BiquadFilterNode'una
    birebir map'lenir:
      filter.type = type.lower().replace("_", "")  → "peaking" / "lowshelf" / "highshelf"
      filter.frequency.value = fc
      filter.Q.value = q
      filter.gain.value = gain
    """

    type: Literal["PEAKING", "LOW_SHELF", "HIGH_SHELF"]
    fc: float = Field(..., description="Center frequency (Hz)")
    q: float = Field(..., description="Quality factor")
    gain: float = Field(..., description="Gain (dB)")


class EqualizeResponse(BaseModel):
    """FastAPI'nin döndürdüğü cevap."""

    source_id: str
    target_id: str
    fs: int
    preamp_db: float = Field(
        ...,
        description=(
            "Clipping'i önlemek için master volume'a uygulanması gereken negatif gain. "
            "Web Audio'da bir GainNode olarak filtre zincirinin başına koyulur."
        ),
    )
    filters: List[BiquadFilter]


class HealthResponse(BaseModel):
    status: str
    measurements_root: str
    measurements_exists: bool
    indexed_headphones: int


class HeadphoneSearchEntry(BaseModel):
    """Arama sonucundaki tek bir kulaklık."""
    id: str = Field(..., description="EQ isteğinde source_id olarak kullanılacak ID")
    label: str = Field(..., description="Kullanıcıya gösterilecek isim")
    form: str = Field(..., description="over-ear | in-ear | earbud")
    source: str = Field(..., description="Ölçüm kaynağı (oratory1990, crinacle, ...)")


class HeadphoneSearchResponse(BaseModel):
    results: List[HeadphoneSearchEntry]
    total: int


# ---------------------------------------------------------------------------
# Yardımcılar
# ---------------------------------------------------------------------------


def _resolve_measurement_path(headphone_id: str) -> Path:
    """
    'oratory1990/over-ear/Sennheiser HD 650' formatındaki ID'yi gerçek
    dosya yoluna çevirir. AutoEq'nin gerçek dizin yapısı:
      measurements/{kaynak}/data/{form}/{kulaklık}.csv

    Yani 'data' segmenti otomatik araya konuyor. Bu, kullanıcı API'sini
    daha temiz tutuyor — admin "oratory1990/over-ear/HD 650" girer,
    içerideki 'data' segmentini servis halleder.

    Path traversal'a karşı koruma da burada yapılıyor: ID'nin '..' içermesi
    veya MEASUREMENTS_ROOT dışına çıkması yasak.
    """
    parts = headphone_id.strip().split("/")
    if len(parts) < 3:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Geçersiz headphone ID formatı: '{headphone_id}'. "
                "Beklenen format: 'kaynak/form/kulaklık_adı' "
                "(örn: 'oratory1990/over-ear/Sennheiser HD 650')."
            ),
        )

    source, form, *name_parts = parts
    name = "/".join(name_parts)  # kulaklık adında '/' olmaz ama defansif

    # Path traversal koruması
    if ".." in parts or any(p.startswith("/") for p in parts):
        raise HTTPException(status_code=400, detail="Geçersiz karakter ID'de.")

    candidate = MEASUREMENTS_ROOT / source / "data" / form / f"{name}.csv"

    # Resolve edilmiş path'in hâlâ MEASUREMENTS_ROOT altında olduğunu doğrula
    try:
        candidate_resolved = candidate.resolve()
        root_resolved = MEASUREMENTS_ROOT.resolve()
        candidate_resolved.relative_to(root_resolved)
    except (ValueError, OSError):
        raise HTTPException(status_code=400, detail="ID kök dizin dışına çıkıyor.")

    if not candidate.is_file():
        raise HTTPException(
            status_code=404,
            detail=f"Kulaklık ölçümü bulunamadı: '{headphone_id}'. (Beklenen yol: {candidate})",
        )

    return candidate


def _map_filter_type(autoeq_type: str) -> str:
    """
    AutoEq'nin filtre type isimlerini bizim enum'umuza çevirir.
    AutoEq'de filtre sınıfı adından type türetilir: 'Peaking', 'LowShelf', 'HighShelf'.
    """
    mapping = {
        "PEAKING": "PEAKING",
        "LOW_SHELF": "LOW_SHELF",
        "LOWSHELF": "LOW_SHELF",
        "HIGH_SHELF": "HIGH_SHELF",
        "HIGHSHELF": "HIGH_SHELF",
    }
    normalized = autoeq_type.upper().replace(" ", "_")
    if normalized not in mapping:
        raise ValueError(f"Bilinmeyen filtre tipi: {autoeq_type}")
    return mapping[normalized]


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# FastAPI app + lifespan (startup'ta index kurulumu)
# ---------------------------------------------------------------------------

from contextlib import asynccontextmanager

# Global index — startup'ta dolar, sonra read-only
_index: Optional[HeadphoneIndex] = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup'ta kulaklık indexini kur."""
    global _index
    logger.info("Index kurulumu başladı: %s", MEASUREMENTS_ROOT)
    _index = HeadphoneIndex.build_from_measurements(MEASUREMENTS_ROOT)
    logger.info("Index hazır: %d kulaklık.", len(_index))
    yield
    # Shutdown: özel cleanup gerekmiyor


app = FastAPI(
    title="AutoEq Service",
    description="Internal microservice. Kulaklık → kulaklık EQ profili üretir.",
    version="1.1.0",
    lifespan=lifespan,
)


@app.get("/health", response_model=HealthResponse)
def health() -> HealthResponse:
    """Liveness probe için basit health check."""
    return HealthResponse(
        status="ok",
        measurements_root=str(MEASUREMENTS_ROOT),
        measurements_exists=MEASUREMENTS_ROOT.is_dir(),
        indexed_headphones=len(_index) if _index is not None else 0,
    )


@app.get("/headphones", response_model=HeadphoneSearchResponse)
def search_headphones(
    q: Optional[str] = Query(None, description="Arama sorgusu. Boş ise alfabetik ilk N."),
    limit: int = Query(20, ge=1, le=50, description="Maks sonuç sayısı."),
) -> HeadphoneSearchResponse:
    """
    Kulaklık arama endpoint'i. Frontend autocomplete'i bu endpoint'i çağırır.

    Token-based, case-insensitive, prefix match. Aynı kulaklığın birden
    fazla kaynağı varsa kaynak önceliğine göre tek girdi döner.
    """
    if _index is None:
        # Lifespan henüz tamamlanmadıysa (test ortamı vs.)
        raise HTTPException(status_code=503, detail="Index henüz hazır değil.")

    results = _index.search(q, limit=limit)
    return HeadphoneSearchResponse(
        results=[
            HeadphoneSearchEntry(id=e.id, label=e.label, form=e.form, source=e.source)
            for e in results
        ],
        total=len(results),
    )


@app.post("/equalize", response_model=EqualizeResponse)
def equalize(req: EqualizeRequest) -> EqualizeResponse:
    """
    Ana endpoint. Source kulaklığı target kulaklığa benzetecek
    parametric EQ filtrelerini döndürür.

    İç akış:
      1. Source ve target CSV'lerini diskten oku
      2. FrequencyResponse pipeline'ını çalıştır:
         interpolate → center → compensate(target) → smoothen → equalize
      3. Equalization eğrisini parametric EQ filtrelerine optimize et
      4. Filter listesini ve preamp değerini Web Audio uyumlu formatta döndür
    """
    source_path = _resolve_measurement_path(req.source_id)
    target_path = _resolve_measurement_path(req.target_id)

    if source_path == target_path:
        # Aynı kulaklık seçildiyse boş filtre listesi döndür — anlamsız bir
        # işlem yapmamış oluyoruz, frontend hata almıyor.
        logger.info("Source ve target aynı, boş EQ döndürülüyor.")
        return EqualizeResponse(
            source_id=req.source_id,
            target_id=req.target_id,
            fs=req.fs,
            preamp_db=0.0,
            filters=[],
        )

    try:
        # Source: kullanıcının kulaklığı — eşitlenecek olan
        fr = FrequencyResponse.read_csv(str(source_path))
        # Target: ulaşılmak istenen hedef (mağazadaki ürün)
        target_fr = FrequencyResponse.read_csv(str(target_path))
    except Exception as exc:
        logger.exception("CSV okuma hatası.")
        raise HTTPException(status_code=500, detail=f"CSV okunamadı: {exc}")

    try:
        # AutoEq pipeline'ı. Her adım fr'nin internal array'lerini günceller.
        fr.interpolate()       # log-spaced standart frekanslara resample
        target_fr.interpolate()
        fr.center()            # 100-10000Hz ortalamasını 0 dB'e taşı
        target_fr.center()
        fr.compensate(target_fr)  # error ve target array'lerini hesapla
        fr.smoothen()             # Savitzky-Golay smoothing
        fr.equalize(             # equalization eğrisini üret
            max_gain=req.max_gain_db,
            concha_interference=False,  # in-ear olmayan durumlar için False
        )

        # Parametric EQ optimizasyonu — bu işin asıl pahalı kısmı.
        # PEQ_CONFIGS[DEFAULT_PEQ_CONFIG] = 8 peaking + low/high shelf yapısı.
        peqs = fr.optimize_parametric_eq(
            PEQ_CONFIGS[DEFAULT_PEQ_CONFIG],
            req.fs,
        )
    except Exception as exc:
        logger.exception("AutoEq işleme hatası.")
        raise HTTPException(status_code=500, detail=f"EQ hesaplama hatası: {exc}")

    # optimize_parametric_eq bir PEQ listesi döndürür (config'de birden fazla
    # filter group olabileceği için). Standart config'de tek grup gelir.
    if not peqs:
        return EqualizeResponse(
            source_id=req.source_id,
            target_id=req.target_id,
            fs=req.fs,
            preamp_db=0.0,
            filters=[],
        )

    peq = peqs[0]
    filters_out: List[BiquadFilter] = []
    for filt in peq.filters:
        # AutoEq filter sınıf adından type türetiliyor: type(filt).__name__
        # ('Peaking', 'LowShelf', 'HighShelf')
        cls_name = type(filt).__name__
        # 'LowShelf' → 'LOW_SHELF'
        normalized = ""
        for i, ch in enumerate(cls_name):
            if ch.isupper() and i > 0:
                normalized += "_"
            normalized += ch.upper()

        filters_out.append(
            BiquadFilter(
                type=_map_filter_type(normalized),
                fc=float(filt.fc),
                q=float(filt.q),
                gain=float(filt.gain),
            )
        )

    # Preamp hesabı — clipping önleme.
    # AutoEq webapp'i şu yaklaşımı kullanıyor: PEQ filtrelerinin frekans
    # üzerindeki BİRLEŞİK tepkisinin (frequency response) maksimumunu al,
    # ona ufak bir güvenlik payı ekle, negatifini preamp yap.
    # Bu, pozitif gain'lerin toplamından çok daha doğru — çünkü farklı
    # frekanslardaki gain'ler birbirini iptal eder, hepsi toplanmaz.
    try:
        # PEQ objesinin combined frequency response'u
        # peq.fr property'si tüm filtrelerin birleşik dB tepkisini döndürür
        combined_response = peq.fr  # numpy array, frequency üzerinden dB
        peak_db = float(combined_response.max())
        preamp_db = -(peak_db + 0.2)  # 0.2 dB headroom, AutoEq'in kullandığı değer
    except AttributeError:
        # Fallback: PEQ.fr yoksa (eski versiyon vb.), pozitif gain'lerin
        # max'ını kullan — bireysel filtre toplamından daha güvenli
        max_positive = max((f.gain for f in filters_out if f.gain > 0), default=0.0)
        preamp_db = -(max_positive + 0.2)

    logger.info(
        "EQ üretildi: source=%s target=%s filtre_sayısı=%d preamp=%.2fdB",
        req.source_id, req.target_id, len(filters_out), preamp_db,
    )

    return EqualizeResponse(
        source_id=req.source_id,
        target_id=req.target_id,
        fs=req.fs,
        preamp_db=round(preamp_db, 2),
        filters=filters_out,
    )