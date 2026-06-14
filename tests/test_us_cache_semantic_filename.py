"""Regression tests for semantic filenames on US cache hits."""

from __future__ import annotations

from pathlib import Path

import pytest

from unified_downloader.core.downloader import UnifiedDownloader
from unified_downloader.models.enums import Market


def test_us_cache_hit_restores_semantic_download_filename(tmp_path, monkeypatch):
    """A cached US filing must not be returned as data/cache/.../<md5>.pdf.

    IMA uses basename(file_path) as searchable file name. Returning cache hash paths
    makes uploaded US annual/quarterly filings impossible to identify.
    """
    monkeypatch.chdir(tmp_path)
    downloader = UnifiedDownloader()

    monkeypatch.setattr(
        downloader._adapters[Market.M],
        "_get_annual_form_type",
        lambda code: "10-K",
    )

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

    monkeypatch.setattr(
        downloader._adapters[Market.M],
        "_get_annual_form_type",
        lambda code: "10-K",
    )

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


def test_us_annual_report_cache_hit_uses_fpi_actual_form(tmp_path, monkeypatch):
    """US annual_report cache hits must preserve FPI 20-F semantic names."""
    monkeypatch.chdir(tmp_path)
    downloader = UnifiedDownloader()
    monkeypatch.setattr(
        downloader._adapters[Market.M],
        "_get_annual_form_type",
        lambda code: "20-F",
    )

    source = tmp_path / "PDD_2024_20F.pdf"
    source.write_bytes(b"%PDF-1.4\nfpi annual cache fixture\n" + b"x" * 128)
    downloader._cache_manager.put("m", "PDD", 2024, "annual_report", source)
    source.unlink()

    result = downloader.download("PDD", 2024, "annual_report", market=Market.M)

    assert result.success is True
    assert result.cached is True
    assert result.file_path == str(Path("downloads/m/PDD/PDD_2024_20F.pdf"))


def test_us_quarterly_cache_hit_uses_fpi_actual_form(tmp_path, monkeypatch):
    """US quarterly/10q cache hits must preserve FPI 6-K semantic names."""
    monkeypatch.chdir(tmp_path)
    downloader = UnifiedDownloader()
    monkeypatch.setattr(
        downloader._adapters[Market.M],
        "_get_quarterly_form_type",
        lambda code: "6-K",
    )

    source = tmp_path / "PDD_2024_6K.pdf"
    source.write_bytes(b"%PDF-1.4\nfpi quarterly cache fixture\n" + b"x" * 128)
    downloader._cache_manager.put("m", "PDD", 2024, "10q", source)
    source.unlink()

    result = downloader.download("PDD", 2024, "10q", market=Market.M)

    assert result.success is True
    assert result.cached is True
    assert result.file_path == str(Path("downloads/m/PDD/PDD_2024_6K.pdf"))


def test_us_cache_hit_exposes_original_cache_path_metadata(tmp_path, monkeypatch):
    """Cache-hit callers can inspect the original hash cache path when needed."""
    monkeypatch.chdir(tmp_path)
    downloader = UnifiedDownloader()
    monkeypatch.setattr(
        downloader._adapters[Market.M],
        "_get_annual_form_type",
        lambda code: "10-K",
    )

    source = tmp_path / "AAPL_2024_10K.pdf"
    source.write_bytes(b"%PDF-1.4\nmetadata fixture\n" + b"x" * 128)
    downloader._cache_manager.put("m", "AAPL", 2024, "10k", source)
    source.unlink()

    result = downloader.download("AAPL", 2024, "10k", market=Market.M)

    assert result.metadata["cache_path"].startswith("data/cache/m/AAP/")
    assert Path(result.metadata["cache_path"]).exists()


def test_us_cache_hit_does_not_recopied_existing_semantic_file(tmp_path, monkeypatch):
    """Repeated cache hits should not rewrite an unchanged semantic target."""
    monkeypatch.chdir(tmp_path)
    downloader = UnifiedDownloader()
    monkeypatch.setattr(
        downloader._adapters[Market.M],
        "_get_annual_form_type",
        lambda code: "10-K",
    )

    source = tmp_path / "AAPL_2024_10K.pdf"
    source.write_bytes(b"%PDF-1.4\ncopy fixture\n" + b"x" * 128)
    downloader._cache_manager.put("m", "AAPL", 2024, "10k", source)
    source.unlink()

    first = downloader.download("AAPL", 2024, "10k", market=Market.M)
    target = Path(first.file_path)
    first_mtime = target.stat().st_mtime_ns

    def fail_copy(*args, **kwargs):  # pragma: no cover - called only on regression
        raise AssertionError("copy2 should not run when semantic target is already current")

    monkeypatch.setattr("unified_downloader.core.downloader.shutil.copy2", fail_copy)
    second = downloader.download("AAPL", 2024, "10k", market=Market.M)

    assert second.file_path == first.file_path
    assert target.stat().st_mtime_ns == first_mtime


@pytest.mark.asyncio
async def test_async_us_cache_hit_restores_semantic_filename(tmp_path, monkeypatch):
    """Async downloader cache hits should match sync semantic filename behavior."""
    monkeypatch.chdir(tmp_path)
    from unified_downloader.core.async_downloader import AsyncUnifiedDownloader

    downloader = AsyncUnifiedDownloader()
    monkeypatch.setattr(
        downloader._downloader._adapters[Market.M],
        "_get_annual_form_type",
        lambda code: "10-K",
    )

    source = tmp_path / "AAPL_2024_10K.pdf"
    source.write_bytes(b"%PDF-1.4\nasync cache fixture\n" + b"x" * 128)
    downloader._downloader._cache_manager.put("m", "AAPL", 2024, "10k", source)
    source.unlink()

    result = await downloader.download("AAPL", 2024, "10k", market=Market.M)

    assert result.success is True
    assert result.cached is True
    assert result.file_path == str(Path("downloads/m/AAP/AAPL_2024_10K.pdf"))
    assert result.metadata["cache_path"].startswith("data/cache/m/AAP/")
    await downloader.close()
