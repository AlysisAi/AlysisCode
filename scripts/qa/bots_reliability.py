"""Explicit disposable Linux qualification; synthetic sign-in, real Chromium/Office.

No model calls or personal accounts. Browser encryption key stays in memory.
Only this study's owned computers are retired. Evidence is retained for review.
"""

from __future__ import annotations

import argparse
import json
import secrets
import shlex
import threading
import time
from pathlib import Path
from uuid import uuid4

from alysis_code.bots.browser import BrowserComputers
from alysis_code.bots.cloud_desktop import CloudDesktopFactory, CloudDesktopFiles
from alysis_code.bots.compute import ComputerSpec
from alysis_code.bots.compute_docker import DockerComputers
from alysis_code.bots.computer import ComputerViews
from alysis_code.bots.desktop import DesktopComputers
from alysis_code.bots.models import BotInput, TaskInput
from alysis_code.bots.object_storage import LocalObjectStorage
from alysis_code.bots.store import Store
from alysis_code.cancellation import EventCancellationToken

URL = "http://127.0.0.1:8767/"
# A local website fixture exercises HttpOnly cookies, localStorage and IndexedDB.
SITE = '''from http.server import BaseHTTPRequestHandler,HTTPServer
class Handler(BaseHTTPRequestHandler):
 def log_message(self,*args): pass
 def do_GET(self):
  self.send_response(200)
  if self.path=="/login":self.send_header("Set-Cookie","fixture=session; HttpOnly; SameSite=Strict; Max-Age=3600; Path=/")
  if self.path=="/logout":self.send_header("Set-Cookie","fixture=; Max-Age=0; Path=/")
  self.send_header("Content-Type","text/html; charset=utf-8");self.end_headers()
  cookie="present" if "fixture=session" in self.headers.get("Cookie","") else "absent"
  self.wfile.write(("""<meta charset=utf-8><title>Persistence fixture</title><a id=login href=/login>Sign in</a><a id=logout href=/logout>Sign out</a><p id=cookie>cookie COOKIE</p><p id=local></p><p id=indexed></p><script>
  const login=location.pathname==='/login',logout=location.pathname==='/logout';
  if(login)localStorage.setItem('fixture','stored');if(logout)localStorage.removeItem('fixture');
  document.getElementById('local').textContent='local '+(localStorage.getItem('fixture')||'absent');
  const db=indexedDB.open('fixture',1);db.onupgradeneeded=()=>db.result.createObjectStore('state');
  db.onsuccess=()=>{const tx=db.result.transaction('state',login||logout?'readwrite':'readonly'),s=tx.objectStore('state');if(login)s.put('stored','key');if(logout)s.delete('key');const get=s.get('key');get.onsuccess=()=>document.getElementById('indexed').textContent='indexed '+(get.result||'absent');};
  </script>""").replace('COOKIE',cookie).encode())
HTTPServer(('127.0.0.1',8767),Handler).serve_forever()
'''

OFFICE = """from pathlib import Path
from docx import Document
from openpyxl import Workbook
from pptx import Presentation
from pptx.chart.data import CategoryChartData
from pptx.enum.chart import XL_CHART_TYPE
from pptx.util import Inches
from reportlab.pdfgen import canvas
d=Document();d.add_heading('Synthetic report',0);d.add_paragraph('Athens and Tokyo — actual generated file.');d.save('report.docx')
w=Workbook();w.active.append(['Amount','Total']);w.active.append([17, '=SUM(A2:A3)']);w.active.append([42]);w.save('stale.xlsx')
p=Presentation();s=p.slides.add_slide(p.slide_layouts[5]);s.shapes.title.text='Synthetic chart'
c=CategoryChartData();c.categories=['Athens','Tokyo'];c.add_series('Amount',[17,42]);s.shapes.add_chart(XL_CHART_TYPE.COLUMN_CLUSTERED, Inches(1),Inches(1.5), Inches(8), Inches(4), c);p.save('chart.pptx')
pdf=canvas.Canvas('report.pdf');pdf.drawString(60,760,'Synthetic PDF report');pdf.save()
Path('broken.docx').write_bytes(b'not an Office package')
"""

REPAIR_CHART = """from zipfile import ZipFile
from lxml import etree
with ZipFile('chart.pptx') as src,ZipFile('compatible.pptx','w') as dst:
 for entry in src.infolist():
  data=src.read(entry.filename)
  if entry.filename.startswith('ppt/charts/') and entry.filename.endswith('.xml'):
   tree=etree.fromstring(data);nodes=tree.xpath('//*[local-name()="axId" or local-name()="crossAx"]');ids={v:i+1 for i,v in enumerate(dict.fromkeys(n.get('val') for n in nodes))}
   for node in nodes:node.set('val',str(ids[node.get('val')]))
   data=etree.tostring(tree,xml_declaration=True,encoding='UTF-8',standalone=True)
  dst.writestr(entry,data)
"""


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--files-only", action="store_true")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[2]
    output = root / ".dev" / ("bots-reliability-" + uuid4().hex[:12])
    output.mkdir(parents=True, mode=0o700)
    provider = DockerComputers(
        "desktop-linux", namespace_profile=root / "scripts/cloud-dev/seccomp.json"
    )
    spec = ComputerSpec("alysis-bots-reliability-20261103:qa", cpu=2, memory_mib=4096)
    objects = LocalObjectStorage(output / "objects")
    store = Store(output / "bots.db", objects=objects)
    key = secrets.token_bytes(32)
    owner = "reliability-study"
    created = []
    evidence = {"passed": False, "model_calls": 0, "output": str(output)}

    class Factory(CloudDesktopFactory):
        def _acquire(self, *args):
            surface = super()._acquire(*args)
            if surface.ref not in created:
                created.append(surface.ref)
                data = SITE.encode()
                provider.upload(
                    surface.ref, "/tmp/persistence-site.py", iter([data]), size=len(data)
                )
                setup = "import subprocess;subprocess.Popen(['/opt/alysis/.venv/bin/python','-I','/tmp/persistence-site.py'],stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,start_new_session=True)"
                assert (
                    provider.run(
                        surface.ref, ["/opt/alysis/.venv/bin/python", "-I", "-c", setup], timeout=15
                    ).exit_code
                    == 0
                )
            return surface

    def managers():
        factory = Factory(
            owner,
            provider,
            spec,
            output / "leases",
            browser_enabled=True,
            preview_urls=(URL,),
            store=store,
            browser_state_key=key,
        )
        views = ComputerViews()
        return BrowserComputers(owner, views, factory.browser, store=store), DesktopComputers(
            owner, views, factory=factory, store=store, file_transfer=CloudDesktopFiles()
        )

    bot = store.save_bot(owner, BotInput(name="Persistence", purpose="Synthetic Linux checks"))
    chat = store.create_chat(owner, bot["id"], "Checks")
    token = EventCancellationToken(threading.Event())
    browser, desktop = managers()

    def new_task():
        store.enqueue(
            owner, chat["id"], TaskInput(message="Synthetic qualification", request_id=uuid4().hex)
        )
        return store.claim(owner)

    def web(task, name, **args):
        return browser.call(task, name, args, token, approved_origins=(URL,), image_input=False)

    def observe(task, expected):
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            view = web(task, "browser_observe")
            text = json.dumps(view)
            if all(value in text for value in expected):
                return view
            time.sleep(0.1)
        raise AssertionError("Browser state did not match the synthetic fixture")

    def click(task, view, name):
        node = next(
            n["node"]
            for n in view["dom"]["documents"][0]["nodes"]
            if n.get("attributes", {}).get("id") == name
        )
        return web(task, "browser_click", source_id=view["source_id"], node=node)

    def finish(task, cancelled=False):
        browser.release(task["id"], cancelled=cancelled)
        desktop.release(task["id"], cancelled=cancelled)
        store.finish(
            owner, task["id"], "cancelled" if cancelled else "completed", content="Synthetic checks"
        )

    try:
        if not args.files_only:
            task = new_task()
            view = web(task, "browser_navigate", url=URL)
            click(task, view, "login")
            web(task, "browser_navigate", url=URL)
            observe(task, ["cookie present", "local stored", "indexed stored"])
            isolated = desktop.call(
                task,
                "computer_run",
                {
                    "command": "test ! -e /tmp/persistence-site.py && test ! -e /tmp/alysis-cloud-* && printf isolated",
                    "timeout": 10,
                },
                token,
                approved=True,
            )
            assert isolated["exit_code"] == 0, isolated
            finish(task)
            browser.close()
            desktop.close()
            # Recreate controller objects and the VM, retaining only encrypted storage/key.
            store = Store(output / "bots.db", objects=objects)
            browser, desktop = managers()
            task = new_task()
            web(task, "browser_navigate", url=URL)
            view = observe(task, ["cookie present", "local stored", "indexed stored"])
            evidence["browser_restart_cookie_localstorage_indexeddb"] = True
            click(task, view, "logout")
            finish(task, cancelled=True)
            task = new_task()
            web(task, "browser_navigate", url=URL)
            view = observe(task, ["cookie present", "local stored", "indexed stored"])
            evidence["stop_retains_previous_checkpoint"] = True
            click(task, view, "logout")
            finish(task)
            task = new_task()
            web(task, "browser_navigate", url=URL)
            observe(task, ["cookie absent", "local absent", "indexed absent"])
            evidence["logout_persists"] = True
        else:
            task = new_task()
        command = "python - <<'PY'\n" + OFFICE + "\nPY"
        result = desktop.call(
            task, "computer_run", {"command": command, "timeout": 30}, token, approved=True
        )
        assert result["exit_code"] == 0, result
        files = {}
        evidence["files"] = files
        for name in ("report.docx", "report.pdf", "chart.pptx", "stale.xlsx", "broken.docx"):
            result = desktop.call(task, "computer_save_file", {"name": name}, token, approved=True)
            files[name] = result
        if not files["report.docx"]["saved"]:
            evidence["renderer_diagnostic"] = desktop.call(
                task,
                "computer_run",
                {
                    "command": "mkdir -p render-debug; libreoffice -env:UserInstallation=file:///tmp/render-debug-profile --headless --convert-to pdf --outdir render-debug report.docx",
                    "timeout": 30,
                },
                token,
                approved=True,
            )
        assert files["report.docx"]["saved"] and files["report.pdf"]["saved"]
        assert (
            not files["chart.pptx"]["saved"]
            and not files["stale.xlsx"]["saved"]
            and not files["broken.docx"]["saved"]
        )
        result = desktop.call(
            task,
            "computer_run",
            {
                "command": "python - <<'PY'\n"
                + REPAIR_CHART
                + "\nPY\npython -m alysis_code.office recalculate --source stale.xlsx --output calculated.xlsx",
                "timeout": 30,
            },
            token,
            approved=True,
        )
        assert result["exit_code"] == 0, result
        evidence["repair_command"] = result
        files["compatible.pptx"] = desktop.call(
            task, "computer_save_file", {"name": "compatible.pptx"}, token, approved=True
        )
        assert files["compatible.pptx"]["saved"]
        files["calculated.xlsx"] = desktop.call(
            task, "computer_save_file", {"name": "calculated.xlsx"}, token, approved=True
        )
        assert files["calculated.xlsx"]["saved"]
        chart_pdf = files["compatible.pptx"]["validation"]["preview"]["pdf"]
        chart_check = (
            "import pypdfium2;from pptx import Presentation;pdf=pypdfium2.PdfDocument("
            + repr(chart_pdf)
            + ");text=''.join(page.get_textpage().get_text_range() for page in pdf);labels=[str(category.label) for slide in Presentation('compatible.pptx').slides for shape in slide.shapes if shape.has_chart for category in shape.chart.plots[0].categories];assert labels and all(label in text for label in labels), 'Chart labels disappeared during rendering';print('Chart labels verified against the source chart')"
        )
        chart_result = desktop.call(
            task,
            "computer_run",
            {"command": "python -c " + shlex.quote(chart_check), "timeout": 10},
            token,
            approved=True,
        )
        assert chart_result["exit_code"] == 0, chart_result
        evidence["chart_labels_match_source"] = True
        for name, result in list(files.items()):
            if not result["saved"]:
                continue
            for index, page in enumerate(result["validation"]["preview"]["pages"]):
                target = name + f"-page-{index + 1}.png"
                copied = desktop.call(
                    task,
                    "computer_run",
                    {
                        "command": "cp -- "
                        + shlex.quote(page["image"])
                        + " "
                        + shlex.quote(target),
                        "timeout": 10,
                    },
                    token,
                    approved=True,
                )
                assert copied["exit_code"] == 0, copied
                saved = desktop.call(
                    task, "computer_save_file", {"name": target}, token, approved=True
                )
                (output / target).write_bytes(store.file_data(owner, bot["id"], saved["id"]))
        evidence["files"] = files
        finish(task)
        evidence["passed"] = True
    finally:
        browser.close()
        desktop.close()
        evidence["computers"] = [
            {"id": ref.resource_id, "state": provider.status(ref).state} for ref in created
        ]
        (output / "receipt.json").write_text(json.dumps(evidence, ensure_ascii=False, indent=2))
        print(
            json.dumps({k: v for k, v in evidence.items() if k not in {"files", "repair_command"}})
        )


if __name__ == "__main__":
    main()
