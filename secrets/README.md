# secrets/

이 폴더의 파일은 `.gitignore`로 제외됩니다(이 README만 예외). 필요한 것만 만드세요.

| 파일 | 내용 | 대체 환경변수 |
|---|---|---|
| `telegram_bot_token.txt` | 텔레그램 봇 토큰 (@BotFather) | `TELEGRAM_BOT_TOKEN` |
| `telegram_chat_id.txt` | 받을 채팅 ID (`python tg_setup.py`로 확인) | `TELEGRAM_CHAT_ID` |
| `claude_oauth_token.txt` | `claude setup-token`으로 발급한 토큰 (다이제스트용) | `CLAUDE_CODE_OAUTH_TOKEN` |
| `sec_user_agent.txt` | `이름 이메일` 형식 — SEC EDGAR 공정접근 정책 | `SEC_USER_AGENT` |
| `dart_api_key.txt` | OpenDART 인증키 (캘린더의 국내 공시) | `DART_API_KEY` |

파일 하나에 값 하나, 앞뒤 공백 없이 저장합니다.
