"""
Telegram 설정 도우미 (Stage 2).

사용 순서:
  1) 텔레그램에서 @BotFather 로 봇 생성 -> 토큰 발급.
  2) secrets/telegram_bot_token.txt 에 토큰을 넣거나, 아래처럼 인자로 전달.
  3) 방금 만든 봇을 텔레그램에서 검색해 대화방을 열고 아무 메시지나 1개 보냄
     (예: "hi"). (봇은 먼저 말을 건 사용자에게만 메시지를 보낼 수 있음)
  4) 이 스크립트로 chat_id 확인:
        python tg_setup.py                 (config의 토큰 사용)
        python tg_setup.py <BOT_TOKEN>
  5) 나온 chat_id 를 secrets/telegram_chat_id.txt 에 넣고, config.json 의 telegram.enabled=true.
  6) 테스트 전송:
        python tg_setup.py <BOT_TOKEN> <CHAT_ID>
"""
import sys
import json
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CONFIG = ROOT / "config.json"


def cfg_token():
    try:
        sys.path.insert(0, str(ROOT))
        from secrets_loader import load_config
        return (load_config().get("telegram", {}).get("bot_token", "") or "").strip()
    except Exception:
        return ""


def api(token, method, params=None):
    url = f"https://api.telegram.org/bot{token}/{method}"
    data = json.dumps(params).encode("utf-8") if params else None
    headers = {"Content-Type": "application/json"} if data else {}
    req = urllib.request.Request(url, data=data, headers=headers)
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read().decode("utf-8"))


def main():
    args = sys.argv[1:]
    token = args[0] if args else cfg_token()
    if not token:
        print("토큰이 없습니다. config.json 에 넣거나 인자로 전달하세요.")
        print("  python tg_setup.py <BOT_TOKEN>")
        return

    # 테스트 전송 모드
    if len(args) >= 2:
        chat_id = args[1]
        res = api(token, "sendMessage", {
            "chat_id": chat_id,
            "text": "✅ News Monitor 텔레그램 연결 테스트 성공!",
        })
        print("sendMessage ok:", res.get("ok"), res.get("description", ""))
        return

    # chat_id 탐색 모드
    who = api(token, "getMe")
    if not who.get("ok"):
        print("토큰이 올바르지 않은 것 같습니다:", who)
        return
    print("봇 확인:", "@" + who["result"].get("username", "?"))
    upd = api(token, "getUpdates")
    chats = {}
    for u in upd.get("result", []):
        msg = u.get("message") or u.get("channel_post") or {}
        chat = msg.get("chat")
        if chat:
            chats[chat["id"]] = chat.get("username") or chat.get("title") or chat.get("first_name", "")
    if not chats:
        print("아직 수신된 메시지가 없습니다. 텔레그램에서 봇에게 아무 메시지나 보낸 뒤 다시 실행하세요.")
        return
    print("발견된 chat_id:")
    for cid, name in chats.items():
        print(f"  chat_id = {cid}   ({name})")
    print("\n이 chat_id 를 secrets/telegram_chat_id.txt 에 넣고 config.json 의 telegram.enabled=true 로 설정하세요.")


if __name__ == "__main__":
    main()
