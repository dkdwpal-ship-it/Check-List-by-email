import json
import re
from datetime import datetime

from email_task_agent.eml_parser import EmailRecord
from email_task_agent.export import dashboard_html


def test_dashboard_html_embeds_data_safely(tmp_path):
    mail = tmp_path / "a.eml"
    mail.write_text("From: x@y.z\nSubject: </script><script>alert(1)</script>\nDate: Mon, 5 Oct 2026 09:00:00 +0900\n\n"
                    "본문\n\n-----Original Message-----\n인용\n", encoding="utf-8")
    rec = EmailRecord(path=str(mail), message_id="m", subject="</script><script>alert(1)</script>", sender="x@y.z",
                      to=[], cc=[], date=datetime(2026, 10, 5, 9), body="본문")
    result = {"checklist": {"reference_date": "2026-10-08"}, "digest": [], "topics": {"mails": [], "keywords": {}}}
    html = dashboard_html(result, [rec])
    # 데이터 안의 '</script>' 가 스크립트를 끝내지 않도록 이스케이프됨
    blob = re.search(r"<script>window.__EXPORT__ = (.*?);</script>", html, re.S).group(1)
    assert "</script>" not in blob
    data = json.loads(blob.replace("<\\/", "</"))
    assert data["mails"]["M1"]["subject"].startswith("</script>")
    assert "Original Message" in data["mails"]["M1"]["body"]   # 원문은 인용 본문 포함
    assert "https://" not in html.replace("http://www.w3.org", "")  # 외부 리소스 없음
