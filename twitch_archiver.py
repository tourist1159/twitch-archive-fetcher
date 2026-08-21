"""
Twitch (mokouliszt1) の過去配信(VOD)一覧を Twitch Helix API から取得し、
twitch_archives.json に書き出す。コメント(チャットリプレイ)は対象外 — VOD一覧のみ。

- 認証: App Access Token (Client Credentials Flow)。ユーザーログイン不要、
  公開VOD一覧の取得にはこれで足りる。TWITCH_CLIENT_ID / TWITCH_CLIENT_SECRET を
  環境変数で渡す (ローカルは各自設定 / GitHub Actions は repo の Secrets)。
  https://dev.twitch.tv/console/apps で作成した Application の値。
- type=archive のみ対象 (配信の完全アーカイブ。ハイライト/アップロードは対象外)。
- Twitch は VOD を一定期間で削除する。削除されたVODは一覧から静かに消えるだけで
  個別取得も404になるとは限らないため、Kick版 fetcher の mark_availability と
  同じガード付きロジックで available:false を付与する (取りこぼし防止)。
"""

import json
import os
import re
import sys
import functools
from datetime import datetime
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode

print = functools.partial(print, file=sys.stderr, flush=True)

CLIENT_ID = os.environ["TWITCH_CLIENT_ID"]
CLIENT_SECRET = os.environ["TWITCH_CLIENT_SECRET"]
CHANNEL_LOGIN = "mokouliszt1"
OUT_FILE = "twitch_archives.json"

DURATION_RE = re.compile(r"(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s)?")


def request_json(url, method="GET", data=None, headers=None):
    body = urlencode(data).encode() if data else None
    req = Request(url, data=body, method=method, headers=headers or {})
    with urlopen(req, timeout=20) as res:
        return json.loads(res.read().decode("utf-8"))


def get_app_token():
    res = request_json(
        "https://id.twitch.tv/oauth2/token",
        method="POST",
        data={
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            "grant_type": "client_credentials",
        },
    )
    return res["access_token"]


def helix_headers(token):
    return {"Client-Id": CLIENT_ID, "Authorization": f"Bearer {token}"}


def get_user_id(login, token):
    res = request_json(
        f"https://api.twitch.tv/helix/users?login={login}",
        headers=helix_headers(token),
    )
    data = res.get("data") or []
    if not data:
        raise RuntimeError(f"Twitchユーザーが見つかりません: {login}")
    return data[0]["id"]


def parse_duration(s):
    """Helix の 'duration' ('1h2m3s' 形式) を秒に変換する"""
    m = DURATION_RE.fullmatch(s or "")
    if not m:
        return 0
    h, mi, se = (int(x) if x else 0 for x in m.groups())
    return h * 3600 + mi * 60 + se


def build_thumbnail(template, width=320, height=180):
    if not template:
        return None
    return template.replace("%{width}", str(width)).replace("%{height}", str(height))


def fetch_videos(user_id, token, video_type="archive"):
    videos = []
    cursor = None
    while True:
        params = {"user_id": user_id, "type": video_type, "first": "100"}
        if cursor:
            params["after"] = cursor
        res = request_json(
            "https://api.twitch.tv/helix/videos?" + urlencode(params),
            headers=helix_headers(token),
        )
        data = res.get("data") or []
        for v in data:
            videos.append(
                {
                    "id": v["id"],
                    "title": v.get("title") or "",
                    "url": f"https://www.twitch.tv/videos/{v['id']}",
                    "start_time": v.get("created_at"),
                    "duration": parse_duration(v.get("duration")),
                    "thumbnail": build_thumbnail(v.get("thumbnail_url")),
                    "view_count": v.get("view_count"),
                    "available": True,
                }
            )
        cursor = (res.get("pagination") or {}).get("cursor")
        if not cursor or not data:
            break
    return videos


def load_existing():
    try:
        with open(OUT_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return []


def to_epoch(iso):
    try:
        return datetime.fromisoformat((iso or "").replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def merge(existing, fresh):
    """fresh (現在のAPI一覧) で upsert し、一覧から消えたVODは available:false にする。

    誤判定防止:
      - fresh が空 (取得失敗) なら何もしない。全件を削除済みにしてしまわないため。
      - fresh 中の最古の配信より新しいはずなのに一覧に無いものは判定を保留する
        (ページング取りこぼし対策)。
    """
    if not fresh:
        print("⚠️ Twitch側の一覧が空 (取得失敗の可能性) — 既存データを保持します")
        return existing

    by_id = {a["id"]: a for a in existing}
    fresh_ids = set()
    for v in fresh:
        fresh_ids.add(v["id"])
        prev = by_id.get(v["id"])
        if prev:
            prev.update(v)
        else:
            by_id[v["id"]] = v

    oldest = min(
        (to_epoch(v["start_time"]) for v in fresh if to_epoch(v["start_time"]) is not None),
        default=None,
    )
    changed = 0
    for vid, a in by_id.items():
        if vid in fresh_ids or oldest is None:
            continue
        ts = to_epoch(a.get("start_time"))
        if ts is None or ts >= oldest:
            continue  # 判定保留
        if a.get("available") is not False:
            a["available"] = False
            changed += 1
    if changed:
        print(f"🗑️ 削除済みとして記録: {changed} 件")

    return list(by_id.values())


def main():
    token = get_app_token()
    user_id = get_user_id(CHANNEL_LOGIN, token)
    fresh = fetch_videos(user_id, token)
    print(f"📡 Twitch API から {len(fresh)} 件取得 ({CHANNEL_LOGIN})")

    merged = merge(load_existing(), fresh)
    merged.sort(key=lambda a: a.get("start_time") or "", reverse=True)

    with open(OUT_FILE, "w", encoding="utf-8") as f:
        json.dump(merged, f, ensure_ascii=False, indent=2)
    print(f"📁 {OUT_FILE} 更新完了 (計 {len(merged)} 件)")


if __name__ == "__main__":
    try:
        main()
    except (HTTPError, URLError) as e:
        print(f"❌ 通信エラー: {e}")
        sys.exit(1)
