"""
Kulaklık arama indexi.

Startup'ta measurements/ klasörünü tarayıp tüm kulaklıkları bir in-memory
listeye koyar. Aynı kulaklığın birden fazla kaynağı varsa (oratory1990,
crinacle vs.), önceden tanımlı kaynak önceliği listesine göre en
güveniliri seçilir.

Arama: token-based, case-insensitive, prefix match. Skor + alfabetik
sıralama. autoeq.app'in yaptığına yakın bir yaklaşım.
"""

import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

logger = logging.getLogger(__name__)

# Kaynak güven sırası — düşük index = daha güvenilir.
# Aynı kulaklık birden fazla kaynakta varsa, sıralamada öne çıkan tutulur.
SOURCE_PRIORITY = [
    "oratory1990",
    "rtings",
    "crinacle",
    "innerfidelity",
    "headphonecom",
    "hypethesonics",
    "squiglink",
]


def _source_rank(source: str) -> int:
    """Düşük sayı = daha öncelikli."""
    try:
        return SOURCE_PRIORITY.index(source)
    except ValueError:
        # Listede olmayan kaynaklar en sona
        return len(SOURCE_PRIORITY)


@dataclass(frozen=True)
class HeadphoneEntry:
    """Index'teki bir kulaklık girdisi."""
    id: str           # "oratory1990/over-ear/Sennheiser HD 650"
    label: str        # "Sennheiser HD 650" — kullanıcıya gösterilen
    form: str         # "over-ear" | "in-ear" | "earbud"
    source: str       # "oratory1990"
    _tokens: tuple    # ("sennheiser", "hd", "650") — arama için


# Tokenize regex'i: kelime karakterleri grupları
_TOKEN_RE = re.compile(r"\w+", re.UNICODE)


def _tokenize(text: str) -> tuple:
    """
    'Sennheiser HD-650 (Black)' → ('sennheiser', 'hd', '650', 'black')
    Lowercase + noktalama temizleme.
    """
    return tuple(m.group(0).lower() for m in _TOKEN_RE.finditer(text))


class HeadphoneIndex:
    """
    Tüm kulaklıkların in-memory indexi.

    Startup'ta bir kez build edilir, sonra read-only.
    """

    def __init__(self, entries: List[HeadphoneEntry]):
        self._entries = entries

    @classmethod
    def build_from_measurements(cls, measurements_root: Path) -> "HeadphoneIndex":
        """
        measurements/{source}/data/{form}/{name}.csv yapısını tarar.
        Aynı (form, name) çiftinden birden fazla kaynak varsa kaynak
        önceliğine göre en güveniliri seçer.
        """
        if not measurements_root.is_dir():
            logger.warning("measurements_root yok: %s", measurements_root)
            return cls([])

        # Geçici: (form, name) -> [HeadphoneEntry, ...]
        # Sonra her grup için en yüksek öncelikli kaynak seçilir
        groups: dict = {}

        # Beklenen yapı: measurements/{source}/data/{form}/{name}.csv
        for source_dir in measurements_root.iterdir():
            if not source_dir.is_dir():
                continue
            data_dir = source_dir / "data"
            if not data_dir.is_dir():
                continue

            for form_dir in data_dir.iterdir():
                if not form_dir.is_dir():
                    continue
                form = form_dir.name

                for csv_path in form_dir.rglob("*.csv"):
                    # csv_path'in form_dir'a göre relative path'ini al,
                    # uzantıyı çıkar — bu kulaklık ismi olur
                    # (bazı kaynaklarda alt klasörler var, onu da hesaba kat)
                    rel = csv_path.relative_to(form_dir)
                    name = str(rel.with_suffix("")).replace("\\", "/")

                    entry = HeadphoneEntry(
                        id=f"{source_dir.name}/{form}/{name}",
                        label=name.split("/")[-1],  # son segment kullanıcıya gösterilen
                        form=form,
                        source=source_dir.name,
                        _tokens=_tokenize(name),
                    )
                    key = (form, name.lower())
                    groups.setdefault(key, []).append(entry)

        # Her grupta en yüksek öncelikli kaynağı tut
        deduped: List[HeadphoneEntry] = []
        for key, candidates in groups.items():
            candidates.sort(key=lambda e: _source_rank(e.source))
            deduped.append(candidates[0])

        # Alfabetik sıralama (varsayılan tutarlılık için)
        deduped.sort(key=lambda e: e.label.lower())

        logger.info(
            "Index kuruldu: %d kulaklık (deduplicate öncesi %d girdi)",
            len(deduped),
            sum(len(v) for v in groups.values()),
        )
        return cls(deduped)

    def __len__(self) -> int:
        return len(self._entries)

    def search(self, query: Optional[str], limit: int = 20) -> List[HeadphoneEntry]:
        """
        Token-based arama. Sorgudaki her token, hedefin token'larından
        birinin prefix'i olmalı.

        Skor (yüksek = iyi):
          - tam token eşleşmesi: +2
          - prefix eşleşmesi:   +1

        Sıralama: önce skor (yüksek), sonra label uzunluğu (kısa), sonra alfabetik.

        Boş sorgu: ilk N kulaklığı döndürür (alfabetik).
        """
        if not query or not query.strip():
            return self._entries[:limit]

        query_tokens = _tokenize(query)
        if not query_tokens:
            return self._entries[:limit]

        scored: List[tuple] = []  # (skor, label_len, label_lower, idx, entry)
        for idx, entry in enumerate(self._entries):
            total_score = 0
            all_matched = True

            for qt in query_tokens:
                best = 0
                for tt in entry._tokens:
                    if tt == qt:
                        best = max(best, 2)
                    elif tt.startswith(qt):
                        best = max(best, 1)
                if best == 0:
                    all_matched = False
                    break
                total_score += best

            if all_matched:
                scored.append((
                    -total_score,         # negatif: büyük skor önce
                    len(entry.label),     # kısa label önce
                    entry.label.lower(),  # alfabetik tiebreaker
                    idx,                  # son tiebreaker — entry'leri karşılaştırmamak için
                    entry,
                ))

        scored.sort()
        return [tup[4] for tup in scored[:limit]]