import os
import re
import time
import uuid
import shutil
import threading

import fitz
from pyzbar.pyzbar import decode
from PIL import Image
from pypdf import PdfReader, PdfWriter

from flask import (
    Flask, render_template, request, redirect, url_for,
    flash, send_file, abort
)

# ============================ НАСТРОЙКИ ============================

app = Flask(__name__)
app.secret_key = "change-me"
app.config["MAX_CONTENT_LENGTH"] = 20 * 1024 * 1024  # 20 МБ

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
RESULTS_DIR = os.path.join(BASE_DIR, "results")
os.makedirs(RESULTS_DIR, exist_ok=True)

JOB_TTL = 60 * 60          # сколько хранить результаты (сек)
CLEANUP_INTERVAL = 5 * 60  # как часто чистить (сек)
PDF_MAGIC = b"%PDF-"

# ======================= ТВОЯ ЛОГИКА РАЗБОРА PDF =======================

ZOOM = 3
SKU_RE = re.compile(r'^[0O]ZN\d{8,}$')


def analyze_page(page, zoom=ZOOM):
    pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom))
    img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
    has_qr = has_bar = False
    for c in decode(img):
        if c.type == "QRCODE":
            has_qr = True
        else:
            has_bar = True
    return has_qr, has_bar


def extract_article(fitz_page):
    lines = [l.strip() for l in (fitz_page.get_text() or "").splitlines() if l.strip()]
    for i, line in enumerate(lines):
        if SKU_RE.match(line) and i + 1 < len(lines):
            return lines[i + 1]
    if len(lines) >= 2:
        return lines[-2]
    return lines[0] if lines else None


def sanitize(name):
    for ch in '<>:"/\\|?*\n\r\t':
        name = name.replace(ch, "_")
    return name.strip().strip(".") or "article"


def split_pdf_by_article(input_path, output_dir=None, zoom=ZOOM, debug=False):
    if output_dir is None:
        output_dir = os.path.dirname(os.path.abspath(input_path)) or "."
    os.makedirs(output_dir, exist_ok=True)

    fitz_doc = fitz.open(input_path)
    n = len(fitz_doc)

    types = []
    for page in fitz_doc:
        qr, bar = analyze_page(page, zoom=zoom)
        types.append("qr" if qr and not bar else
                     "bar" if bar and not qr else
                     "both" if qr and bar else "none")

    ranges = []
    i = 0
    while i < n:
        if types[i] not in ("qr", "both"):
            i += 1
            continue
        start = i
        j = i + 1
        while j < n and types[j] in ("bar", "both"):
            j += 1
        if j > start + 1:
            art = extract_article(fitz_doc[start + 1]) or f"article_{start+1}"
            ranges.append((art, list(range(start, j))))
        i = j

    art_pages, order = {}, []
    for art, idxs in ranges:
        if art not in art_pages:
            art_pages[art] = set()
            order.append(art)
        art_pages[art].update(idxs)

    reader = PdfReader(input_path)
    outputs = []
    for art in order:
        out_path = os.path.join(output_dir, f"{sanitize(art)}.pdf")
        base, k = out_path, 1
        while os.path.exists(out_path):
            out_path = f"{base[:-4]}_{k}.pdf"
            k += 1
        w = PdfWriter()
        for idx in sorted(art_pages[art]):
            w.add_page(reader.pages[idx])
        with open(out_path, "wb") as f:
            w.write(f)
        outputs.append(out_path)

    if debug:
        print("Распределение:")
        for art in order:
            print(f"  {art}: {[i+1 for i in sorted(art_pages[art])]}")

    return outputs


# ==== ЭТУ ОБЁРТКУ ВЫЗЫВАЕТ FLASK: принимает путь + папку, отдаёт список ====

def my_function(input_pdf_path: str, output_dir: str) -> list[str]:
    return split_pdf_by_article(input_pdf_path, output_dir=output_dir, debug=False)

# ============================ ФОНОВАЯ УБОРКА ============================

def cleanup_old_jobs():
    now = time.time()
    try:
        names = os.listdir(RESULTS_DIR)
    except OSError:
        return
    for name in names:
        path = os.path.join(RESULTS_DIR, name)
        if not os.path.isdir(path):
            continue
        try:
            if now - os.path.getmtime(path) > JOB_TTL:
                shutil.rmtree(path, ignore_errors=True)
        except OSError:
            pass


def _cleanup_loop():
    cleanup_old_jobs()
    while True:
        time.sleep(CLEANUP_INTERVAL)
        cleanup_old_jobs()


_thread_lock = threading.Lock()
_thread_started = False


def ensure_cleanup_thread():
    global _thread_started
    with _thread_lock:
        if _thread_started:
            return
        _thread_started = True
        threading.Thread(target=_cleanup_loop, name="cleanup", daemon=True).start()


@app.before_request
def _start_cleanup_once():
    ensure_cleanup_thread()

# ============================ ВСПОМОГАТЕЛЬНОЕ ============================

def is_pdf(file_storage) -> tuple[bool, str]:
    filename = file_storage.filename or ""
    if not filename.lower().endswith(".pdf"):
        return False, "Файл должен иметь расширение .pdf"
    head = file_storage.stream.read(5)
    file_storage.stream.seek(0)
    if head != PDF_MAGIC:
        return False, "Файл не является PDF"
    return True, ""


def list_job_files(job_id: str) -> list[str]:
    out_dir = os.path.join(RESULTS_DIR, job_id, "output")
    if not os.path.isdir(out_dir):
        return []
    return sorted(
        n for n in os.listdir(out_dir)
        if n.lower().endswith(".pdf") and os.path.isfile(os.path.join(out_dir, n))
    )

# ============================ РОУТЫ ============================

@app.route("/", methods=["GET", "POST"])
def index():
    if request.method == "POST":
        file = request.files.get("file")
        if not file or file.filename == "":
            flash("Файл не выбран", "error")
            return redirect(url_for("index"))

        ok, err = is_pdf(file)
        if not ok:
            flash(err, "error")
            return redirect(url_for("index"))

        job_id = uuid.uuid4().hex
        job_dir = os.path.join(RESULTS_DIR, job_id)
        out_dir = os.path.join(job_dir, "output")
        os.makedirs(out_dir, exist_ok=True)

        input_path = os.path.join(job_dir, "input.pdf")
        file.save(input_path)

        try:
            results = my_function(input_path, out_dir)
        except Exception as e:
            app.logger.exception("Ошибка обработки")
            shutil.rmtree(job_dir, ignore_errors=True)
            flash(f"Ошибка обработки: {e}", "error")
            return redirect(url_for("index"))
        finally:
            try:
                os.remove(input_path)
            except OSError:
                pass

        files = list_job_files(job_id)
        if not files:
            shutil.rmtree(job_dir, ignore_errors=True)
            flash("Функция не вернула ни одного файла", "error")
            return redirect(url_for("index"))

        return redirect(url_for("result", job_id=job_id))

    return render_template("index.html")


@app.route("/result/<job_id>")
def result(job_id):
    files = list_job_files(job_id)
    if not files:
        flash("Результаты не найдены или срок их хранения истёк", "error")
        return redirect(url_for("index"))
    return render_template("result.html", job_id=job_id, files=files)


@app.route("/download/<job_id>/<path:filename>")
def download(job_id, filename):
    out_dir = os.path.realpath(os.path.join(RESULTS_DIR, job_id, "output"))
    if not os.path.isdir(out_dir):
        abort(404)

    safe_name = os.path.basename(filename)
    target = os.path.realpath(os.path.join(out_dir, safe_name))
    if not target.startswith(out_dir + os.sep) or not os.path.isfile(target):
        abort(404)

    try:
        os.utime(os.path.join(RESULTS_DIR, job_id), None)
    except OSError:
        pass

    return send_file(
        target,
        mimetype="application/pdf",
        as_attachment=True,
        download_name=safe_name,
    )

@app.route("/delete/<job_id>", methods=["POST"])
def delete_result(job_id):
    results_root = os.path.realpath(RESULTS_DIR)
    job_dir = os.path.realpath(os.path.join(RESULTS_DIR, job_id))

    # защита от path traversal: папка должна лежать строго внутри results/
    if not job_dir.startswith(results_root + os.sep):
        abort(404)

    if not os.path.isdir(job_dir):
        flash("Результаты уже удалены или не найдены", "error")
        return redirect(url_for("index"))

    shutil.rmtree(job_dir, ignore_errors=True)
    flash("Результаты удалены", "success")
    return redirect(url_for("index"))


if __name__ == "__main__":
    app.run(debug=True)