"""可选的 curl 传输后端。

为什么需要它
------------
实测（2026-09-23，Ramon 本机 + 沙箱网络）：`fred.stlouisfed.org`（CloudFront）
**对 Python HTTP 栈的请求一律不响应**——httpx 的默认配置、显式代理、绕过代理、
`verify=False`、`Connection: close` 全部 ReadTimeout；而同一 URL 用 `curl` 取只要 1.9 秒。
同一网络下 httpx 访问 jsdelivr / 东方财富 / 中证指数都正常，所以既不是网络不通，
也不是 httpx 坏了，而是**那个站点与 Python 客户端之间的组合问题**（TLS 指纹、
加密套件协商、或 CDN 侧的客户端过滤）。

这不是我们代码的错，但工具需要一条能走通的路，所以提供这个后端。

为什么默认不用它
----------------
1. 纯 Python 是首选：无外部依赖、行为可预测、测试里能注入桩。
2. 依赖 `curl` 存在，并会把参数拼进命令——虽然做了严格转义，但少一个执行外部进程的
   路径总是更安全。

因此它是**显式选择**（`--transport curl`），不是默认值。这也让
`tests/test_independence.py` 可以断言"默认路径不依赖任何外部命令"。

代理：必须自己处理
------------------
curl 默认会读 `http_proxy` / `HTTPS_PROXY` 等环境变量，所以"跟随系统"这一侧是免费的；
但**"忽略代理"必须显式传 `--noproxy '*'`**，否则命令行上的 `--no-proxy`
对这个后端就是一句空话（曾真的如此：同一份配置下 httpx 直连被拒、curl 却仍走代理拿到 502）。
两个后端的代理语义必须一致，否则"换个后端试试"会变成调试陷阱。
"""

from __future__ import annotations

import subprocess

from .fetch import FetchError

STATUS_MARKER = "\n__STATUS__:"
"""用哨兵而不是"最后一个换行"来分离状态码：响应体本身可能以换行结尾。

⚠️ 它必须与 `%{http_code}` 拼在一起使用——只写哨兵而不带占位符，
curl 会把哨兵原样输出、后面什么都不跟，于是所有响应都被判成"状态码无法解析"。
（这个 bug 真踩过一次，已在 tests/test_curl_transport.py 里钉住。）
"""


class CurlTransport:
    """调用系统 curl 取数。接口与 `HttpxTransport` 完全一致。"""

    def __init__(
        self,
        binary: str = "curl",
        *,
        proxy: str | None = None,
        trust_env: bool = True,
    ) -> None:
        self.binary = binary
        self.proxy = proxy
        self.trust_env = trust_env

    def _proxy_args(self) -> list[str]:
        """代理参数，语义与 `HttpxTransport` 对齐。

        * `proxy` 显式给出 → `--proxy`，覆盖环境变量
        * `trust_env=False` → `--noproxy '*'`，彻底不走代理
        * 两者都给 → 以 `proxy` 为准（显式优于隐式）
        """
        if self.proxy:
            return ["--proxy", self.proxy]
        if not self.trust_env:
            # `*` 是 curl 的通配写法，意为"所有主机都别走代理"
            return ["--noproxy", "*"]
        return []

    def get(self, url: str, headers: dict[str, str], timeout: float) -> tuple[int, bytes]:
        cmd = [
            self.binary,
            "--silent",
            "--show-error",
            "--location",
            "--max-time",
            str(int(timeout)),
            "--write-out",
            STATUS_MARKER + "%{http_code}",  # 哨兵 + 实际状态码，缺一不可
            *self._proxy_args(),
        ]
        for key, value in headers.items():
            # 空值 = **不发送这个 header**，让 curl 用它的默认值。
            # 这不是洁癖：实测该域带上一个浏览器 UA 会立刻失败（HTTP/2 stream 报错或超时），
            # 而不带 UA 用 curl 默认值时 2.4 秒稳定成功。所以"不发送"是必须能表达的状态。
            if not value:
                continue
            # 用列表传参而非 shell，参数不会被解释；这里再做一次保险的换行剔除
            cmd += ["-H", f"{key}: {value.replace(chr(10), ' ')}"]
        cmd.append(url)

        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                timeout=timeout + 5,  # 留出进程启动开销
                check=False,
            )
        except FileNotFoundError as exc:
            raise FetchError(url, f"找不到 curl 可执行文件：{self.binary}") from exc
        except subprocess.TimeoutExpired as exc:
            raise FetchError(url, f"curl 进程超时（{timeout}s）") from exc

        if proc.returncode != 0:
            detail = proc.stderr.decode("utf-8", "replace").strip()[:200]
            raise FetchError(url, f"curl 退出码 {proc.returncode}：{detail}")

        out = proc.stdout
        marker = STATUS_MARKER.encode("utf-8")
        idx = out.rfind(marker)
        if idx < 0:
            raise FetchError(url, "curl 输出里没有状态码标记（--write-out 未生效？）")

        body = out[:idx]
        raw_status = out[idx + len(marker) :].strip()
        try:
            status = int(raw_status)
        except ValueError as exc:
            raise FetchError(url, f"curl 返回的状态码无法解析：{raw_status!r}") from exc
        return status, body
