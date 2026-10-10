"""Word(.docx)·Excel(.xlsx) 문서 읽기와 분석 테스트 — 문서는 zipfile 로 직접 만듦 (추가 패키지 불필요)."""
import io
import json
import zipfile
from datetime import date, datetime, timedelta, timezone
from urllib.parse import quote
from xml.sax.saxutils import escape

import pytest

from test_todo_list import FakeVLLM, call, eml, run, upload, web  # noqa: F401  (web 은 fixture)
import todo_list

W = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
CORE = ('<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" '
        'xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:dcterms="http://purl.org/dc/terms/">{}</cp:coreProperties>')


def zipped(parts: dict, stamp=(1980, 1, 1, 0, 0, 0)) -> bytes:
    """Word 처럼 압축 항목 시각을 1980-01-01 로 (= 날짜 정보 없음)."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, text in parts.items():
            z.writestr(zipfile.ZipInfo(name, stamp), text)
    return buf.getvalue()


def core(title="", author="", modified=None):
    inner = (f"<dc:title>{escape(title)}</dc:title>" if title else "") + (f"<dc:creator>{escape(author)}</dc:creator>" if author else "")
    if modified:
        inner += f"<dcterms:modified>{modified.strftime('%Y-%m-%dT%H:%M:%SZ')}</dcterms:modified>"
    return CORE.format(inner)


def make_docx(paragraphs, table=None, **meta):
    p = lambda t: f"<w:p><w:r><w:t>{escape(t)}</w:t></w:r></w:p>"
    tbl = ""
    if table:
        tbl = "<w:tbl>" + "".join("<w:tr>" + "".join(f"<w:tc>{p(c)}</w:tc>" for c in row) + "</w:tr>" for row in table) + "</w:tbl>"
    doc = f"<w:document {W}><w:body>{''.join(p(t) for t in paragraphs)}{tbl}</w:body></w:document>"
    parts = {"word/document.xml": doc}
    if meta:
        parts["docProps/core.xml"] = core(**meta)
    return zipped(parts)


def serial(d: date) -> int:
    return (d - date(1899, 12, 30)).days


def make_xlsx(rows, sheet="일정", hidden_rows=None, **meta):
    """rows: 칸 값 목록. str → 공유 문자열, date → 날짜 서식 숫자, 숫자 → 숫자."""
    shared, xml_rows = [], []
    for r, row in enumerate(rows, 1):
        cells = []
        for c, v in enumerate(row):
            ref = f"{chr(65 + c)}{r}"
            if isinstance(v, str):
                shared.append(v)
                cells.append(f'<c r="{ref}" t="s"><v>{len(shared) - 1}</v></c>')
            elif isinstance(v, date):
                cells.append(f'<c r="{ref}" s="1"><v>{serial(v)}</v></c>')
            else:
                cells.append(f'<c r="{ref}"><v>{v}</v></c>')
        xml_rows.append(f'<row r="{r}">{"".join(cells)}</row>')
    ns = 'xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"'
    rns = 'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"'
    sheets = f'<sheet name="{sheet}" sheetId="1" r:id="rId1"/>' + ('<sheet name="숨김" sheetId="2" state="hidden" r:id="rId2"/>' if hidden_rows else "")
    parts = {
        "xl/workbook.xml": f"<workbook {ns} {rns}><sheets>{sheets}</sheets></workbook>",
        "xl/_rels/workbook.xml.rels": '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Target="worksheets/sheet1.xml"/><Relationship Id="rId2" Target="worksheets/sheet2.xml"/></Relationships>',
        "xl/sharedStrings.xml": f"<sst {ns}>" + "".join(f"<si><t>{escape(t)}</t></si>" for t in shared) + "</sst>",
        "xl/styles.xml": f'<styleSheet {ns}><numFmts count="1"><numFmt numFmtId="164" formatCode="yyyy\\-mm\\-dd"/></numFmts>'
            '<cellXfs count="2"><xf numFmtId="0"/><xf numFmtId="164"/></cellXfs></styleSheet>',
        "xl/worksheets/sheet1.xml": f"<worksheet {ns}><sheetData>{''.join(xml_rows)}</sheetData></worksheet>",
        "xl/worksheets/sheet2.xml": f'<worksheet {ns}><sheetData><row r="1"><c r="A1" t="inlineStr"><is><t>비밀 시트</t></is></c></row></sheetData></worksheet>',
    }
    if meta:
        parts["docProps/core.xml"] = core(**meta)
    return zipped(parts)


def test_read_docx_paragraphs_table_and_properties():
    mod = datetime(2026, 9, 30, 1, 0)
    data = make_docx(["3분기 업무 회의록", "액션 아이템은 아래 표 참고"],
                     [["담당", "할 일", "기한"], ["김대리", "견적서 작성", "2026-10-14"]], title="주간 회의록", author="박팀장", modified=mod)
    m = todo_list.read_doc("회의록.docx", data)
    assert m["kind"] == "docx" and m["subject"] == "주간 회의록" and m["sender"] == "박팀장"
    assert "3분기 업무 회의록" in m["body"] and "김대리 | 견적서 작성 | 2026-10-14" in m["body"]
    assert m["date"] == mod.replace(tzinfo=timezone.utc).astimezone().replace(tzinfo=None)   # UTC → 내 PC 시각
    # 속성이 없으면 파일 이름·브라우저가 준 수정 시각 사용
    m2 = todo_list.read_doc("폴더/메모.docx", make_docx(["내용"]), modified=datetime(2026, 10, 1, 9))
    assert m2["subject"] == "메모" and m2["date"] == datetime(2026, 10, 1, 9) and m2["sender"] == ""
    # 속성·브라우저 시각 모두 없으면 압축 항목 시각 (1980 은 무시)
    m3 = todo_list.read_doc("x.docx", zipped({"word/document.xml": f"<w:document {W}><w:body/></w:document>"}, stamp=(2026, 9, 1, 8, 0, 0)))
    assert m3["date"] == datetime(2026, 9, 1, 8, 0)
    assert todo_list.read_doc("y.docx", make_docx(["x"]))["date"] is None


def test_word_content_controls_relocated_part_and_docm():
    """회사 양식에 흔한 내용 컨트롤(sdt) 안의 글·표, 다른 위치의 본문, .docm 도 읽음."""
    doc = (f"<w:document {W}><w:body><w:sdt><w:sdtContent><w:p><w:r><w:t>회의록 제목</w:t></w:r></w:p>"
           "<w:tbl><w:tr><w:tc><w:p><w:r><w:t>김대리</w:t></w:r></w:p></w:tc><w:tc><w:p><w:r><w:t>계약 검토</w:t></w:r></w:p></w:tc></w:tr></w:tbl>"
           "</w:sdtContent></w:sdt><w:p><w:customXml><w:r><w:t>끝</w:t></w:r></w:customXml></w:p><w:sectPr/></w:body></w:document>")
    assert todo_list.read_doc("a.docx", zipped({"word/document.xml": doc}))["body"] == "회의록 제목\n김대리 | 계약 검토\n끝"
    rels = ('<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" '
            'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document2.xml"/></Relationships>')
    assert "계약 검토" in todo_list.read_doc("b.docx", zipped({"_rels/.rels": rels, "word/document2.xml": doc}))["body"]
    assert todo_list.read_doc("c.docm", zipped({"word/document.xml": doc}))["kind"] == "docx"


def test_read_xlsx_dates_numbers_and_hidden_sheet():
    data = make_xlsx([["업무", "담당", "기한", "진척"], ["견적 회신", "김대리", date(2026, 10, 14), 0.5], ["보고서", "이과장", date(2026, 10, 20), 1]],
                     hidden_rows=True, title="10월 일정표")
    m = todo_list.read_doc("일정.xlsx", data)
    assert m["kind"] == "xlsx" and m["subject"] == "10월 일정표"
    assert "[시트: 일정]" in m["body"] and "견적 회신 | 김대리 | 2026-10-14 | 0.5" in m["body"] and "보고서 | 이과장 | 2026-10-20 | 1" in m["body"]
    assert "비밀 시트" not in m["body"]                                     # 숨긴 시트는 읽지 않음


def test_bad_documents_are_rejected():
    with pytest.raises(ValueError):
        todo_list.read_doc("x.docx", b"not a zip")
    evil = zipped({"word/document.xml": '<!DOCTYPE x [<!ENTITY a "aaaa">]><w:document ' + W + '><w:body/></w:document>'})
    with pytest.raises(ValueError):
        todo_list.read_doc("x.docx", evil)                                 # DTD(엔티티 폭탄) 거부


def test_upload_and_analyze_documents(web):
    base, app = web
    now = datetime.now()
    docx = make_docx(["A사 견적 관련 회의"], [["담당", "할 일"], ["김대리", "견적 회신"]], title="A사 견적 회의록", author="박팀장", modified=now - timedelta(days=1))
    code, r = upload(base, "회의록.docx", docx)
    assert code == 200 and r["status"] == "ok" and r["kind"] == "docx" and r["subject"] == "A사 견적 회의록"
    # 문서 속성에 날짜가 없으면 브라우저의 파일 수정 시각(X-File-Modified, ms)
    xlsx = make_xlsx([["업무", "기한"], ["주간 보고", date.today()]])
    ms = int((now - timedelta(weeks=10)).timestamp() * 1000)
    code, r2 = call(base, "POST", "/api/upload", xlsx, {"X-File-Name": "%EB%B3%B4%EA%B3%A0.xlsx", "X-File-Modified": str(ms)})
    assert r2["status"] == "ok" and r2["date"].startswith((now - timedelta(weeks=10)).strftime("%Y-%m-%d"))
    assert call(base, "POST", "/api/upload", make_xlsx([["x"]]), {"X-File-Name": quote("날짜없음.xlsx")})[1]["status"] == "nodate"
    assert upload(base, "old.docx", make_docx(["x"], modified=now - timedelta(days=1000)))[1]["status"] == "old"
    bad = upload(base, "bad.docx", b"broken")[1]
    assert bad["status"] == "failed" and "손상" in bad["reason"]
    code, err = upload(base, "옛날.doc", b"x")
    assert code == 415 and ".docx" in err["error"]
    assert upload(base, "옛날.xls", b"x")[0] == 415 and upload(base, "그림.png", b"x")[0] == 415

    s = run(base, me="김대리", weeks=4)
    assert s["state"] == "done", s
    sent = "".join(c["messages"][-1]["content"] for c in FakeVLLM.chats)
    assert "Word 문서: 회의록.docx | 작성자: 박팀장" in sent and "김대리 | 견적 회신" in sent
    assert "Excel 문서: 보고.xlsx" in sent and "주간 보고 | " + date.today().isoformat() in sent   # 10주 전 문서도 기한 기준에 포함
    d = s["result"]["deadline"]
    assert [(t["title"], t["kind"]) for t in d["buckets"]["next"]] == [("견적 회신하기", "docx")]
    assert [(t["title"], t["kind"]) for t in d["buckets"]["today"]] == [("보고서 제출하기", "xlsx")]
    assert {m["kind"] for m in s["result"]["mails"]} == {"docx", "xlsx"}
    _, orig = call(base, "GET", f"/api/mails/{r['id']}")
    assert orig["kind"] == "docx" and "김대리 | 견적 회신" in orig["body"]
    # 빼기: 저장한 원본 파일도 지움
    path = app.store.mails[r["id"]]["path"]
    assert path.suffix == ".docx" and path.exists()
    call(base, "DELETE", f"/api/mails/{r['id']}")
    assert not path.exists()


def test_unanalyzed_files_are_reported_and_retried(web):
    base, _ = web
    now = datetime.now()
    ok = upload(base, "회의록.docx", make_docx(["A사 견적"], title="A사 견적 회의록", modified=now - timedelta(days=1)))[1]
    lost = upload(base, "누락.docx", make_docx(["내용"], title="누락되는 문서", modified=now - timedelta(days=1)))[1]
    far = upload(base, "예전.eml", eml("지난 봄 메일", now - timedelta(days=150)))[1]
    noti = upload(base, "noti.eml", eml("점검 알림", now - timedelta(days=1), sender="no-reply@sys.example"))[1]
    nodate = call(base, "POST", "/api/upload", make_xlsx([["x"]]), {"X-File-Name": quote("날짜없음.xlsx")})[1]
    s = run(base, me="김대리", weeks=4)
    assert s["state"] == "done", s
    sk = {x["id"]: (x["type"], x["why"]) for x in s["result"]["skipped"]}
    assert sk[lost["id"]] == ("failed", "LLM 응답에 결과가 없음")
    assert sk[far["id"]][0] == "range" and "최근 4주" in sk[far["id"]][1]
    assert sk[noti["id"]][0] == "noise" and sk[nodate["id"]][0] == "nodate"
    assert ok["id"] not in sk
    mails = {m["id"]: m for m in s["result"]["mails"]}
    assert mails[lost["id"]]["failed"] and not mails[ok["id"]]["failed"]
    extract = lambda word: [c for c in FakeVLLM.chats if c["messages"][0]["content"].startswith("업무 메일에서") and word in c["messages"][-1]["content"]]
    assert len(extract("누락되는 문서")) == 2                     # 묶음 요청 1번 + 혼자 다시 1번
    assert len(extract("A사 견적 회의록")) == 1
    # 실패한 것은 저장하지 않으므로 다음 분석 때 다시 시도, 성공한 것은 저장된 결과 사용
    run(base, me="김대리", weeks=4)
    assert len(extract("누락되는 문서")) == 4 and len(extract("A사 견적 회의록")) == 1
