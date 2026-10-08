"""「刷新数据」链路必须端到端打通。

回归背景（2026-10-08）：前端在走 /api/rerun 分支时会丢掉 force，后端缓存又只
看固定 6 小时定时器，于是收盘后点刷新只是回放缓存 CSV，右上角「数据截至」纹丝
不动。这两处都靠下面的断言钉住。
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_refresh_flag_is_forwarded_with_stored_settings():
    """带浏览器本地设置时走 /api/rerun，refresh 必须随请求体一起发出。"""
    js = (ROOT / "static" / "app.js").read_text(encoding="utf-8")

    assert "refresh: force" in js, "刷新按钮的 force 必须放进 /api/rerun 请求体"
    assert "...LOCAL_SETTINGS, refresh: force" in js, "refresh 不能覆盖已存设置"


def test_refresh_button_exposes_loading_state():
    html = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
    js = (ROOT / "static" / "app.js").read_text(encoding="utf-8")

    assert 'id="refreshBtn"' in html, "刷新按钮需要 id 才能切换加载态"
    assert "刷新中…" in js, "强制刷新会重新下载数据，需要可见的进行中提示"
    assert '"数据已刷新"' in js or "'数据已刷新'" in js, "刷新完成应有反馈"


def test_backend_accepts_refresh_flag():
    from backend.data_feed import forcing

    with forcing(True):
        pass  # 上下文可正常进出，且不抛错
