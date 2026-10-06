# Check-List-by-email

저장된 과거 메일(`.eml`, Outlook `.msg`)을 분석해 **이번 주 / 다음 주에 내가 해야 할 업무 체크리스트**를 만들어 주는 에이전트입니다.
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

1. **파싱** (`eml_parser.py`, `msg_parser.py`): `.eml`과 Outlook `.msg`(대소문자 확장자 무관, 하위 폴더 포함)에서 제목·발신·수신·참조·날짜·첨부·본문 추출. HTML 메일은 텍스트로 변환, EUC-KR/CP949 메일 지원,
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
pip install -r requirements.txt   # openai, pydantic, olefile(.msg 읽기)
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

### 메일 파일 준비

| 메일 프로그램 | 저장 방법 | 형식 |
|---|---|---|
| Outlook (데스크톱) | 메일 여러 개 선택 → 탐색기 폴더로 끌어다 놓기, 또는 "다른 이름으로 저장" | `.msg` |
| 새 Outlook / Outlook 웹 | 메일 열기 → `…` → 다운로드 | `.eml` |
| Gmail / 네이버 / 다음 | 메일 열기 → 원문 보기/다운로드 | `.eml` |

`.pst`/`.ost`(Outlook 데이터 파일 전체)는 직접 읽을 수 없으니 위 방법으로 개별 메일을 저장하세요.

### 메일이 읽히지 않을 때

실행하면 먼저 아래처럼 몇 개의 파일을 찾았고 왜 제외했는지 보여줍니다. `--list-emails`로 LLM 호출 없이 확인할 수 있습니다.

```
분석 기간: 2026-08-11 ~ 2026-10-06
메일 파일 3개 발견 → 0건 사용
  - 기간 이전이라 제외: 2건 (가장 최근 2026-05-27), 분석 시작일 2026-08-11
  - 읽기 실패: mails/a.msg (NotOleFileError: not an OLE2 structured storage file)
  - 지원하지 않는 파일(무시): .pst 1개
[안내] 모든 메일이 분석 기간(최근 8주)보다 오래되었습니다. --lookback-weeks 값을 늘리거나 --date 로 기준일을 메일 시점에 맞추세요.
```

| 메시지 | 원인 / 조치 |
|---|---|
| `메일 파일 0개 발견` | 경로 확인. 폴더 경로에 공백이 있으면 `"C:\내 메일"`처럼 따옴표로 감싸기 |
| `기간 이전이라 제외` | 기본은 최근 8주 메일만 분석 → `--lookback-weeks 26` 등으로 늘리기 |
| `기준일 이후라 제외` | `--date`가 메일 날짜보다 과거로 지정됨 |
| `지원하지 않는 파일` | `.pst`, `.txt` 등은 무시됨 → `.eml`/`.msg`로 저장 |
| `읽기 실패` | 파일이 손상되었거나 확장자만 바뀐 파일 |

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
  eml_parser.py  # .eml 파싱 (인코딩/HTML/인용 처리), 폴더 로딩 + 제외 사유 리포트
  msg_parser.py  # Outlook .msg 파싱 (olefile)
  llm.py         # vLLM(OpenAI 호환) 클라이언트, 구조화 응답 + 재시도
  models.py      # Task / Recurrence / Checklist 모델
  agent.py       # 추출 → 병합 → 주차 분류 파이프라인, 프롬프트
  render.py      # Markdown / JSON 출력
  cli.py         # 명령행 인터페이스
```

## 참고

- 메일 본문은 사내 서버로만 전송되며 외부로 나가지 않습니다.
- 메일 본문 안의 지시문은 따르지 않도록 프롬프트에 명시되어 있지만, 결과는 LLM의 해석이므로 원문 메일로 확인하세요.
