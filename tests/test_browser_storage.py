"""浏览器本地存储与云端无状态契约静态回归。"""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_browser_store_uses_indexeddb_and_non_exportable_aes_key():
    js = (ROOT / "static" / "browser_store.js").read_text(encoding="utf-8")
    assert "indexedDB.open" in js
    assert 'name: "AES-GCM"' in js
    assert "false," in js, "生成密钥必须设置 extractable=false"
    assert "localStorage" not in js
    assert "sessionStorage" not in js


def test_frontend_uses_browser_store_instead_of_server_persistence_endpoints():
    js = (ROOT / "static" / "app.js").read_text(encoding="utf-8")
    assert 'BrowserStore.set("holdings"' in js
    assert 'BrowserStore.set("strategy-settings"' in js
    assert "BrowserStore.saveProfiles" in js
    assert 'fetch("/api/holdings"' not in js
    assert 'fetch("/api/ai/config"' not in js
    assert 'fetch("/api/ai/history"' not in js


def test_docker_image_contains_browser_store_but_excludes_personal_files():
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    ignore = (ROOT / ".dockerignore").read_text(encoding="utf-8")
    assert "COPY static ./static" in dockerfile
    for name in ("holdings.json", "strategy_settings.json", "data_cache"):
        assert name in ignore
