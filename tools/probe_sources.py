#!/usr/bin/env python3
"""数据源可行性探测器（P-C1）

只做一件事：对候选源发一次 GET，打印状态码、类型、长度与响应片段。
不解析业务、不写任何内容——纯粹用来回答"这个源到底通不通、返回什么"。

用法：python3 tools/probe_sources.py
"""
from __future__ import annotations

import ssl
import sys
import urllib.error
import urllib.request

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0 Safari/537.36"
CTX = ssl.create_default_context()
CTX.check_hostname = False
CTX.verify_mode = ssl.CERT_NONE


def probe(name: str, url: str, headers: dict[str, str] | None = None, limit: int = 320) -> bool:
    h = {"User-Agent": UA, "Accept": "*/*"}
    if headers:
        h.update(headers)
    try:
        req = urllib.request.Request(url, headers=h)
        with urllib.request.urlopen(req, timeout=20, context=CTX) as r:
            body = r.read(20000)
            ctype = (r.headers.get("Content-Type") or "").split(";")[0]
            text = body.decode("utf-8", "replace").replace("\n", " ").replace("\r", " ")
            print(f"[OK ] {name}")
            print(f"      HTTP {r.status} | {ctype} | {len(body)}+ bytes")
            print(f"      {text[:limit]}")
            return True
    except urllib.error.HTTPError as e:
        print(f"[ERR] {name}  HTTP {e.code} {e.reason}")
        return False
    except Exception as e:
        print(f"[ERR] {name}  {type(e).__name__}: {e}")
        return False
    finally:
        print()


if __name__ == "__main__":
    targets = json_targets = None  # 占位，实际目标在下方
    targets = [
        ("国家统计局 easyquery（制造业 PMI）",
         "https://data.stats.gov.cn/easyquery.htm?m=QueryData&dbcode=hgyd&rowcode=zb&colcode=sj"
         "&wds=%5B%5D&dfwds=%5B%7B%22wdcode%22%3A%22zb%22%2C%22valuecode%22%3A%22A0B0101%22%7D%5D",
         {"Referer": "https://data.stats.gov.cn/easyquery.htm?cn=A01"}),
        ("FRED fredgraph.csv（10Y 国债，无需 key）",
         "https://fred.stlouisfed.org/graph/fredgraph.csv?id=DGS10",
         None),
        ("FRED 发布日历（releases/dates，演示用 ID=10）",
         "https://fred.stlouisfed.org/graph/fredgraph.csv?id=DGS2",
         None),
        ("中国人民银行 社会融资规模页",
         "http://www.pbc.gov.cn/diaochatongjisi/116219/116319/index.html",
         None),
        ("中证指数官网首页",
         "https://www.csindex.com.cn/",
         None),
    ]
    ok = 0
    for name, url, hdr in targets:
        if probe(name, url, hdr):
            ok += 1
    print(f"=== 成功 {ok} / {len(targets)} ===")
    sys.exit(0 if ok else 1)
