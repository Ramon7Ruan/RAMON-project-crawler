"""curl 后端测试。

**不真的调用 curl**——用 monkeypatch 替换 `subprocess.run`，
这样测试不需要网络、也不需要机器上装了 curl。

其中 `test_command_carries_http_code_placeholder` 是为一个真实踩过的 bug 写的：
最初 `--write-out` 只传了哨兵而漏了 `%{http_code}`，结果是每个响应都被判成
"状态码无法解析"、三个源全部报坏——而单元测试当时还全绿（因为没测这条路径）。
"""

from __future__ import annotations

import subprocess

import pytest

from beacon.core.curl_transport import STATUS_MARKER, CurlTransport
from beacon.core.fetch import FetchError

URL = "https://example.test/data"


def fake_run_factory(stdout: bytes, returncode: int = 0, stderr: bytes = b""):
    def _fake(cmd, **kwargs):  # noqa: ANN001, ANN003
        _fake.cmd = cmd  # type: ignore[attr-defined]
        _fake.kwargs = kwargs  # type: ignore[attr-defined]
        return subprocess.CompletedProcess(args=cmd, returncode=returncode, stdout=stdout, stderr=stderr)

    return _fake


class TestParsing:
    def test_parses_status_and_body(self, monkeypatch) -> None:
        fake = fake_run_factory(f'{{"ok":true}}{STATUS_MARKER}200'.encode())
        monkeypatch.setattr(subprocess, "run", fake)
        status, body = CurlTransport().get(URL, {}, 10)
        assert status == 200
        assert body == b'{"ok":true}'

    def test_body_with_trailing_newline_is_preserved(self, monkeypatch) -> None:
        """响应体本身以换行结尾时，不能用"最后一个换行"来切状态码。"""
        raw = f"line1\nline2\n{STATUS_MARKER}200".encode()
        monkeypatch.setattr(subprocess, "run", fake_run_factory(raw))
        status, body = CurlTransport().get(URL, {}, 10)
        assert status == 200
        assert body == b"line1\nline2\n"

    def test_missing_marker_raises(self, monkeypatch) -> None:
        monkeypatch.setattr(subprocess, "run", fake_run_factory(b"no marker here"))
        with pytest.raises(FetchError, match="没有状态码标记"):
            CurlTransport().get(URL, {}, 10)

    def test_unparsable_status_raises(self, monkeypatch) -> None:
        monkeypatch.setattr(subprocess, "run", fake_run_factory(f"x{STATUS_MARKER}abc".encode()))
        with pytest.raises(FetchError, match="无法解析"):
            CurlTransport().get(URL, {}, 10)


class TestCommandConstruction:
    def test_command_carries_http_code_placeholder(self, monkeypatch) -> None:
        """回归：`--write-out` 必须同时带上哨兵与 `%{http_code}`。

        漏掉占位符时 curl 只输出哨兵、后面什么都没有，于是**每个**响应都被判成
        "状态码无法解析"。这个 bug 会让 curl 后端在真实环境里完全不可用，
        而只看单测时却是绿的（因为当时没覆盖这条路径）。
        """
        fake = fake_run_factory(f"x{STATUS_MARKER}200".encode())
        monkeypatch.setattr(subprocess, "run", fake)
        CurlTransport().get(URL, {}, 10)

        write_out = fake.cmd[fake.cmd.index("--write-out") + 1]  # type: ignore[attr-defined]
        assert "%{http_code}" in write_out, "缺少 %{http_code}，状态码永远解析不出来"
        assert STATUS_MARKER in write_out

    def test_headers_are_forwarded(self, monkeypatch) -> None:
        fake = fake_run_factory(f"x{STATUS_MARKER}200".encode())
        monkeypatch.setattr(subprocess, "run", fake)
        CurlTransport().get(URL, {"Referer": "https://data.eastmoney.com/"}, 10)

        cmd = fake.cmd  # type: ignore[attr-defined]
        assert "Referer: https://data.eastmoney.com/" in cmd

    def test_empty_header_value_is_not_sent(self, monkeypatch) -> None:
        """空值 = 不发送该 header。

        这条是真实踩出来的：某站点带浏览器 UA 会直接失败，必须能表达"不发送"。
        """
        fake = fake_run_factory(f"x{STATUS_MARKER}200".encode())
        monkeypatch.setattr(subprocess, "run", fake)
        CurlTransport().get(URL, {"User-Agent": "", "Accept": "", "Referer": "https://r/"}, 10)

        cmd = fake.cmd  # type: ignore[attr-defined]
        joined = " ".join(cmd)
        assert "User-Agent" not in joined
        assert "Accept:" not in joined
        assert "Referer: https://r/" in joined, "非空的 header 必须照常发送"

    def test_timeout_passed_to_curl_and_process(self, monkeypatch) -> None:
        fake = fake_run_factory(f"x{STATUS_MARKER}200".encode())
        monkeypatch.setattr(subprocess, "run", fake)
        CurlTransport().get(URL, {}, 12)

        cmd = fake.cmd  # type: ignore[attr-defined]
        assert cmd[cmd.index("--max-time") + 1] == "12"
        assert fake.kwargs["timeout"] > 12, "进程超时应略长于 curl 自身超时，否则杀不到尾"  # type: ignore[attr-defined]

    def test_uses_list_args_not_shell(self, monkeypatch) -> None:
        """必须用列表传参（不经过 shell），否则 URL 里的 & 会被解释成后台执行。"""
        fake = fake_run_factory(f"x{STATUS_MARKER}200".encode())
        monkeypatch.setattr(subprocess, "run", fake)
        CurlTransport().get(URL + "?a=1&b=2", {}, 10)
        assert fake.kwargs.get("shell") is not True  # type: ignore[attr-defined]


class TestProxySemantics:
    """代理语义必须与 `HttpxTransport` 一致。

    曾经不一致过：CLI 上有 `--no-proxy`，httpx 端生效而 curl 端完全没实现，
    于是同一份配置下"httpx 直连被拒、curl 却仍走代理拿到 502"，
    `--no-proxy` 成了一句话空话。这类差异会让你在"换个后端试试"时越调越糊涂。
    """

    def test_default_follows_environment(self, monkeypatch) -> None:
        """默认跟随环境变量——不加任何代理参数，把决定权交给 curl 与系统。"""
        fake = fake_run_factory(f"x{STATUS_MARKER}200".encode())
        monkeypatch.setattr(subprocess, "run", fake)
        CurlTransport().get(URL, {}, 10)

        cmd = fake.cmd  # type: ignore[attr-defined]
        assert "--proxy" not in cmd
        assert "--noproxy" not in cmd

    def test_trust_env_false_disables_proxy(self, monkeypatch) -> None:
        fake = fake_run_factory(f"x{STATUS_MARKER}200".encode())
        monkeypatch.setattr(subprocess, "run", fake)
        CurlTransport(trust_env=False).get(URL, {}, 10)

        cmd = fake.cmd  # type: ignore[attr-defined]
        assert cmd[cmd.index("--noproxy") + 1] == "*"

    def test_explicit_proxy_is_passed(self, monkeypatch) -> None:
        fake = fake_run_factory(f"x{STATUS_MARKER}200".encode())
        monkeypatch.setattr(subprocess, "run", fake)
        CurlTransport(proxy="http://127.0.0.1:7890").get(URL, {}, 10)

        cmd = fake.cmd  # type: ignore[attr-defined]
        assert cmd[cmd.index("--proxy") + 1] == "http://127.0.0.1:7890"
        assert "--noproxy" not in cmd, "显式代理优先，不应同时再关代理"

    def test_make_transport_forwards_proxy_settings(self) -> None:
        """工厂必须把代理配置转交给后端——否则 CLI 的参数在中途就丢了。"""
        from beacon.core.fetch import make_transport

        t = make_transport("curl", proxy="http://p:1", trust_env=False)
        assert t._proxy_args() == ["--proxy", "http://p:1"]

        t2 = make_transport("curl", trust_env=False)
        assert t2._proxy_args() == ["--noproxy", "*"]


class TestFailure:
    def test_nonzero_exit_raises_with_stderr(self, monkeypatch) -> None:
        monkeypatch.setattr(
            subprocess, "run", fake_run_factory(b"", returncode=28, stderr=b"Operation timed out")
        )
        with pytest.raises(FetchError, match="curl 退出码 28"):
            CurlTransport().get(URL, {}, 10)

    def test_missing_binary_raises(self, monkeypatch) -> None:
        def boom(cmd, **kwargs):  # noqa: ANN001, ANN003
            raise FileNotFoundError("curl")

        monkeypatch.setattr(subprocess, "run", boom)
        with pytest.raises(FetchError, match="找不到 curl"):
            CurlTransport().get(URL, {}, 10)

    def test_process_timeout_raises(self, monkeypatch) -> None:
        def boom(cmd, **kwargs):  # noqa: ANN001, ANN003
            raise subprocess.TimeoutExpired(cmd, 10)

        monkeypatch.setattr(subprocess, "run", boom)
        with pytest.raises(FetchError, match="curl 进程超时"):
            CurlTransport().get(URL, {}, 10)
