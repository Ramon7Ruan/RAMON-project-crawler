"""抓取纪律层（PRD §3.4 的六条纪律）。

这一层只做一件事：**把"从网上取一个 URL"变成一件可预测、可留痕、可测试的事**。

六条纪律的落点
--------------
| 纪律 | 落在这里的什么 |
|---|---|
| 优先 API，其次 RSS，最后才解析 HTML | 由 sources 层决定；本层不关心内容格式 |
| 本地缓存（按 URL + 日期） | `_cache_path()` —— 同一天同一 URL 只打一次网络 |
| 限速 + 退避重试 | `_throttle()` / 退避等待 |
| 显式超时 | `timeout_seconds`，逐源可配 |
| **失败时不产出候选，而不是产出空内容** | 失败一律 `raise FetchError`，**绝不返回空** |
| 每次抓取记录 时间/URL/状态 | `FetchResult` + `fetch-log.jsonl` |

为什么"失败抛异常"这么重要
--------------------------
如果这里返回 `None` 或空字符串，上游很容易把它当成"这个指标这次没有值"，
继续往下走，最后在某个环节变成 `0` 或缺失——而**空的数值比没有数值危险得多**，
它会被当成"数据就是 0"。所以本层的契约是：要么给你一份成功的结果，要么抛异常。
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Callable, Protocol
from urllib.parse import urlparse

DEFAULT_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0 Safari/537.36"
)


class FetchError(RuntimeError):
    """抓取失败。**调用方必须让它继续往上抛，不得吞掉。**"""

    def __init__(self, url: str, reason: str, status: int | None = None) -> None:
        self.url = url
        self.reason = reason
        self.status = status
        super().__init__(f"抓取失败 {url}：{reason}" + (f"（HTTP {status}）" if status else ""))


class Transport(Protocol):
    """HTTP 传输的抽象。存在的唯一理由是**让测试可以注入、不打网络**（NF-C9）。"""

    def get(self, url: str, headers: dict[str, str], timeout: float) -> tuple[int, bytes]:
        """返回 (status_code, body)。网络层错误应抛 FetchError。"""
        ...


class HttpxTransport:
    """默认传输实现。

    代理行为的默认是**跟随系统**（`trust_env=True`，读 `HTTPS_PROXY` 等环境变量）——
    用户机器上可能有公司代理或本地代理，忽略它会让工具在那些环境里直接不可用。
    但两个方向都要能覆盖：

    * `proxy="http://..."` —— 显式指定（系统没配但有可用代理时）
    * `trust_env=False` —— 绕过环境里的代理直连

    之所以把这些做成参数而不是写死：实测同一台机器上，走代理与直连的可达性完全不同
    （沙箱环境下 `curl` 走本地代理可通 FRED，而 Python 走同一代理会读超时），
    这类差异只能靠参数让调用方按环境决定。
    """

    def __init__(self, *, proxy: str | None = None, trust_env: bool = True) -> None:
        self.proxy = proxy
        self.trust_env = trust_env

    def get(self, url: str, headers: dict[str, str], timeout: float) -> tuple[int, bytes]:
        import httpx

        kwargs: dict[str, object] = {"timeout": timeout, "follow_redirects": True}
        if self.proxy:
            kwargs["proxy"] = self.proxy
        if not self.trust_env:
            kwargs["trust_env"] = False
        try:
            with httpx.Client(**kwargs) as client:  # type: ignore[arg-type]
                resp = client.get(url, headers=headers)
                return resp.status_code, resp.content
        except Exception as exc:  # noqa: BLE001 —— 网络异常种类繁多，统一收敛成 FetchError
            raise FetchError(url, f"{type(exc).__name__}: {exc}") from exc


@dataclass(frozen=True)
class FetchResult:
    """一次成功抓取的全部留痕。"""

    url: str
    status: int
    content: bytes
    fetched_at: datetime
    from_cache: bool
    sha256: str

    @property
    def text(self) -> str:
        return self.content.decode("utf-8", "replace")


def make_transport(
    name: str = "httpx",
    *,
    proxy: str | None = None,
    trust_env: bool = True,
) -> Transport:
    """按名字构造传输后端。

    `curl` 是**显式的备用选项**，默认永远用纯 Python 的 httpx——
    原因见 `curl_transport.py` 的模块说明（简言之：某个站点挑客户端，
    但默认路径不该因此依赖外部命令）。
    """
    if name == "httpx":
        return HttpxTransport(proxy=proxy, trust_env=trust_env)
    if name == "curl":
        from .curl_transport import CurlTransport

        # 代理语义两个后端必须一致，否则"换个后端试试"会变成调试陷阱。
        return CurlTransport(proxy=proxy, trust_env=trust_env)
    raise ValueError(f"未知的传输后端：{name}（可选 httpx / curl）")


class Fetcher:
    """带缓存、限速、退避、超时与留痕的取数器。"""

    def __init__(
        self,
        cache_dir: Path,
        *,
        transport: Transport | None = None,
        transport_name: str = "httpx",
        timeout: float = 30.0,
        min_interval: float = 1.0,
        max_retries: int = 3,
        cache_ttl_hours: float = 12.0,
        user_agent: str = DEFAULT_UA,
        now: Callable[[], datetime] | None = None,
        sleeper: Callable[[float], None] | None = None,
        proxy: str | None = None,
        trust_env: bool = True,
    ) -> None:
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self._default_transport_name = transport_name
        self._proxy = proxy
        self._trust_env = trust_env
        # 后端按需构造并缓存。默认只会有 httpx 一个；
        # 若某个源在配置里声明了别的后端（如 fred 声明 curl），才会多出一个。
        self._transports: dict[str, Transport] = {
            transport_name: transport
            if transport is not None
            else make_transport(transport_name, proxy=proxy, trust_env=trust_env)
        }
        self.timeout = timeout
        self.min_interval = min_interval
        self.max_retries = max_retries
        self.cache_ttl = timedelta(hours=cache_ttl_hours)
        self.user_agent = user_agent
        self._now = now or (lambda: datetime.now(UTC))
        self._sleep = sleeper or time.sleep
        self._last_hit: dict[str, float] = {}
        self.log_path = self.cache_dir / "fetch-log.jsonl"

    # ---------------------------------------------------------------- 公开接口

    @property
    def transport(self) -> Transport:
        """默认后端。保留这个属性是为了让调用方与测试有一个稳定的默认入口。"""
        return self._transport_for(self._default_transport_name)

    def _transport_for(self, name: str) -> Transport:
        if name not in self._transports:
            self._transports[name] = make_transport(
                name, proxy=self._proxy, trust_env=self._trust_env
            )
        return self._transports[name]

    def get(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        timeout: float | None = None,
        use_cache: bool = True,
        transport_name: str | None = None,
    ) -> FetchResult:
        if use_cache:
            cached = self._read_cache(url)
            if cached is not None:
                self._log(url, 200, from_cache=True, note="cache-hit")
                return cached

        body, status = self._request_with_retry(
            url, headers or {}, timeout or self.timeout, transport_name=transport_name
        )
        result = FetchResult(
            url=url,
            status=status,
            content=body,
            fetched_at=self._now(),
            from_cache=False,
            sha256=hashlib.sha256(body).hexdigest(),
        )
        self._write_cache(url, result)
        self._log(url, status, from_cache=False, note="network")
        return result

    def health(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        transport_name: str | None = None,
    ) -> tuple[bool, str]:
        """探活：只看能不能拿到 2xx，不写缓存、不解析内容。"""
        try:
            _, status = self._request_with_retry(
                url, headers or {}, self.timeout, is_health=True, transport_name=transport_name
            )
        except FetchError as exc:
            return False, exc.reason
        return True, f"HTTP {status}"

    # ---------------------------------------------------------------- 内部实现

    def _request_with_retry(
        self,
        url: str,
        headers: dict[str, str],
        timeout: float,
        *,
        is_health: bool = False,
        transport_name: str | None = None,
    ) -> tuple[bytes, int]:
        merged = {"User-Agent": self.user_agent, "Accept": "*/*"} | headers
        transport = self._transport_for(transport_name or self._default_transport_name)
        last: FetchError | None = None

        for attempt in range(self.max_retries):
            self._throttle(url)
            try:
                status, body = transport.get(url, merged, timeout)
            except FetchError as exc:
                last = exc
            else:
                if 200 <= status < 300:
                    return body, status
                # 4xx 不重试（重试也不会变好，只会更像攻击）；5xx 才退避重试
                # 状态码写进 reason 而不只放进 status 字段：reason 是唯一会一路
                # 传到 CLI 与 changes.md 的字符串，诊断时"403 还是 502"是决定性的。
                last = FetchError(url, f"非 2xx 响应（HTTP {status}）", status)
                if status < 500:
                    break
            if attempt < self.max_retries - 1:
                self._sleep(0.5 * (2**attempt))

        assert last is not None
        self._log(url, last.status or 0, from_cache=False, note=f"error:{last.reason}")
        raise last

    def _throttle(self, url: str) -> None:
        """同域名最小请求间隔。被拉黑的爬虫等于没有爬虫。"""
        host = urlparse(url).netloc
        now = time.monotonic()
        last = self._last_hit.get(host)
        if last is not None:
            wait = self.min_interval - (now - last)
            if wait > 0:
                self._sleep(wait)
        self._last_hit[host] = time.monotonic()

    def _cache_path(self, url: str) -> Path:
        """按 URL + 当天日期做键——同一天同一 URL 只打一次网络。"""
        day = self._now().strftime("%Y%m%d")
        digest = hashlib.sha256(url.encode("utf-8")).hexdigest()[:20]
        return self.cache_dir / f"{day}-{digest}.bin"

    def _read_cache(self, url: str) -> FetchResult | None:
        path = self._cache_path(url)
        if not path.exists():
            return None
        if datetime.now(UTC) - datetime.fromtimestamp(path.stat().st_mtime, UTC) > self.cache_ttl:
            return None
        body = path.read_bytes()
        return FetchResult(
            url=url,
            status=200,
            content=body,
            fetched_at=datetime.fromtimestamp(path.stat().st_mtime, UTC),
            from_cache=True,
            sha256=hashlib.sha256(body).hexdigest(),
        )

    def _write_cache(self, url: str, result: FetchResult) -> None:
        self._cache_path(url).write_bytes(result.content)

    def _log(self, url: str, status: int, *, from_cache: bool, note: str) -> None:
        entry = {
            "at": self._now().isoformat(),
            "url": url,
            "status": status,
            "from_cache": from_cache,
            "note": note,
        }
        with self.log_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
