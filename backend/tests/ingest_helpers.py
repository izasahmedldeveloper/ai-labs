"""Builders for sample files and an in-memory VersionRepo so worker logic can be tested without Postgres."""
import io
import uuid
import zipfile

from studybuddy.ingestion.repo import VersionRecord, chunk_id

W_NS = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
A_NS = 'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main"'


def esc(t):
    return t.replace("&", "&amp;").replace("<", "&lt;")


def make_docx(items):
    """items: ("h1"|"h2"|"p", text) or ("table", [[cells]])"""
    body = []
    for kind, val in items:
        if kind in ("h1", "h2"):
            body.append(f'<w:p><w:pPr><w:pStyle w:val="Heading{kind[1]}"/></w:pPr><w:r><w:t>{esc(val)}</w:t></w:r></w:p>')
        elif kind == "p":
            body.append(f"<w:p><w:r><w:t>{esc(val)}</w:t></w:r></w:p>")
        else:
            rows = "".join("<w:tr>" + "".join(f"<w:tc><w:p><w:r><w:t>{esc(c)}</w:t></w:r></w:p></w:tc>" for c in row) + "</w:tr>" for row in val)
            body.append(f"<w:tbl>{rows}</w:tbl>")
    xml = f'<?xml version="1.0"?><w:document {W_NS}><w:body>{"".join(body)}</w:body></w:document>'
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("[Content_Types].xml", "<Types/>")
        z.writestr("word/document.xml", xml)
    return buf.getvalue()


def make_pptx(slides):
    """slides: list of (title, body, notes)"""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("[Content_Types].xml", "<Types/>")
        for i, (title, body, notes) in enumerate(slides, start=1):
            paras = "".join(f"<a:p><a:r><a:t>{esc(t)}</a:t></a:r></a:p>" for t in (title, body) if t)
            z.writestr(f"ppt/slides/slide{i}.xml", f'<?xml version="1.0"?><p:sld {A_NS}><a:txBody>{paras}</a:txBody></p:sld>')
            if notes:
                z.writestr(f"ppt/notesSlides/notesSlide{i}.xml", f'<?xml version="1.0"?><p:notes {A_NS}><a:p><a:r><a:t>{esc(notes)}</a:t></a:r></a:p></p:notes>')
    return buf.getvalue()


def make_pdf(pages, encrypt=None):
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas
    buf = io.BytesIO()
    kw = {}
    if encrypt:
        from reportlab.lib.pdfencrypt import StandardEncryption
        kw["encrypt"] = StandardEncryption(encrypt, canPrint=0)
    c = canvas.Canvas(buf, pagesize=A4, **kw)
    for lines in pages:
        y = 800
        for line in lines:
            c.drawString(50, y, line)
            y -= 16
        c.showPage()
    c.save()
    return buf.getvalue()


class MemRepo:
    """VersionRepo in memory with the same rules as PgVersionRepo.complete()."""

    def __init__(self):
        self.versions, self.documents, self.chunks, self.events, self.audits = {}, {}, {}, [], []
        self.fail_begin = False

    def add_version(self, tenant, course, doc, version_no, key, status="uploaded"):
        vid = str(uuid.uuid4())
        self.documents.setdefault((tenant, doc), {"course": course, "status": "active", "current": None})
        self.versions[(tenant, vid)] = {"doc": doc, "no": version_no, "key": key, "status": status, "attempts": 0, "progress": 0,
                                        "reason": None, "flagged": False, "retired": False}
        return vid

    def _rec(self, tenant, vid):
        v = self.versions.get((tenant, vid))
        if v is None:
            return None
        d = self.documents[(tenant, v["doc"])]
        return VersionRecord(tenant, d["course"], v["doc"], vid, v["no"], v["key"], v["status"], v["attempts"], d["status"], d["current"] == vid)

    async def load(self, tenant_id, version_id):
        return self._rec(tenant_id, version_id)

    async def begin(self, rec):
        v = self.versions[(rec.tenant_id, rec.version_id)]
        v["status"], v["attempts"], v["reason"] = "processing", v["attempts"] + 1, None
        return v["attempts"]

    async def progress(self, rec, percent):
        self.versions[(rec.tenant_id, rec.version_id)]["progress"] = percent

    async def fail(self, rec, reason):
        v = self.versions[(rec.tenant_id, rec.version_id)]
        if v["status"] != "ready":
            v["status"], v["reason"] = "failed", reason

    async def replace_chunks(self, rec, drafts, flagged, model):
        self.chunks[(rec.tenant_id, rec.version_id)] = [(chunk_id(rec.version_id, d.ordinal), d, d.ordinal in flagged) for d in drafts]

    async def complete(self, rec, *, flagged):
        v = self.versions[(rec.tenant_id, rec.version_id)]
        v["status"], v["progress"], v["flagged"] = "ready", 100, flagged
        d = self.documents[(rec.tenant_id, rec.document_id)]
        prev = d["current"]
        prev_no = self.versions[(rec.tenant_id, prev)]["no"] if prev else 0
        if d["status"] != "active" or (prev and prev != rec.version_id and prev_no >= v["no"]):
            return False, None
        d["current"] = rec.version_id
        if prev and prev != rec.version_id:
            self.versions[(rec.tenant_id, prev)]["retired"] = True
        self.events.append(("document.changed", rec.document_id, "replaced" if prev else "uploaded"))
        return True, prev

    async def audit(self, rec, action):
        self.audits.append(action)
