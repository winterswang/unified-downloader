"""Regression tests for semantic filenames on US cache hits."""

from __future__ import annotations

from pathlib import Path

from unified_downloader.core.downloader import UnifiedDownloader
from unified_downloader.models.enums import Market


def test_us_cache_hit_restores_semantic_download_filename(tmp_path, monkeypatch):
    """A cached US filing must not be returned as data/cache/.../<md5>.pdf.

    IMA uses basename(file_path) as searchable file name. Returning cache hash paths
    makes uploaded US annual/quarterly filings impossible to identify.
    """
    monkeypatch.chdir(tmp_path)
    downloader = UnifiedDownloader()

    source = tmp_path / "AAPL_2024_10K.pdf"
    source.write_bytes(b"%PDF-1.4\nsemantic cache fixture\n" + b"x" * 128)
    downloader._cache_manager.put("m", "AAPL", 2024, "10k", source)
    source.unlink()

    result = downloader.download("AAPL", 2024, "10k", market=Market.M)

    assert result.success is True
    assert result.cached is True
    assert result.file_path == str(Path("downloads/m/AAP/AAPL_2024_10K.pdf"))
    assert Path(result.file_path).exists()
    assert Path(result.file_path).read_bytes().startswith(b"%PDF-1.4")
    assert not Path(result.file_path).name.startswith("9")  # not a hash-like cache key


def test_us_cache_hit_with_pdf_request_ignores_cached_html(tmp_path, monkeypatch):
    """--pdf should not return a cached HTML file for US filings."""
    monkeypatch.chdir(tmp_path)
    downloader = UnifiedDownloader()

    source = tmp_path / "AAPL_2024_10K.html"
    source.write_text("<html><body>cached html</body></html>", encoding="utf-8")
    downloader._cache_manager.put("m", "AAPL", 2024, "10k", source)
    source.unlink()

    calls = []

    def fake_download(**kwargs):
        calls.append(kwargs)
        out = Path("downloads/m/AAP/AAPL_2024_10K.pdf")
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(b"%PDF-1.4 generated\n" + b"x" * 128)
        from unified_downloader.models.entities import DownloadResult
        return DownloadResult(success=True, file_path=str(out), file_size=out.stat().st_size)

    monkeypatch.setattr(downloader._adapters[Market.M], "download", fake_download)

    result = downloader.download("AAPL", 2024, "10k", market=Market.M, convert_to_pdf=True)

    assert calls, "cached HTML must not short-circuit a US --pdf download"
    assert result.success is True
    assert result.file_path == "downloads/m/AAP/AAPL_2024_10K.pdf"
    assert Path(result.file_path).suffix.lower() == ".pdf"
