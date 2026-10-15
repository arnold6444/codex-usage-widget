# Codex / Claude Usage Widget for Windows

Codex CLI와 Claude CLI가 로그인한 계정의 사용량을 각각 공식 `codex app-server`와 `claude` CLI의 `get_usage` 컨트롤 요청으로 조회해 Windows 작업 표시줄의 빈 영역에 띄우는 작은 tkinter HUD입니다.

표시 항목은 세 가지입니다.

| 열 | 의미 | 출처 | 갱신 주기 |
|---|---|---|---|
| `CX 주` | Codex 주간 한도 잔여 | `codex app-server` → `account/rateLimits/read` | 30초 |
| `CL 5h` | Claude 5시간 한도 잔여 | `claude` CLI → `get_usage` | 2분 (자동 조절) |
| `CL 주` | Claude 주간 한도 잔여 | `claude` CLI → `get_usage` | 2분 (자동 조절) |

세 열 모두 컴팩트 모드에서도 아랫줄에 reset 시각을 함께 표시합니다. 한쪽 CLI가 실패해도 다른 쪽 표시는 계속 갱신됩니다.

조회가 오래 실패하면(Codex 5분, Claude 15분 이상) 마지막 값은 남겨 두되 **회색**으로 바꿉니다. 회색 숫자는 현재 값이 아니라 마지막으로 받은 값입니다.

Claude 사용량 조회 API는 계정별로 호출 횟수 제한이 있습니다. 제한에 걸리면 서버가 `429 rate_limit_error`(retry-after 1시간)를 돌려주고, CLI는 `rate_limits: null`로 답합니다. 제한이 걸려 있는 동안은 **대체 조회**로 실제 값을 계속 받습니다. 모든 대화 응답 헤더에는 같은 5시간·주간 사용률이 실려 오고, CLI는 한 턴을 마친 뒤 그 값으로 `get_usage`에 답합니다. 그래서 5분마다 Haiku 한 단어 턴(시스템 프롬프트 교체, 도구·MCP 없음, 세션 저장 안 함)을 한 번 실행해 값을 갱신합니다. 한 번에 입력 약 600토큰(약 $0.0015 상당)이라 한도 영향은 무시할 수준이고, 무료 API가 막혔을 때만 씁니다. 대체 조회까지 실패하면 상태가 `WAIT`가 되고 값은 회색으로 바뀝니다.

한 번 제한에 걸리면 평소 조회 간격을 2배로 늘리고(최대 10분), 30번 연속 성공하면 절반씩 줄여 다시 2분으로 돌아옵니다. 조회 시에는 `--setting-sources local`로 실행해 사용자의 전역 hook이 매번 돌지 않게 합니다.

왼쪽 끝의 작은 점은 **연결 상태**이며 사용 중 표시가 아닙니다. 두 출처 중 나쁜 쪽을 따라갑니다: 초록 = 둘 다 방금 갱신됨(LIVE), 주황 = 재연결 중이거나 마지막 정상값을 보여주는 중, 빨강 = 한도를 아예 읽지 못함.

## 폴더와 EXE 실행

권장 설치 폴더:

```text
%LOCALAPPDATA%\Programs\CodexUsageWidget
```

Python 없이 실행할 EXE:

```text
%LOCALAPPDATA%\Programs\CodexUsageWidget\CodexUsageWidget.exe
```

EXE를 더블클릭하면 됩니다. 실행 중인 위젯이 있으면 중복 창은 만들지 않습니다.

## 요구 환경

- Windows 10/11
- PATH에서 실행 가능한 Codex CLI와 완료된 Codex 로그인
- PATH 또는 `%USERPROFILE%\.localin`의 Claude CLI와 완료된 Claude 로그인 (구독 계정이 아니면 Claude 열은 `—`로 남습니다)

완성된 EXE 실행에는 Python이 필요 없습니다. 소스 실행에는 Python 3.10 이상이 필요하며, 현재 Windows 빌드는 Python 3.11과 PyInstaller 사용을 권장합니다.

EXE 실행 전 PowerShell에서 Codex CLI만 확인하세요.

```powershell
codex --version
codex app-server --help
claude --version
```

소스 실행이나 재빌드가 필요할 때는 `python --version`도 확인합니다.

## Windows 자동 실행

현재 사용자 시작프로그램 바로가기:

```text
%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\Codex Usage Widget.lnk
```

로그인할 때마다 Windows 작업 스케줄러의 `Codex Usage Widget Supervisor`가 콘솔 없는 supervisor를 실행합니다. Python 3.11이 있으면 서명된 `pythonw.exe`와 소스 파일을 우선 사용하므로 Windows 앱 제어 정책이 unsigned PyInstaller EXE를 차단하는 환경에서도 동작합니다. 같은 예약 작업이 1분마다 supervisor 존재도 다시 확인합니다. 시작프로그램 바로가기는 보조 진입점이며, 중복 supervisor는 mutex로 차단됩니다. CMD나 PowerShell 창을 만들지 않으며, 위젯을 닫거나 프로세스가 종료되어도 몇 초 뒤 다시 실행됩니다.

```powershell
powershell -ExecutionPolicy Bypass -File .\install_startup.ps1
```

자동 실행만 해제하려면:

```powershell
powershell -ExecutionPolicy Bypass -File .\install_startup.ps1 -Remove
```

예약 작업은 현재 로그인한 사용자 권한으로만 실행되며 관리자 권한을 사용하지 않습니다.

## 소스 실행

이 폴더에서 다음 한 줄만 실행합니다.

```powershell
python codex_usage_widget.py
```

- 기본 compact HUD는 왼쪽 날씨 위젯 오른쪽 4px 지점에 붙어 `CX 주 4%` / `CL 5h 87%` / `CL 주 97%` 세 열과 각 열의 `R 9/11(금)15:17` 형태 reset 시각을 표시합니다. 폭은 308px, 높이는 작업 표시줄에 맞춘 32px이며 compact에서는 progress bar를 숨깁니다.
- `▴`를 누르면 세 항목의 progress bar와 reset 시간이 있는 280x186 상세 HUD가 열리고, `▾`로 다시 접습니다.
- 버튼 이외의 영역을 드래그해 이동할 수 있습니다.
- 드래그한 위치와 compact/상세 상태는 종료 전에 자동 저장되며, 다음 실행 때 그대로 복원됩니다.
- 한도 임계 알림은 **기본으로 꺼져 있습니다**. HUD가 이미 항상 보이는 자리에 있어서 별도 알림이 방해가 됩니다. 켜려면 `CodexUsageWidget(root, alerts_enabled=True)`로 생성하세요. 켜면 **80% 사용**에 경고, **90% 사용**에 임박 알림이 한 번씩 뜨고 HUD 테두리가 세 번 깜빡입니다. 같은 한도 창에서는 같은 임계값을 다시 알리지 않으며, 재시작해도 캐시를 읽어 이미 알린 값은 다시 알리지 않습니다.
- `×` 또는 `Esc`로 닫습니다.
- HUD는 항상 위에 있습니다. 0.5초마다 위젯 중앙 픽셀의 주인이 자기 자신인지 확인하고, 작업 표시줄이나 다른 창에 가려졌을 때만 다시 올립니다. 가려진 상태에서 약 0.4초 안에 복구되는 것을 실제로 확인했습니다.
- 디스플레이 배율·해상도·모니터 구성이 바뀌면 30초 안에 감지해 화면 안으로 다시 맞춥니다. 프로세스는 per-monitor DPI aware로 실행되어 작업 표시줄 좌표와 창 좌표가 같은 픽셀 공간을 씁니다.
- Explorer가 작업표시줄을 다시 그려도 포커스를 빼앗지 않고 위젯을 다시 최상위로 올립니다.
- 위젯이 시작한 `app-server`가 종료되면 자동으로 다시 연결합니다. Claude는 폴링마다 CLI를 짧게 실행했다 종료하므로 상주 프로세스를 남기지 않습니다.
- 인터넷이나 Codex 연결이 끊기면 마지막 정상 주간 값을 유지하고 상태 점을 주황색으로 바꿉니다. 연결이 복구되면 자동으로 최신 값과 초록색 상태로 돌아옵니다.

Windows 11은 tkinter 창을 정식 작업 표시줄 항목으로 삽입하는 공개 방식을 제공하지 않으므로, 이 프로그램은 Explorer 설정을 바꾸지 않고 작업 표시줄 빈 공간 위에 borderless/topmost 창을 맞춰 놓습니다. 처음 실행할 때 현재 작업 표시줄을 감지하고, 이후에는 사용자가 옮긴 위치를 우선 복원합니다. 작업 표시줄 위치를 바꿨다면 위젯을 원하는 곳으로 한 번 드래그하면 새 위치가 저장됩니다.

창 상태에는 인증 정보 없이 위치와 표시 모드만 아래 파일에 저장됩니다.

```text
%LOCALAPPDATA%\CodexUsageWidget\settings.json
%LOCALAPPDATA%\CodexUsageWidget\last_snapshot.json
%LOCALAPPDATA%\CodexUsageWidget\widget.log
```

`widget.log`는 256KB씩 최대 3개까지 회전하는 로그입니다. 시작/종료, 출처별 상태 전환, 한도 알림, 작업 표시줄에 가려져 다시 올린 횟수(1분에 한 번 요약), 디스플레이 구성 변경, Win32 실패가 기록됩니다. 사용률 수치 외의 계정 정보는 남지 않습니다.

`last_snapshot.json`에는 출처별(`codex`/`claude`) 마지막 사용률과 reset 시각만 저장되며 계정 ID, 토큰, 인증정보는 저장하지 않습니다. 인터넷이 끊긴 동안에는 실시간 갱신이 불가능하므로 표시값은 마지막 정상 조회값입니다.

저장 좌표가 현재 화면 밖이면 위젯은 안전한 기본 위치로 돌아옵니다.

## EXE 다시 빌드

실행 중인 위젯을 먼저 닫고 다음을 실행합니다.

```powershell
powershell -ExecutionPolicy Bypass -File .\build_exe.ps1
```

결과는 `dist\CodexUsageWidget.exe`에 만들어집니다. 프로젝트 폴더를 옮겼다면 빌드 후 `install_startup.ps1`도 다시 실행해 바로가기 대상 경로를 갱신하세요.

## 표시 기준

- `CX 주`: 기본 `rateLimits` 또는 `limitId == codex` 항목의 `windowDurationMins == 10080`
- `CL 5h` / `CL 주`: `get_usage` 응답의 `rate_limits.five_hour` / `rate_limits.seven_day`
- 남은 값: Codex는 `100 - usedPercent`, Claude는 `100 - utilization` (`utilization`은 0~100 퍼센트)
- Reset: Codex는 `resetsAt` Unix 초, Claude는 `resets_at` ISO 8601 문자열을 각각 Windows 로컬 시간으로 변환한 값

초미니 화면도 reset 날짜·요일·시간을 모두 표시하며, 펼친 상세 화면은 같은 시각을 더 크게 표시합니다.

기본 Codex의 현재 주간 값만 표시합니다. `GPT-5.3-Codex-Spark` 같은 모델 전용 300분/주간 항목은 모두 무시하므로 전체 Codex 주간 수치와 혼동되지 않습니다. 기본 주간 출처를 확인할 수 없으면 `—`로 둡니다.

이 프로그램은 `auth.json`, `.credentials.json`, access token, 다른 인증 파일을 직접 읽지 않습니다. 인증과 네트워크 요청은 설치된 Codex CLI의 `app-server`와 Claude CLI가 담당합니다.

Claude의 `get_usage` 컨트롤 요청은 CLI가 experimental로 표시한 경로라 응답 구조가 바뀔 수 있습니다. 파서는 키가 없거나 형태가 달라져도 예외 없이 `—`로 떨어지도록 작성되어 있습니다. 프로토콜 설명은 [OpenAI 공식 Codex App Server 문서](https://developers.openai.com/codex/app-server)를 참고하세요.

## 문제 해결

`CONNECTING` 또는 `RECONNECTING`이 오래 지속되면:

1. PowerShell에서 `codex --version`, `claude --version`이 동작하는지 확인합니다.
2. 두 CLI 로그인이 유지되어 있는지 각 공식 CLI에서 확인합니다.
3. `codex app-server --help`가 오류 없이 끝나는지 확인합니다.
4. 상태가 `LOGIN`(빨간 점, Claude 열 회색)이면 Claude CLI 로그인이 만료된 것입니다. 갱신 토큰이 만료되면 CLI가 저장된 토큰을 스스로 지웁니다. 터미널에서 `claude auth login`을 한 번 실행하면 위젯은 다음 조회(최대 2분) 때 저절로 복구됩니다. `claude auth status`의 `loggedIn`으로 확인할 수 있습니다.
5. Claude 열만 비어 있다면 Claude Code에서 `/usage`가 값을 보여주는지 확인합니다. API 키 계정 등 구독 한도가 없는 경우에는 정상적으로 `NO MATCH`가 됩니다.
6. CLI 업데이트 후 위젯을 다시 실행합니다.

증상이 표시 쪽(안 보임, 위치 이상)이라면 `%LOCALAPPDATA%\CodexUsageWidget\widget.log`를 먼저 확인하세요. 가려짐 복구 횟수와 Win32 실패가 그대로 남습니다.

테스트는 다음 명령으로 실행합니다.

```powershell
python -m unittest discover -v
python -m compileall -q .
```
