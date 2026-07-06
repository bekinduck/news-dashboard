# -*- coding: utf-8 -*-
"""
ニュース収集プログラム
=====================
各メディアのRSS（新着記事のお知らせリスト）を巡回し、
記事を2つのジャンルに自動振り分けして data/news.json に保存します。

GitHub Actions（毎朝7時）から自動実行されます。
手元で動かす場合: python scripts/collect_news.py
"""

import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import feedparser  # RSSを読み取るための道具（ライブラリ）

# ============================================================
# ▼ 設定エリア（ここを編集すればカスタマイズできます）
# ============================================================

# 何日分のニュースを保存するか
DAYS_TO_KEEP = 14

# 巡回するRSSの一覧
#   name  : 画面に表示するメディア名
#   url   : RSSのアドレス
#   mode  : "all"    = 全記事を取り込む（AI専門メディア向け）
#           "filter" = 下のキーワードに当てはまる記事だけ取り込む（総合メディア向け）
#   genre : "ai" = 生成AIジャンル固定 / None = キーワードで自動判定
FEEDS = [
    # --- AI専門メディア・海外公式ブログ（全記事を取り込み） ---
    {"name": "ITmedia AI+",        "url": "https://rss.itmedia.co.jp/rss/2.0/aiplus.xml",                  "mode": "all",    "genre": "ai"},
    {"name": "OpenAI News",        "url": "https://openai.com/news/rss.xml",                               "mode": "all",    "genre": "ai"},
    {"name": "Google AI Blog",     "url": "https://blog.google/technology/ai/rss/",                        "mode": "all",    "genre": "ai"},
    {"name": "Hugging Face Blog",  "url": "https://huggingface.co/blog/feed.xml",                          "mode": "all",    "genre": "ai"},
    {"name": "TechCrunch AI",      "url": "https://techcrunch.com/category/artificial-intelligence/feed/", "mode": "all",    "genre": "ai"},
    # --- 総合メディア（キーワードに合う記事だけ取り込み） ---
    {"name": "ITmedia NEWS",       "url": "https://rss.itmedia.co.jp/rss/2.0/news_bursts.xml",             "mode": "filter", "genre": None},
    {"name": "ITmedia ビジネス",   "url": "https://rss.itmedia.co.jp/rss/2.0/business.xml",                "mode": "filter", "genre": None},
    {"name": "ASCII.jp",           "url": "https://ascii.jp/rss.xml",                                      "mode": "filter", "genre": None},
    {"name": "Impress Watch",      "url": "https://www.watch.impress.co.jp/data/rss/1.0/ipw/feed.rdf",     "mode": "filter", "genre": None},
    {"name": "東洋経済オンライン", "url": "https://toyokeizai.net/list/feed/rss",                          "mode": "filter", "genre": None},
    {"name": "PR TIMES",           "url": "https://prtimes.jp/index.rdf",                                  "mode": "filter", "genre": None},
    {"name": "CNET Japan",         "url": "http://feeds.japan.cnet.com/rss/cnet/all.rdf",                  "mode": "filter", "genre": None},
    {"name": "ZDNET Japan",        "url": "http://feeds.japan.zdnet.com/rss/zdnet/all.rdf",                "mode": "filter", "genre": None},
]

# 「生成AI・AIツール」ジャンルに振り分けるキーワード
AI_KEYWORDS = [
    "生成AI", "生成 AI", "ChatGPT", "OpenAI", "Claude", "Anthropic", "アンソロピック",
    "Gemini", "Copilot", "LLM", "大規模言語モデル", "AIエージェント", "AIモデル",
    "画像生成", "動画生成", "音声AI", "Midjourney", "Stable Diffusion", "Sora",
    "GPT", "AIツール", "AIサービス", "AI活用", "AI搭載", "AIアシスタント",
    "RAG", "プロンプト", "Hugging Face", "DeepSeek", "Perplexity", "NotebookLM",
]

# 「新規事業・新サービス」ジャンルに振り分けるキーワード
BIZ_KEYWORDS = [
    "新規事業", "新事業", "新サービス", "新会社", "子会社設立", "合弁",
    "提供開始", "提供を開始", "サービス開始", "正式リリース", "正式に提供",
    "本格展開", "事業化", "参入", "実証実験", "業務提携", "資本提携",
    "ローンチ", "β版", "ベータ版", "先行公開", "新製品", "新機能",
]

# ============================================================
# ▲ 設定エリアここまで（以下は変更不要）
# ============================================================

JST = timezone(timedelta(hours=9))
REPO_ROOT = Path(__file__).resolve().parent.parent
OUTPUT_FILE = REPO_ROOT / "data" / "news.json"

TAG_RE = re.compile(r"<[^>]+>")  # HTMLタグを取り除くためのパターン


def clean_text(text):
    """記事の説明文からHTMLタグや余分な空白を取り除き、短くする"""
    if not text:
        return ""
    text = TAG_RE.sub("", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:140] + ("…" if len(text) > 140 else "")


def detect_genres(text, feed_conf):
    """タイトル＋説明文からジャンルを判定する。該当なしなら空リスト"""
    genres = set()
    if feed_conf["genre"] == "ai":
        genres.add("ai")
    for kw in AI_KEYWORDS:
        if kw.lower() in text.lower():
            genres.add("ai")
            break
    for kw in BIZ_KEYWORDS:
        if kw in text:
            genres.add("biz")
            break
    return sorted(genres)


def entry_datetime(entry):
    """記事の公開日時を日本時間で取り出す。取れない場合はNone"""
    for key in ("published_parsed", "updated_parsed"):
        t = entry.get(key)
        if t:
            dt = datetime(*t[:6], tzinfo=timezone.utc)
            return dt.astimezone(JST)
    return None


def collect():
    cutoff = datetime.now(JST) - timedelta(days=DAYS_TO_KEEP)
    articles = {}

    # 前回の結果を読み込む（フィードから消えた記事も保持期間内なら残すため）
    if OUTPUT_FILE.exists():
        try:
            old = json.loads(OUTPUT_FILE.read_text(encoding="utf-8"))
            for a in old.get("articles", []):
                articles[a["link"]] = a
        except Exception:
            pass  # 壊れていた場合は無視して作り直す

    ok, failed = [], []
    for feed_conf in FEEDS:
        try:
            d = feedparser.parse(
                feed_conf["url"],
                agent="Mozilla/5.0 (compatible; NewsDashboard/1.0)",
            )
            if not d.entries:
                failed.append(feed_conf["name"])
                continue
            count = 0
            for e in d.entries:
                title = (e.get("title") or "").strip()
                link = (e.get("link") or "").strip()
                if not title or not link:
                    continue
                dt = entry_datetime(e)
                if dt is None or dt < cutoff:
                    continue
                summary = clean_text(e.get("summary") or e.get("description") or "")
                genres = detect_genres(title + " " + summary, feed_conf)
                if feed_conf["mode"] == "filter" and not genres:
                    continue  # キーワードに合わない記事は取り込まない
                if not genres:
                    genres = [feed_conf["genre"] or "ai"]
                articles[link] = {
                    "title": title,
                    "link": link,
                    "source": feed_conf["name"],
                    "genres": genres,
                    "published": dt.isoformat(),
                    "summary": summary,
                }
                count += 1
            ok.append(f"{feed_conf['name']}({count}件)")
        except Exception as ex:
            failed.append(f"{feed_conf['name']}: {ex}")

    # 保持期間を過ぎた記事を削除し、新しい順に並べ替え
    kept = [
        a for a in articles.values()
        if datetime.fromisoformat(a["published"]) >= cutoff
    ]
    kept.sort(key=lambda a: a["published"], reverse=True)

    result = {
        "generated_at": datetime.now(JST).isoformat(),
        "days_kept": DAYS_TO_KEEP,
        "articles": kept,
    }
    OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_FILE.write_text(
        json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8"
    )

    print(f"保存完了: {len(kept)}件 → {OUTPUT_FILE}")
    print("取得成功:", ", ".join(ok) if ok else "なし")
    if failed:
        print("取得失敗（スキップ）:", ", ".join(failed))
    # フィードが全滅した場合のみエラー終了（1つでも成功すれば正常扱い）
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(collect())
