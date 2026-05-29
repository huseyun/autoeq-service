"""
Endpoint testleri. Sahte measurements klasörüyle çalışır,
gerçek AutoEq pipeline'ı arka planda çalıştırılır.
"""


class TestHealth:
    def test_returns_ok(self, client):
        r = client.get("/health")
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "ok"
        assert body["measurements_exists"] is True


class TestEqualize:
    def test_bass_to_flat_produces_filters(self, client):
        """
        Bass-heavy bir kulaklığı düz hedefe eşitlemek → düşük frekanslarda
        negatif gain'li filtre(ler) üretmeli. Tam değerleri test etmek
        kırılgan olur ama filtre listesinin BOŞ olmaması ve filtre
        özelliklerinin makul aralıkta olması test edilebilir.
        """
        r = client.post(
            "/equalize",
            json={
                "source_id": "oratory1990/over-ear/Test Headphone Bass",
                "target_id": "oratory1990/over-ear/Test Headphone Flat",
            },
        )
        assert r.status_code == 200, r.text
        body = r.json()

        assert body["fs"] == 44100
        assert len(body["filters"]) > 0
        assert "preamp_db" in body
        assert body["preamp_db"] <= 0  # preamp daima negatif veya sıfır

        for filt in body["filters"]:
            assert filt["type"] in {"PEAKING", "LOW_SHELF", "HIGH_SHELF"}
            assert 10 < filt["fc"] < 22000
            assert 0.1 < filt["q"] < 10
            assert -24 < filt["gain"] < 24

    def test_same_source_and_target_returns_empty(self, client):
        r = client.post(
            "/equalize",
            json={
                "source_id": "oratory1990/over-ear/Test Headphone Flat",
                "target_id": "oratory1990/over-ear/Test Headphone Flat",
            },
        )
        assert r.status_code == 200
        body = r.json()
        assert body["filters"] == []
        assert body["preamp_db"] == 0.0

    def test_custom_fs(self, client):
        r = client.post(
            "/equalize",
            json={
                "source_id": "oratory1990/over-ear/Test Headphone Bass",
                "target_id": "oratory1990/over-ear/Test Headphone Treble",
                "fs": 48000,
            },
        )
        assert r.status_code == 200
        assert r.json()["fs"] == 48000


class TestErrors:
    def test_invalid_id_format(self, client):
        r = client.post(
            "/equalize",
            json={"source_id": "invalid", "target_id": "also/bad"},
        )
        assert r.status_code == 400
        assert "format" in r.json()["detail"].lower()

    def test_nonexistent_headphone(self, client):
        r = client.post(
            "/equalize",
            json={
                "source_id": "oratory1990/over-ear/Nonexistent",
                "target_id": "oratory1990/over-ear/Test Headphone Flat",
            },
        )
        assert r.status_code == 404
        assert "bulunamadı" in r.json()["detail"]

    def test_path_traversal_blocked(self, client):
        r = client.post(
            "/equalize",
            json={
                "source_id": "../../../etc/passwd/foo",
                "target_id": "oratory1990/over-ear/Test Headphone Flat",
            },
        )
        # 400 veya 404 — önemli olan 200 dönmemesi
        assert r.status_code in (400, 404)

    def test_missing_required_field(self, client):
        r = client.post("/equalize", json={"source_id": "x/y/z"})
        assert r.status_code == 422  # Pydantic validation

    def test_fs_out_of_range(self, client):
        r = client.post(
            "/equalize",
            json={
                "source_id": "oratory1990/over-ear/Test Headphone Flat",
                "target_id": "oratory1990/over-ear/Test Headphone Bass",
                "fs": 999999,
            },
        )
        assert r.status_code == 422


class TestSearch:
    def test_empty_query_returns_all(self, client):
        r = client.get("/headphones")
        assert r.status_code == 200
        body = r.json()
        assert body["total"] >= 3  # conftest 3 kulaklık oluşturuyor

    def test_search_by_partial_name(self, client):
        r = client.get("/headphones", params={"q": "bass"})
        assert r.status_code == 200
        body = r.json()
        assert body["total"] >= 1
        assert any("Bass" in entry["label"] for entry in body["results"])

    def test_search_case_insensitive(self, client):
        r1 = client.get("/headphones", params={"q": "TREBLE"})
        r2 = client.get("/headphones", params={"q": "treble"})
        assert r1.json()["total"] == r2.json()["total"]

    def test_search_token_based(self, client):
        # "headphone flat" → token'lar her ikisi de "Test Headphone Flat"'te
        # prefix olarak bulunmalı
        r = client.get("/headphones", params={"q": "headphone flat"})
        assert r.status_code == 200
        body = r.json()
        assert body["total"] >= 1
        assert any("Flat" in entry["label"] for entry in body["results"])

    def test_search_no_match(self, client):
        r = client.get("/headphones", params={"q": "xyznonexistent"})
        assert r.status_code == 200
        assert r.json()["total"] == 0

    def test_search_limit(self, client):
        r = client.get("/headphones", params={"limit": 2})
        assert r.status_code == 200
        assert len(r.json()["results"]) <= 2

    def test_search_returns_required_fields(self, client):
        r = client.get("/headphones", params={"q": "test"})
        for entry in r.json()["results"]:
            assert "id" in entry
            assert "label" in entry
            assert "form" in entry
            assert "source" in entry