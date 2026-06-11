from __future__ import annotations

import logging


class _Response:
    def __init__(self, status_code: int = 200):
        self.status_code = status_code

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


def test_pushover_posts_minimal_documented_payload(data_tmp, monkeypatch):
    cfg = data_tmp / "config.toml"
    cfg.write_text(
        '[notify]\nbackend = "pushover"\npushover_token = "app-token"\npushover_user = "user-key"\n',
        encoding="utf-8",
    )
    from mastisk.settings import reload_settings

    reload_settings()

    calls: list[dict] = []

    def fake_post(url, *, data=None, timeout=None, **kwargs):
        calls.append({"url": url, "data": data, "timeout": timeout, "kwargs": kwargs})
        return _Response()

    monkeypatch.setattr("mastisk.notify.httpx.post", fake_post)

    from mastisk.notify import send

    assert send("Task due", "Call Sam", url="mastisk://tasks/abc123") is True
    assert calls == [
        {
            "url": "https://api.pushover.net/1/messages.json",
            "data": {
                "token": "app-token",
                "user": "user-key",
                "title": "Task due",
                "message": "Call Sam",
                "url": "mastisk://tasks/abc123",
            },
            "timeout": 10,
            "kwargs": {},
        }
    ]


def test_ntfy_posts_to_topic_with_title_header(data_tmp, monkeypatch):
    cfg = data_tmp / "config.toml"
    cfg.write_text(
        '[notify]\nbackend = "ntfy"\nntfy_topic = "mastisk-test"\nntfy_server = "https://ntfy.example/"\n',
        encoding="utf-8",
    )
    from mastisk.settings import reload_settings

    reload_settings()

    calls: list[dict] = []

    def fake_post(url, *, content=None, headers=None, timeout=None, **kwargs):
        calls.append(
            {"url": url, "content": content, "headers": headers, "timeout": timeout, "kwargs": kwargs}
        )
        return _Response()

    monkeypatch.setattr("mastisk.notify.httpx.post", fake_post)

    from mastisk.notify import send

    assert send("Daily summary", "2 due today", url="https://mastisk.local/reminders/1") is True
    assert calls == [
        {
            "url": "https://ntfy.example/mastisk-test",
            "content": b"2 due today",
            "headers": {
                "Title": "Daily summary",
                "Click": "https://mastisk.local/reminders/1",
            },
            "timeout": 10,
            "kwargs": {},
        }
    ]


def test_pushover_truncates_message_before_post(data_tmp, monkeypatch):
    cfg = data_tmp / "config.toml"
    cfg.write_text(
        '[notify]\nbackend = "pushover"\npushover_token = "app-token"\npushover_user = "user-key"\n',
        encoding="utf-8",
    )
    from mastisk.settings import reload_settings

    reload_settings()
    calls: list[dict] = []

    def fake_post(url, *, data=None, timeout=None, **kwargs):
        calls.append({"data": data})
        return _Response()

    monkeypatch.setattr("mastisk.notify.httpx.post", fake_post)

    from mastisk.notify import send

    assert send("Daily summary", "x" * 1030) is True
    assert calls[0]["data"]["message"] == "x" * 1024


def test_ntfy_truncates_message_before_post(data_tmp, monkeypatch):
    cfg = data_tmp / "config.toml"
    cfg.write_text(
        '[notify]\nbackend = "ntfy"\nntfy_topic = "mastisk-test"\n',
        encoding="utf-8",
    )
    from mastisk.settings import reload_settings

    reload_settings()
    calls: list[dict] = []

    def fake_post(url, *, content=None, headers=None, timeout=None, **kwargs):
        calls.append({"content": content})
        return _Response()

    monkeypatch.setattr("mastisk.notify.httpx.post", fake_post)

    from mastisk.notify import send

    assert send("Daily summary", "x" * 5000) is True
    assert calls[0]["content"] == b"x" * 4096


def test_ntfy_encodes_non_ascii_title_header(data_tmp, monkeypatch):
    cfg = data_tmp / "config.toml"
    cfg.write_text(
        '[notify]\nbackend = "ntfy"\nntfy_topic = "mastisk-test"\n',
        encoding="utf-8",
    )
    from mastisk.settings import reload_settings

    reload_settings()
    headers_seen: list[dict] = []

    def fake_post(url, *, content=None, headers=None, timeout=None, **kwargs):
        headers_seen.append(headers or {})
        return _Response()

    monkeypatch.setattr("mastisk.notify.httpx.post", fake_post)

    from mastisk.notify import send

    assert send("Daily 🚀", "2 due today") is True
    assert headers_seen == [{"Title": "=?utf-8?b?RGFpbHkg8J+agA==?="}]


def test_none_backend_returns_false_and_logs(data_tmp, caplog):
    cfg = data_tmp / "config.toml"
    cfg.write_text('[notify]\nbackend = "none"\n', encoding="utf-8")
    from mastisk.settings import reload_settings

    reload_settings()

    from mastisk.notify import send

    with caplog.at_level(logging.INFO):
        assert send("Task due", "Call Sam") is False

    assert "notify backend disabled" in caplog.text
