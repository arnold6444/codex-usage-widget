# Codex Usage Widget for Windows

Codex CLI가 로그인한 계정의 사용량을 공식 `codex app-server`로 조회해 Windows 작업 표시줄의 빈 영역에 띄우는 작은 tkinter HUD입니다.

## 폴더와 EXE 실행

프로젝트 폴더:

```text
C:\Users\arnol\Documents\ChatGPT\ai-usage-bot
```

Python 없이 실행할 EXE:

```text
C:\Users\arnol\Documents\ChatGPT\ai-usage-bot\dist\CodexUsageWidget.exe
```

EXE를 더블클릭하면 됩니다. 실행 중인 위젯이 있으면 중복 창은 만들지 않습니다.

## 요구 환경

- Windows 10/11
- PATH에서 실행 가능한 Codex CLI와 완료된 Codex 로그인

완성된 EXE 실행에는 Python이 필요 없습니다. 소스 실행이나 EXE 재빌드에만 Python 3.10 이상과 PyInstaller가 필요합니다.

EXE 실행 전 PowerShell에서 Codex CLI만 확인하세요.

```powershell
codex --version
codex app-server --help
```

소스 실행이나 재빌드가 필요할 때는 `python --version`도 확인합니다.

## Windows 자동 실행

현재 사용자 Windows 작업 스케줄러의 `Codex Usage Widget` 작업으로 실행합니다.

```text
C:\Users\arnol\Documents\ChatGPT\ai-usage-bot\dist\CodexUsageWidget.exe
```

로그인 시 실행하고, 종료되면 1분 간격으로 다시 실행합니다. Windows가 직접 실행하므로 Codex 앱 종료와 독립적입니다. 배터리에서도 계속 실행하고 실행 시간 제한은 없습니다. 기존 로그인 전용 바로가기는 등록 후 제거합니다. 다시 등록하려면 프로젝트 폴더에서 다음을 실행합니다.

```powershell
powershell -ExecutionPolicy Bypass -File .\install_startup.ps1
```

계속 켜짐 모드에서는 `×`로 닫아도 다음 주기에 다시 열립니다. 완전히 종료하려면 먼저 아래 명령으로 자동 복구를 해제한 뒤 위젯을 닫으세요. EXE를 재빌드할 때도 먼저 해제하고 닫은 다음 빌드 후 재등록하세요.

```powershell
powershell -ExecutionPolicy Bypass -File .\install_startup.ps1 -Remove
```

## 소스 실행

이 폴더에서 다음 한 줄만 실행합니다.

```powershell
python codex_usage_widget.py
```

- 기본 compact HUD는 왼쪽 날씨 위젯 오른쪽 4px 지점에 붙어 `Week …`와 `R 9/11(금) 15:17` 형태의 reset 날짜·요일·시간을 표시합니다. 폭은 114px이며 compact에서는 progress bar를 숨깁니다.
- `▴`를 누르면 progress bar와 reset 시간이 있는 280x130 상세 HUD가 작업 표시줄 바로 위에 열리고, `▾`로 다시 접습니다.
- 버튼 이외의 영역을 드래그해 이동할 수 있습니다.
- 드래그한 위치와 compact/상세 상태는 종료 전에 자동 저장되며, 다음 실행 때 그대로 복원됩니다.
- `×` 또는 `Esc`로 닫습니다.
- HUD는 항상 위에 있고 30초마다 자동 갱신됩니다.
- Explorer가 작업표시줄을 다시 그려도 포커스를 빼앗지 않고 위젯을 다시 최상위로 올립니다.
- 위젯이 시작한 `app-server`가 종료되면 자동으로 다시 연결합니다.

Windows 11은 tkinter 창을 정식 작업 표시줄 항목으로 삽입하는 공개 방식을 제공하지 않으므로, 이 프로그램은 Explorer 설정을 바꾸지 않고 작업 표시줄 빈 공간 위에 borderless/topmost 창을 맞춰 놓습니다. 처음 실행할 때 현재 작업 표시줄을 감지하고, 이후에는 사용자가 옮긴 위치를 우선 복원합니다. 작업 표시줄 위치를 바꿨다면 위젯을 원하는 곳으로 한 번 드래그하면 새 위치가 저장됩니다.

창 상태에는 인증 정보 없이 위치와 표시 모드만 아래 파일에 저장됩니다.

```text
%LOCALAPPDATA%\CodexUsageWidget\settings.json
```

저장 좌표가 현재 화면 밖이면 위젯은 안전한 기본 위치로 돌아옵니다.

## EXE 다시 빌드

실행 중인 위젯을 먼저 닫고 다음을 실행합니다.

```powershell
powershell -ExecutionPolicy Bypass -File .\build_exe.ps1
```

결과는 `dist\CodexUsageWidget.exe`에 만들어집니다. 프로젝트 폴더를 옮겼다면 빌드 후 `install_startup.ps1`도 다시 실행해 바로가기 대상 경로를 갱신하세요.

## 표시 기준

- `Week`: 기본 `rateLimits` 또는 `limitId == codex` 항목의 `windowDurationMins == 10080`
- 남은 값: `100 - usedPercent`
- Reset: `resetsAt` Unix 초를 Windows 로컬 시간으로 변환한 값

초미니 화면도 reset 날짜·요일·시간을 모두 표시하며, 펼친 상세 화면은 같은 시각을 더 크게 표시합니다.

기본 Codex의 현재 주간 값만 표시합니다. `GPT-5.3-Codex-Spark` 같은 모델 전용 300분/주간 항목은 모두 무시하므로 전체 Codex 주간 수치와 혼동되지 않습니다. 기본 주간 출처를 확인할 수 없으면 `—`로 둡니다.

이 프로그램은 `auth.json`, access token, 다른 인증 파일을 직접 읽지 않습니다. 인증과 네트워크 요청은 설치된 Codex CLI의 `app-server`가 담당합니다. 프로토콜 설명은 [OpenAI 공식 Codex App Server 문서](https://developers.openai.com/codex/app-server)를 참고하세요.

## 문제 해결

`CONNECTING` 또는 `RECONNECTING`이 오래 지속되면:

1. PowerShell에서 `codex --version`이 동작하는지 확인합니다.
2. Codex CLI 로그인이 유지되어 있는지 공식 CLI에서 확인합니다.
3. `codex app-server --help`가 오류 없이 끝나는지 확인합니다.
4. CLI 업데이트 후 위젯을 다시 실행합니다.

테스트는 다음 명령으로 실행합니다.

```powershell
python -m unittest discover -v
python -m compileall -q .
```
