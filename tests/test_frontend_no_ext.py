"""核心页面不再加载扩展实验区，后端兼容接口不受此测试约束。"""
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_frontend_has_no_ext_section_or_requests():
    html = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
    js = (ROOT / "static" / "app.js").read_text(encoding="utf-8")

    assert 'id="extSec"' not in html
    assert "扩展标的 · 多策略实验区" not in html
    assert "/api/ext/" not in js
    assert "loadExt" not in js
    assert "const EXT" not in js and "let EXT" not in js
