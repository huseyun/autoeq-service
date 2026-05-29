"""
Test fixture'ları. AutoEq'nin gerçek measurements klasörü olmadan
test çalıştırabilmek için sahte CSV ölçümleri oluşturuyoruz.
"""
import os
import tempfile
from pathlib import Path

import numpy as np
import pytest


def _generate_csv(path: Path, response_shape: str = "flat") -> None:
    """
    AutoEq formatında sahte bir frekans yanıt CSV'si oluşturur.
    Format: 'frequency,raw' header + log-spaced veriler.
    """
    # AutoEq'nin standart log-spaced frekansları (20Hz - 20kHz)
    frequencies = np.logspace(np.log10(20), np.log10(20000), num=600)

    if response_shape == "flat":
        # Düz yanıt — referans olarak
        raw = np.zeros_like(frequencies)
    elif response_shape == "bass_heavy":
        # Bass'ı şişirilmiş kulaklık — düşük frekanslarda +6dB
        raw = 6.0 * np.exp(-((np.log10(frequencies) - np.log10(80)) ** 2) / 0.5)
    elif response_shape == "treble_heavy":
        # Yüksek frekansları parlak — 8kHz civarında +5dB
        raw = 5.0 * np.exp(-((np.log10(frequencies) - np.log10(8000)) ** 2) / 0.2)
    else:
        raise ValueError(f"Bilinmeyen şekil: {response_shape}")

    # AutoEq pipeline'ında smoothing var, çok düşük seviyede gürültü
    # ekleyerek daha gerçekçi yapıyoruz
    rng = np.random.default_rng(seed=42)
    raw = raw + rng.normal(0, 0.1, size=raw.shape)

    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        f.write("frequency,raw\n")
        for fr, rw in zip(frequencies, raw):
            f.write(f"{fr:.4f},{rw:.4f}\n")


@pytest.fixture(scope="session")
def fake_measurements_root():
    """
    Test sürecinde geçici bir measurements klasörü oluşturup
    MEASUREMENTS_ROOT env değişkenini ona yönlendirir.
    """
    tmpdir = tempfile.mkdtemp(prefix="autoeq_test_")
    root = Path(tmpdir)

    # AutoEq'nin gerçek dizin yapısını taklit et:
    # measurements/{kaynak}/data/{form}/{ad}.csv
    _generate_csv(
        root / "oratory1990" / "data" / "over-ear" / "Test Headphone Bass.csv",
        "bass_heavy",
    )
    _generate_csv(
        root / "oratory1990" / "data" / "over-ear" / "Test Headphone Treble.csv",
        "treble_heavy",
    )
    _generate_csv(
        root / "oratory1990" / "data" / "over-ear" / "Test Headphone Flat.csv",
        "flat",
    )

    # Env'i ayarla, sonra app'i import et (env okunması import sırasında olur)
    os.environ["MEASUREMENTS_ROOT"] = str(root)

    yield root

    # Cleanup
    import shutil
    shutil.rmtree(tmpdir, ignore_errors=True)


@pytest.fixture
def client(fake_measurements_root):
    """FastAPI test client."""
    # Module re-import: MEASUREMENTS_ROOT env'inin etkili olması için
    import importlib
    from app import main
    importlib.reload(main)

    from fastapi.testclient import TestClient
    return TestClient(main.app)