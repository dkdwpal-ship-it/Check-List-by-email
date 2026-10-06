# Check-List-by-email

저장된 과거 메일(`.eml`)을 분석해 **이번 주 / 다음 주에 내가 해야 할 업무 체크리스트**를 만들어 주는 에이전트입니다.
LLM은 사내 on-premise **vLLM 서버(OpenAI 호환 API)** 를 사용하며 API Key가 필요 없습니다.

| 항목 | 기본값 | 변경 방법 |
|---|---|---|
| 서버 URL | `http://75.12.15.121:8000/v1` | `--base-url` 또는 환경변수 `LLM_BASE_URL` |
| 모델 | `thinkingcap` | `--model` 또는 환경변수 `LLM_MODEL` |
| API Key | 없음 (`EMPTY` 전송) | 필요 시 환경변수 `LLM_API_KEY` |

## 동작 방식

```
.eml 폴더 ──► ① 파싱 ──► ② 업무 추출(LLM, 배치) ──► ③ 중복 병합(LLM) ──► ④ 주차 분류(코드) ──► 체크리스트(MD/JSON)
```

1. **파싱** (`eml_parser.py`): 제목·발신·수신·참조·날짜·첨부·본문 추출. HTML 메일은 텍스트로 변환, EUC-KR/CP949 메일 지원,
   회신 메일의 인용 본문(`-----Original Message-----`, `On ... wrote:`, `>` 등) 제거, Message-ID 기준 중복 제거.
2. **업무 추출** (LLM): 메일을 `--batch-chars` 단위로 묶어 "사용자 본인이 해야 할 일"만 JSON으로 추출합니다.
   - "다음주 목요일까지" 같은 상대 기한을 **메일 발송일 기준**으로 날짜 환산
   - 내가 약속한 일("제가 수요일까지 공유드리겠습니다")도 포함, 단순 공지/타인 업무는 제외
   - 이후 메일에서 완료가 확인되면 `done` 처리
   - 과거 메일의 반복 패턴(매주 금요일 주간보고, 매월 말일 정산 등)을 **반복 업무**로 인식
3. **병합** (LLM): 배치가 여러 개일 때 같은 업무를 하나로 합치고 최신 마감일을 반영합니다.
4. **주차 분류** (코드, 결정적): 기준일의 이번 주(월~일)·다음 주로 나누고, 반복 업무는 해당 주의 날짜로 펼칩니다.
   기한이 지난 미완료 업무와 기한 미정 업무는 별도 섹션으로 보여줍니다.

LLM 응답은 vLLM의 guided decoding(`response_format: json_schema`)으로 스키마를 강제합니다. 서버가 이를 지원하지 않으면
자동으로 프롬프트 기반 JSON 출력으로 전환하며, `<think>...</think>` 추론 블록은 자동으로 제거합니다.

## 설치

```bash
pip install -r requirements.txt   # openai, pydantic
```

## 사용법

```bash
# 기본: 오늘 기준, 최근 8주 메일 분석, 화면 출력
python -m email_task_agent ./my_mails --me "김대리 <me@corp.example>"

# 기준일 지정 + 파일로 저장
python -m email_task_agent ./my_mails --me "김대리 <me@corp.example>" --date 2026-10-06 -o checklist.md

# JSON 출력 (다른 시스템 연동용)
python -m email_task_agent ./my_mails --format json -o checklist.json

# LLM 호출 없이 어떤 메일이 읽히는지만 확인
python -m email_task_agent ./my_mails --list-emails
```

주요 옵션

| 옵션 | 설명 |
|---|---|
| `--me` | 본인 이름/메일. 누구의 할 일인지 판단하는 데 사용 (지정 권장) |
| `--date` | 기준일 (기본: 오늘) |
| `--lookback-weeks` | 몇 주 전 메일까지 분석할지 (기본 8). 반복 업무 패턴을 잡으려면 4주 이상 권장 |
| `--batch-chars` | LLM 1회 호출에 넣을 메일 글자 수 (기본 24000). 모델 컨텍스트 길이에 맞춰 조정 |
| `--max-body-chars` | 메일 1건당 본문 최대 글자 수 (기본 6000) |
| `--max-tokens` | LLM 응답 최대 토큰 (기본 8192). 추론 모델이라 응답이 잘리면 늘려주세요 |
| `--no-json-schema` | guided decoding을 사용하지 않음 |
| `--include-done` | 완료된 업무도 표시 |
| `--keep-quotes` | 회신 인용 본문을 제거하지 않음 |

Outlook에서는 메일을 선택해 폴더로 드래그하거나 "다른 이름으로 저장"하면 `.eml`로 저장됩니다(`.msg`는 지원하지 않음).

## 출력 예시

```markdown
# 📋 메일 기반 주간 업무 체크리스트

- 기준일: 2026-10-06 (화)
- 이번 주: 10/5(월) ~ 10/11(일)
- 다음 주: 10/12(월) ~ 10/18(일)

## 이번 주 할 일 (2)

- [ ] 🔴 **Q3 실적 보고서 초안 송부** — 기한 10/8(목)
  - 요청: 이부장 · 원문 기한: “다음주 목요일까지” · 메일: Q3 실적 보고서 작성 요청
- [ ] 🟡 **주간보고 업로드** — 기한 10/9(금) 🔁 반복

## 다음 주 할 일 (2)

- [ ] 🟡 **단가표 수정본 회신** — 기한 10/14(수)
- [ ] 🟡 **주간보고 업로드** — 기한 10/16(금) 🔁 반복
```

## 코드에서 사용

```python
from datetime import date
from email_task_agent import EmailTaskAgent, LLMClient, load_emails
from email_task_agent.render import to_markdown

llm = LLMClient()  # http://75.12.15.121:8000/v1, thinkingcap
agent = EmailTaskAgent(llm, me="김대리 <me@corp.example>")
checklist, tasks = agent.run(load_emails("./my_mails"), today=date.today())
print(to_markdown(checklist))
```

## 데모 & 테스트

```bash
python samples/make_samples.py                       # samples/mails 에 예시 .eml 6건 생성
python -m email_task_agent samples/mails --date 2026-10-06 --me "김대리 <me@corp.example>"
python -m pytest -q                                  # 모의 vLLM 서버로 전체 파이프라인 테스트 (실서버 불필요)
```

## 구조

```
email_task_agent/
  eml_parser.py  # .eml 파싱 (인코딩/HTML/인용 처리)
  llm.py         # vLLM(OpenAI 호환) 클라이언트, 구조화 응답 + 재시도
  models.py      # Task / Recurrence / Checklist 모델
  agent.py       # 추출 → 병합 → 주차 분류 파이프라인, 프롬프트
  render.py      # Markdown / JSON 출력
  cli.py         # 명령행 인터페이스
```

## 참고

- 메일 본문은 사내 서버로만 전송되며 외부로 나가지 않습니다.
- 메일 본문 안의 지시문은 따르지 않도록 프롬프트에 명시되어 있지만, 결과는 LLM의 해석이므로 원문 메일로 확인하세요.
