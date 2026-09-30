"""설정 로더 — config 와 비밀값을 분리한다.

- 일반 설정: config.json (없으면 config.example.json)
- 비밀값: 환경변수 → secrets/ 폴더의 텍스트 파일 순으로 찾아 설정에 덮어쓴다.

공개 저장소에는 config.example.json 만 올라가고, config.json 과 secrets/ 는
.gitignore 로 제외된다. 토큰을 config.json 에 직접 적어도 동작은 하지만,
secrets/ 에 두면 설정 파일을 그대로 공유해도 안전하다.

  secrets/telegram_bot_token.txt    또는  TELEGRAM_BOT_TOKEN
  secrets/telegram_chat_id.txt      또는  TELEGRAM_CHAT_ID
  secrets/claude_oauth_token.txt    또는  CLAUDE_CODE_OAUTH_TOKEN
  secrets/sec_user_agent.txt        또는  SEC_USER_AGENT   ("이름 이메일" 형식, SEC 정책)
  secrets/dart_api_key.txt          또는  DART_API_KEY     (evcal/sources/dart.py 가 직접 읽음)
"""
import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SECRETS_DIR = ROOT / "secrets"

# (설정 경로, 환경변수, secrets 파일명)
_SECRET_MAP = [
    (("telegram", "bot_token"), "TELEGRAM_BOT_TOKEN", "telegram_bot_token.txt"),
    (("telegram", "chat_id"), "TELEGRAM_CHAT_ID", "telegram_chat_id.txt"),
    (("digest", "claude_oauth_token"), "CLAUDE_CODE_OAUTH_TOKEN", "claude_oauth_token.txt"),
    (("calendar", "edgar_user_agent"), "SEC_USER_AGENT", "sec_user_agent.txt"),
    (("btc_treasury", "sec_user_agent"), "SEC_USER_AGENT", "sec_user_agent.txt"),
]


def _read_secret(name: str) -> str:
    p = SECRETS_DIR / name
    return p.read_text(encoding="utf-8").strip() if p.exists() else ""


def config_path() -> Path:
    p = ROOT / "config.json"
    return p if p.exists() else ROOT / "config.example.json"


def load_config() -> dict:
    cfg = json.loads(config_path().read_text(encoding="utf-8"))
    for keys, env, fname in _SECRET_MAP:
        val = os.environ.get(env, "").strip() or _read_secret(fname)
        if not val:
            continue
        node = cfg
        for k in keys[:-1]:
            node = node.setdefault(k, {})
        node[keys[-1]] = val
    # dart_api_key_file 이 상대경로면 프로젝트 루트 기준으로 해석
    cal = cfg.get("calendar") or {}
    f = cal.get("dart_api_key_file")
    if f and not Path(f).is_absolute():
        cal["dart_api_key_file"] = str(ROOT / f)
    return cfg
