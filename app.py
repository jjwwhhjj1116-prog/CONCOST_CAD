"""
건축 물량산출 자동화 - Flask 백엔드
1단계: DXF 분석 / 2.5단계: 블록 평탄화 / 2단계: 도면 정리 + QTO 레이어
진행률은 작업(job) 기반 폴링 방식으로 제공한다.
"""
import uuid
import threading
import traceback
from pathlib import Path
from flask import Flask, request, jsonify, render_template, send_file
from analyzer import analyze_dxf
from cleaner import build_cleaned_dxf, build_qto_dxf, build_classify_table
from flatten import flatten_dxf
from room_extract import extract_rooms

try:
    from converter import convert_dwg_to_dxf
    DWG_SUPPORTED = True
except Exception:
    DWG_SUPPORTED = False

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 100 * 1024 * 1024  # 100MB

UPLOAD_DIR = Path("uploads")
UPLOAD_DIR.mkdir(exist_ok=True)
OUTPUT_DIR = Path("outputs")
OUTPUT_DIR.mkdir(exist_ok=True)

# ─────────────────────────────────────────────
# 작업(job) 진행률 저장소
# JOBS[job_id] = {percent, stage, status, result, error}
# ─────────────────────────────────────────────
JOBS = {}
JOBS_LOCK = threading.Lock()


def _new_job() -> str:
    job_id = uuid.uuid4().hex
    with JOBS_LOCK:
        JOBS[job_id] = {
            "percent": 0, "stage": "대기 중...", "status": "running",
            "result": None, "error": None,
        }
    return job_id


def _update_job(job_id, percent=None, stage=None, status=None, result=None, error=None):
    with JOBS_LOCK:
        j = JOBS.get(job_id)
        if not j:
            return
        if percent is not None:
            j["percent"] = percent
        if stage is not None:
            j["stage"] = stage
        if status is not None:
            j["status"] = status
        if result is not None:
            j["result"] = result
        if error is not None:
            j["error"] = error


def _src_path(file_id: str) -> Path:
    safe = Path(file_id).name
    p = UPLOAD_DIR / f"{safe}.dxf"
    if not p.exists():
        raise FileNotFoundError("원본 파일을 찾을 수 없습니다. 분석을 다시 실행하세요.")
    return p


@app.after_request
def add_cors(resp):
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Methods"] = "POST, GET, OPTIONS"
    resp.headers["Access-Control-Allow-Headers"] = "*"
    return resp


@app.route("/")
def index():
    return render_template("index.html")


# ─────────────────────────────────────────────
# 진행률 조회 (공통)
# ─────────────────────────────────────────────
@app.route("/api/progress/<job_id>")
def progress(job_id):
    with JOBS_LOCK:
        j = JOBS.get(job_id)
        if not j:
            return jsonify({"error": "작업을 찾을 수 없습니다."}), 404
        return jsonify(dict(j))


# ─────────────────────────────────────────────
# 1단계: 분석 (비동기 작업)
# ─────────────────────────────────────────────
def _run_analyze(job_id, save_path, filename, file_id, is_dwg):
    try:
        dxf_path = str(save_path)

        # ── DWG면 먼저 DXF로 변환 (ZWCAD COM) ──
        if is_dwg:
            if not DWG_SUPPORTED:
                raise RuntimeError("DWG 변환 모듈을 사용할 수 없습니다 (pywin32/ZWCAD 확인).")
            dxf_out = str(UPLOAD_DIR / f"{file_id}.dxf")

            def conv_cb(p, s):
                # 변환은 전체 진행률의 0~30% 구간
                _update_job(job_id, int(p * 0.30), f"[DWG 변환] {s}")

            convert_dwg_to_dxf(dxf_path, dxf_out, progress_cb=conv_cb)
            dxf_path = dxf_out

        def cb(p, s):
            # 분석은 30~100% 구간으로 스케일
            scaled = 30 + int(p * 0.70) if is_dwg else p
            _update_job(job_id, scaled, s)

        result = analyze_dxf(dxf_path, progress_cb=cb)
        result["filename"] = filename
        result["file_id"] = file_id
        _update_job(job_id, 100, "분석 완료", status="done", result=result)
    except Exception as e:
        tb = traceback.format_exc()
        print("=" * 60, flush=True)
        print(f"[분석 실패] {filename}\n{tb}", flush=True)
        print("=" * 60, flush=True)
        _update_job(job_id, status="error", error=f"분석 실패: {str(e)}")


@app.route("/api/analyze", methods=["POST"])
def analyze():
    if "file" not in request.files:
        return jsonify({"error": "파일이 없습니다."}), 400
    f = request.files["file"]
    if not f.filename:
        return jsonify({"error": "파일명이 없습니다."}), 400

    ext = Path(f.filename).suffix.lower()
    if ext not in (".dxf", ".dwg"):
        return jsonify({"error": "DXF 또는 DWG 파일만 지원합니다."}), 400
    if ext == ".dwg" and not DWG_SUPPORTED:
        return jsonify({"error": "DWG 변환을 사용할 수 없습니다. DXF로 저장 후 업로드하세요."}), 400

    file_id = uuid.uuid4().hex
    is_dwg = ext == ".dwg"
    # DWG는 원본 그대로 저장(변환 전), DXF는 바로 저장
    save_path = UPLOAD_DIR / f"{file_id}{ext}"
    f.save(str(save_path))

    job_id = _new_job()
    threading.Thread(
        target=_run_analyze,
        args=(job_id, save_path, f.filename, file_id, is_dwg),
        daemon=True,
    ).start()
    return jsonify({"job_id": job_id})


# ─────────────────────────────────────────────
# 2단계 파이프라인: 평탄화 → 삭제정리 → QTO 생성 (비동기)
# ─────────────────────────────────────────────
def _run_clean(job_id, file_id, overrides):
    """2단계: 원본 구조 유지하며 삭제 레이어 정리 + QTO 골격 생성.
    평탄화는 2단계에서 하지 않는다 (3단계 벽체 추출의 내부 분석 단계로 분리)."""
    try:
        src = str(_src_path(file_id))

        # ── 도면 정리 (원본 블록 구조 유지) ──
        _update_job(job_id, 25, "삭제 후보 레이어 정리 중...")
        cleaned_out = OUTPUT_DIR / f"{file_id}_cleaned.dxf"
        clean_stats = build_cleaned_dxf(src, str(cleaned_out), overrides)

        # ── QTO 표준 레이어 골격 생성 ──
        _update_job(job_id, 60, "QTO 표준 레이어 생성 중...")
        qto_out = OUTPUT_DIR / f"{file_id}_qto.dxf"
        qto_stats = build_qto_dxf(src, str(qto_out), overrides)

        # ── 분류표 ──
        _update_job(job_id, 88, "레이어 분류표 생성 중...")
        table = build_classify_table(src, overrides)
        (OUTPUT_DIR / f"{file_id}_layers.csv").write_text(table, encoding="utf-8-sig")

        result = {
            "file_id": file_id,
            "clean": clean_stats,
            "qto": qto_stats,
            "downloads": {
                "cleaned": f"/api/download/{file_id}/cleaned",
                "qto": f"/api/download/{file_id}/qto",
                "table": f"/api/download/{file_id}/table",
            },
        }
        _update_job(job_id, 100, "도면 정리 완료", status="done", result=result)
    except FileNotFoundError as e:
        _update_job(job_id, status="error", error=str(e))
    except Exception as e:
        tb = traceback.format_exc()
        print("=" * 60, flush=True)
        print(f"[정리 실패] {file_id}\n{tb}", flush=True)
        print("=" * 60, flush=True)
        _update_job(job_id, status="error", error=f"정리 실패: {str(e)}")


@app.route("/api/clean", methods=["POST"])
def clean():
    data = request.get_json(silent=True) or {}
    file_id = data.get("file_id", "")
    overrides = data.get("overrides", {})
    if not isinstance(overrides, dict):
        overrides = {}

    if not file_id:
        return jsonify({"error": "file_id가 없습니다."}), 400

    job_id = _new_job()
    threading.Thread(
        target=_run_clean,
        args=(job_id, file_id, overrides),
        daemon=True,
    ).start()
    return jsonify({"job_id": job_id})


# ─────────────────────────────────────────────
# 다운로드
# ─────────────────────────────────────────────
# ─────────────────────────────────────────────
# 3단계: 실 추출 (작업도면 → 실명/면적/둘레)
# ─────────────────────────────────────────────
def _run_extract(job_id, save_path, filename, file_id, is_dwg):
    try:
        dxf_path = str(save_path)
        if is_dwg:
            if not DWG_SUPPORTED:
                raise RuntimeError("DWG 변환 모듈을 사용할 수 없습니다.")
            dxf_out = str(UPLOAD_DIR / f"{file_id}.dxf")

            def conv_cb(p, s):
                _update_job(job_id, int(p * 0.35), f"[DWG 변환] {s}")
            convert_dwg_to_dxf(dxf_path, dxf_out, progress_cb=conv_cb)
            dxf_path = dxf_out

        def cb(p, s):
            scaled = 35 + int(p * 0.65) if is_dwg else p
            _update_job(job_id, scaled, s)

        result = extract_rooms(dxf_path, progress_cb=cb)
        result["filename"] = filename
        result["file_id"] = file_id
        _update_job(job_id, 100, "실 추출 완료", status="done", result=result)
    except Exception as e:
        tb = traceback.format_exc()
        print("=" * 60, flush=True)
        print(f"[실 추출 실패] {filename}\n{tb}", flush=True)
        print("=" * 60, flush=True)
        _update_job(job_id, status="error", error=f"실 추출 실패: {str(e)}")


@app.route("/api/extract_rooms", methods=["POST"])
def extract_rooms_api():
    if "file" not in request.files:
        return jsonify({"error": "파일이 없습니다."}), 400
    f = request.files["file"]
    if not f.filename:
        return jsonify({"error": "파일명이 없습니다."}), 400
    ext = Path(f.filename).suffix.lower()
    if ext not in (".dxf", ".dwg"):
        return jsonify({"error": "DXF 또는 DWG 파일만 지원합니다."}), 400
    if ext == ".dwg" and not DWG_SUPPORTED:
        return jsonify({"error": "DWG 변환을 사용할 수 없습니다."}), 400

    file_id = uuid.uuid4().hex
    is_dwg = ext == ".dwg"
    save_path = UPLOAD_DIR / f"{file_id}{ext}"
    f.save(str(save_path))

    job_id = _new_job()
    threading.Thread(target=_run_extract,
                     args=(job_id, save_path, f.filename, file_id, is_dwg),
                     daemon=True).start()
    return jsonify({"job_id": job_id})


@app.route("/api/download/<file_id>/<kind>")
def download(file_id, kind):
    safe = Path(file_id).name
    mapping = {
        "flattened": (OUTPUT_DIR / f"{safe}_flattened.dxf", "flattened.dxf"),
        "cleaned": (OUTPUT_DIR / f"{safe}_cleaned.dxf", "cleaned.dxf"),
        "qto": (OUTPUT_DIR / f"{safe}_qto.dxf", "qto_layered.dxf"),
        "table": (OUTPUT_DIR / f"{safe}_layers.csv", "layer_classify.csv"),
    }
    if kind not in mapping:
        return jsonify({"error": "잘못된 다운로드 종류"}), 400
    path, dl_name = mapping[kind]
    if not path.exists():
        return jsonify({"error": "파일이 없습니다. 정리를 다시 실행하세요."}), 404
    return send_file(str(path), as_attachment=True, download_name=dl_name)


if __name__ == "__main__":
    app.run(debug=True, port=8765, threaded=True)
