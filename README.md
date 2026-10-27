# agent-hub

Claude Code, Codex CLI, 로컬 모델(Hermes Agent + OpenAI 호환 로컬 서버)이 일을 나눠 맡게 하는 작은 CLI다.
파일 하나(`hub.py`)에 표준 라이브러리만 쓴다.

- 잘하는 쪽에 먼저 맡긴다. 설계·검토·문서는 Claude, 구현·테스트·수정은 Codex, 요약·탐색은 로컬이다.
- 구독 한도가 떨어지면 넘긴다. Codex가 막히면 Claude가, Claude가 막히면 Codex가, 둘 다 막히면 로컬이 맡는다.
- 스킬·MCP·플러그인을 `manifest.json` 하나로 관리하고 세 도구에 똑같이 맞춘다.

## 명령

```bash
hub status                         # 세 실행자의 한도와 검토 대기 목록
hub run --kind fix "작업 설명"       # 위임. --kind 를 빼면 키워드와 로컬 모델이 고른다
hub run --to local "작업 설명"       # 실행자 고정(대체 없음)
hub sync                           # 스킬·MCP·플러그인 변경 계획
hub sync --apply                   # 반영(설정 파일을 먼저 백업)
```

| kind | 맡는 순서 |
|---|---|
| design, review, docs, debug | claude → codex → local |
| implement, test, fix | codex → claude → local |
| summarize, explore | local → codex → claude |

`hub run`은 JSON을 출력한다. `who`는 실제로 맡은 실행자, `tried`는 건너뛰거나 실패한 기록, `changed`는 이번 실행에서
새로 바뀐 git 파일이다. 로컬이 클라우드 대신 처리했다면 `needs_review: true`가 붙는다. 로컬 결과는 초안으로 보고 다시 검증한다.

## 한도 판정

| 실행자 | 조회 방법 | 막힌 것으로 보는 조건 |
|---|---|---|
| Claude Code | 구독 OAuth 사용량 API(`~/.claude/.credentials.json`) | 5시간 또는 주간 사용량 100% |
| Codex | `codex app-server`의 `account/rateLimits/read` | `ordinaryUsageAllowed`가 거짓. 크레딧은 `codex_use_credits` 또는 `HUB_CODEX_CREDITS=1`일 때만 쓴다 |
| 로컬 | `GET /v1/models` | 모델 이름이 없음 |

조회 결과는 60초 동안 캐시한다. 실행 중에 한도 오류를 받으면 초기화 시각까지(모르면 1시간) 그 실행자를 건너뛴다.
`HUB_SKIP=claude,codex`로 특정 실행자를 잠시 빼 둘 수 있다.

## 실행 방식

- Claude: `claude -p --output-format json --permission-mode auto`. 구현 계열 작업은 `--model sonnet`.
- Codex: `codex exec --json -o <파일>`. 샌드박스는 사용자 Codex 설정을 그대로 따른다.
- 로컬: `hermes -z`(Hermes 기본 프로필이 로컬 모델을 쓰도록 설정해 둔다).
- 하위 실행자 환경에서는 `ANTHROPIC_API_KEY`·`OPENAI_API_KEY`를 지운다. 구독 대신 API로 과금되는 일을 막기 위해서다.
  `HUB_DEPTH`를 넣어 하위 실행자 안에서 `hub run`을 다시 부르지 못하게 한다.
- 성공했거나 한도와 무관한 이유로 실패하면 거기서 멈추고 보고한다. 같은 일을 다른 모델에 되풀이하지 않는다.

## 동기화(`manifest.json`)

- `skills`: 도구별로 보여 줄 스킬 폴더. `all` 또는 `claude`·`codex`·`hermes`에 경로를 적는다. 이미 그 도구에 보이는
  원본은 건드리지 않고, 없는 것만 `~/.claude/skills`, `~/.agents/skills`(Codex), `~/.hermes/skills`에 심링크한다.
  같은 이름의 다른 사본이 있으면 중복을 피하려고 링크하지 않고 보고한다. 매니페스트에서 빠진 항목은 hub가 만든 링크만 지운다.
- `mcp`: 서버 정의와 `tools`. 각 도구의 CLI(`claude mcp`, `codex mcp`, `hermes mcp`)로 추가·교체한다.
  기존 정의에 env·헤더가 있으면 덮어쓰지 않는다.
- `plugins`: Claude 플러그인 ID와 Codex 플러그인 ID(필요하면 마켓플레이스 경로).
- `--apply`는 공통 지침 동기화 명령도 함께 실행한다. 한 원문을 `CLAUDE.md`, `AGENTS.md`, Hermes `SOUL.md`에 같은 블록으로 넣는다.

상태·실행 기록·백업은 `AGENT_HUB_STATE`(기본 `~/vault/agent-hub`)에 둔다. 실행 색인에는 프롬프트 원문을 넣지 않는다.

## 테스트

```bash
python3 -m unittest test_hub -v
```
