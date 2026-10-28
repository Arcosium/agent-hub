---
name: hub
description: >-
  Claude Code·Codex·로컬 모델(Hermes, arc-local) 사이에서 일을 나누고, 구독 한도가 떨어지면
  다른 쪽으로 넘긴다. 내 한도가 바닥났거나 한도 오류를 받았을 때, 긴 로그·문서 요약이나 파일 찾기를
  로컬에 맡겨 토큰을 아끼고 싶을 때, 범위가 정해진 구현·테스트를 Codex에, 설계·검토·한국어 문서를
  Claude에 넘길 때, 구현을 마치고 다른 모델에게 교차 검토를 받을 때 사용한다.
  "hub", "쿼터", "한도", "넘겨", "위임해", "교차 검토", "Codex한테 검증" 요청에도 쓴다.
---

# hub

명령은 `hub`(`~/projects/agent-hub/hub.py`)이다. 결과는 JSON으로 나온다.

```bash
hub status                                  # claude·codex·local 한도와 검토 대기 목록
hub run --kind implement "작업 설명"          # 잘하는 쪽에 맡기고, 한도가 막히면 다음으로 넘긴다
hub run --kind summarize "이 로그 요약: <경로>"
hub review 바꾼파일1 바꾼파일2                  # 커밋 전 변경을 다른 모델이 한 번 검토(작업 폴더에서 실행)
hub open ["첫 요청"]                          # 사람이 쓸 대화형 도구를 한도 순서(claude → codex → hermes)로 띄운다
hub sync                                    # 스킬·MCP·플러그인 변경 계획 보기(--apply 로 반영)
```

종류와 맡는 순서:

| kind | 순서 |
|---|---|
| design, review, docs, debug | claude → codex → local |
| implement, test, fix | codex → claude → local |
| summarize, explore | local → codex → claude |

`--kind`를 빼면 키워드와 로컬 모델이 고른다. `--to claude|codex|local`은 대체 없이 그쪽만 쓴다.

## 언제 쓰나

- 내 한도가 바닥났거나 한도 오류를 받았다: 남은 일을 `hub run`으로 넘긴다.
- 50KB가 넘는 로그·문서 요약, 파일·심볼 위치 찾기, 단순 분류: `--kind summarize` 또는 `explore`.
  로컬이 먼저 하고, 실패하면 클라우드로 올라간다.
- 파일과 완료 기준이 분명한 구현·테스트: `--kind implement` 또는 `test`. 작업 설명에 파일, 완료 기준, 검사 명령을 적는다.

## 교차 검토(`hub review`)

- 여러 파일에 걸친 구현·수정, 버그 수정, 보안·데이터 경로 변경을 마쳤으면 완료 보고 전에 한 번 실행한다.
  Claude 세션(`CLAUDECODE`)이면 Codex가, 그 밖이면 Claude가 검토한다. 검토자 한도가 없으면 다른 클라우드, 그다음 로컬이다.
- 검토는 커밋하지 않은 git 변경(`git diff HEAD`, 새 파일)을 본다. 커밋하기 전에, 이번 작업에서 바꾼 파일을 인자로 넘겨 부른다.
  오래 커밋하지 않은 저장소가 많아서 파일 없이 부르면 변경이 30개를 넘을 때 거절한다.
- 보통 20~60초 걸린다. Claude Code는 Bash `timeout`을 600000으로 주고, Codex는 JSON 결과가 나올 때까지 기다린다.
- 지적은 원문 코드로 확인해 맞는 것만 고치고 검사를 다시 돌린다. 고친 뒤 `hub review`를 되풀이하지 않는다.
- 몇 줄짜리 수정과 문서만 바꾼 작업은 생략한다. 검토자가 파일을 고쳤다면 `changed`에 나온다.

## 쓰지 말 것

- 몇 줄짜리 수정이나 이미 읽은 파일에 대한 질문은 직접 처리한다. 클라우드 호출 한 번의 기본 입력이 약 4만 토큰이다.
- hub로 실행된 작업 안에서 다시 `hub run`을 부르지 않는다(`HUB_DEPTH`가 막는다).

## 결과 확인

- `answer`는 실행자의 마지막 메시지, `changed`는 git 변경 파일, `log`는 전체 기록 경로다.
- 바뀐 파일은 직접 읽고 검사 명령을 돌려 확인한다. 완료 보고만으로 끝났다고 판단하지 않는다.
- `who: local`이고 `needs_review: true`면 클라우드 한도가 없어 로컬이 대신한 결과다. 초안으로 보고
  수치·경로·호출 관계를 원문으로 다시 확인한다.
