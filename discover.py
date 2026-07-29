from __future__ import annotations

import argparse
import json
import time
from typing import Any

import httpx

import app

AUTHORIZED_SOURCES = {
    "醉青春壁纸库": "MzkyMjY0NTEyMg==",
    "泡芙喵头像社": "Mzk3NTk0MDM5MA==",
    "开心需要理由吗": "Mzg5MDE2MTA1MA==",
    "热头像": "MzU4MTEwNDU5MA==",
    "会画卧蚕吗": "MzYyMTg3MjgwMg==",
}


def article_seen(url: str) -> bool:
    with app.db() as conn:
        return conn.execute(
            "SELECT 1 FROM discovered_articles WHERE article_url = ?", (url,)
        ).fetchone() is not None


def record(result: dict[str, Any]) -> None:
    now = int(time.time())
    with app.db() as conn:
        conn.execute(
            """INSERT INTO discovered_articles(
                article_url, account_name, account_biz, article_title, state,
                found_images, imported_images, error, discovered_at, processed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(article_url) DO UPDATE SET
                account_name=excluded.account_name,
                account_biz=excluded.account_biz,
                article_title=excluded.article_title,
                state=excluded.state,
                found_images=excluded.found_images,
                imported_images=excluded.imported_images,
                error=excluded.error,
                processed_at=excluded.processed_at""",
            (
                result["url"],
                result.get("account", ""),
                result.get("account_biz", ""),
                result.get("title", ""),
                result["state"],
                result.get("found", 0),
                result.get("imported", 0),
                result.get("error", "")[:1000],
                now,
                now,
            ),
        )


def probe(url: str) -> tuple[str, str, str]:
    url = app.validate_article_url(url)
    with httpx.Client(timeout=httpx.Timeout(20, connect=8), follow_redirects=False) as client:
        html, _ = app.fetch_limited(client, url, app.MAX_ARTICLE_BYTES)
    title, account, _ = app.parse_article(html, url)
    return title, account, app.extract_account_biz(html)


def process_url(url: str, retry: bool = False) -> dict[str, Any]:
    result: dict[str, Any] = {"url": url, "state": "failed"}
    try:
        normalized = app.validate_article_url(url)
        result["url"] = normalized
        if article_seen(normalized) and not retry:
            return {"url": normalized, "state": "already_processed"}
        title, account, account_biz = probe(normalized)
        result.update(title=title, account=account, account_biz=account_biz)
        expected_biz = AUTHORIZED_SOURCES.get(account)
        if not expected_biz or expected_biz != account_biz:
            result.update(state="ignored", error="不属于已授权公众号或唯一标识不匹配")
            record(result)
            return result
        imported = app.ingest_article(
            normalized,
            image_status="pending",
            expected_account=account,
            expected_biz=expected_biz,
        )
        result.update(imported)
        result["state"] = "pending_review" if imported["imported"] else "no_new_images"
        record(result)
        return result
    except Exception as exc:
        result["error"] = str(exc)
        try:
            record(result)
        except Exception:
            pass
        return result


def main() -> int:
    parser = argparse.ArgumentParser(
        description="校验公开搜索发现的微信公众号文章，并将新图片导入待审核队列。"
    )
    parser.add_argument("urls", nargs="+", help="公开搜索发现的 mp.weixin.qq.com 文章 URL")
    parser.add_argument("--retry", action="store_true", help="重新处理已经记录的文章")
    args = parser.parse_args()
    results = [process_url(url, retry=args.retry) for url in dict.fromkeys(args.urls)]
    summary = {
        "candidates": len(results),
        "pending_articles": sum(item.get("state") == "pending_review" for item in results),
        "pending_images": sum(int(item.get("imported", 0)) for item in results),
        "ignored": sum(item.get("state") == "ignored" for item in results),
        "failed": sum(item.get("state") == "failed" for item in results),
        "results": results,
    }
    print(json.dumps(summary, ensure_ascii=True, indent=2))
    return 1 if summary["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
