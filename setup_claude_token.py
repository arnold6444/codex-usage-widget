"""One-time setup: let the widget keep reading Claude usage after logins expire.

Run from a terminal:

    python setup_claude_token.py           # create and store a token
    python setup_claude_token.py --check   # test the stored token
    python setup_claude_token.py --remove  # forget it

It runs the official ``claude setup-token`` (you approve it in the browser),
then asks you to paste the token it prints. The token is saved in Windows
Credential Manager and is never printed or logged by the widget.
"""

from __future__ import annotations

import getpass
import subprocess
import sys

from claude_token_store import delete_token, looks_like_token, read_token, write_token
from claude_usage_client import ClaudeUsageClient, resolve_claude_command


def _check() -> int:
    if read_token() is None:
        print("저장된 장기 토큰이 없습니다. `python setup_claude_token.py`로 등록하세요.")
        return 1
    print("저장된 토큰으로 사용량을 조회합니다(짧은 Haiku 한 턴, 10초쯤 걸림)...")
    payload = ClaudeUsageClient()._probe_with_token()
    limits = (payload or {}).get("rate_limits") or {}
    if not limits:
        print("조회 실패: 토큰이 만료됐거나 권한이 없습니다. 다시 등록하세요.")
        return 1
    for key, label in (("five_hour", "5시간"), ("seven_day", "주간")):
        window = limits.get(key) or {}
        used = window.get("utilization")
        if used is not None:
            print(f"  {label}: {used:.0f}% 사용 (남은 {100 - used:.0f}%)")
    print("정상입니다. 위젯이 로그인 만료 때 이 토큰으로 계속 조회합니다.")
    return 0


def main(argv: list[str]) -> int:
    if "--remove" in argv:
        print("삭제했습니다." if delete_token() else "삭제하지 못했습니다.")
        return 0
    if "--check" in argv:
        return _check()

    print("1/3  claude setup-token 을 실행합니다. 브라우저가 열리면 승인하세요.")
    print("     끝나면 화면에 sk-ant-... 로 시작하는 토큰이 나옵니다. 그 토큰을 복사하세요.\n")
    try:
        subprocess.run([*resolve_claude_command(), "setup-token"], check=False)
    except (OSError, KeyboardInterrupt) as exc:
        print(f"claude setup-token 실행 실패: {exc}")
        return 1

    print("\n2/3  복사한 토큰을 붙여넣고 Enter (입력해도 화면에 보이지 않습니다).")
    token = getpass.getpass("토큰: ").strip()
    if not looks_like_token(token):
        print("토큰 형식이 아닙니다(sk-ant- 로 시작해야 함). 다시 실행하세요.")
        return 1
    if not write_token(token):
        print("Windows 자격 증명 관리자에 저장하지 못했습니다.")
        return 1
    token = ""
    print("     저장했습니다 (Windows 자격 증명 관리자).")

    print("\n3/3  저장한 토큰으로 실제 조회를 확인합니다.")
    return _check()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
