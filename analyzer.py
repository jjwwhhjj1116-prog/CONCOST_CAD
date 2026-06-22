"""
DXF 도면 분석 모듈 - 1단계: 읽기 및 기본 분류
"""
import math
import ezdxf
from ezdxf.enums import TextEntityAlignment

# ─────────────────────────────────────────────
# 레이어 분류 키워드
# ─────────────────────────────────────────────
LAYER_DELETE_KEYWORDS = [
    "가구", "furniture", "furn",
    "조경", "landscape", "tree", "plant",
    "차량", "car", "vehicle",
    "사람", "人", "figure",
    "타이틀", "title", "titleblock",
    "north", "방위", "나침",
    "stamp", "도장", "seal",
    "해치장식", "pattern",
]

LAYER_KEEP_KEYWORDS = [
    "wall", "벽", "w-", "_w_",
    "col", "기둥", "column",
    "beam", "보", "girder",
    "slab", "슬래브",
    "room", "실", "공간",
    "door", "문", "d-", "_d_",
    "window", "창", "w-", "_w_",
    "open", "개구",
    "finish", "마감", "fin",
    "center", "cl", "중심",
    "grid", "그리드", "축",
    "level", "레벨", "elev",
    "dim", "치수",
    "text", "문자",
]

WALL_CONC_KEYWORDS = ["rc", "conc", "콘크리트", "concrete", "철근", "라멘"]
WALL_DRY_KEYWORDS = ["gb", "gypsum", "stud", "석고", "경량", "dry", "lgs"]
WALL_MASONRY_KEYWORDS = ["block", "조적", "벽돌", "brick", "블럭", "masonry"]


def _layer_name_lower(layer_name: str) -> str:
    return layer_name.lower().replace(" ", "").replace("_", "").replace("-", "")


def classify_layer(layer_name: str) -> str:
    """레이어명 기준 분류 → 'delete' | 'keep' | 'unknown'"""
    nm = _layer_name_lower(layer_name)
    if nm in ("0", "defpoints", ""):
        return "unknown"
    for kw in LAYER_DELETE_KEYWORDS:
        if kw.lower() in nm:
            return "delete"
    for kw in LAYER_KEEP_KEYWORDS:
        if kw.lower() in nm:
            return "keep"
    return "unknown"


def classify_wall(layer_name: str, nearby_texts: list[str]) -> str:
    """벽체 레이어 + 인접 문자 기준으로 벽 종류 분류"""
    combined = _layer_name_lower(layer_name) + " ".join(t.lower() for t in nearby_texts)
    for kw in WALL_CONC_KEYWORDS:
        if kw in combined:
            return "CONC"
    for kw in WALL_DRY_KEYWORDS:
        if kw in combined:
            return "DRY"
    for kw in WALL_MASONRY_KEYWORDS:
        if kw in combined:
            return "MASONRY"
    return "UNKNOWN"


def _polyline_area(entity) -> float:
    """LWPOLYLINE closed 면적 (m²)"""
    try:
        pts = list(entity.get_points("xy"))
        if len(pts) < 3:
            return 0.0
        n = len(pts)
        area = 0.0
        for i in range(n):
            x1, y1 = pts[i]
            x2, y2 = pts[(i + 1) % n]
            area += x1 * y2 - x2 * y1
        return abs(area) / 2.0 / 1_000_000  # mm² → m²
    except Exception:
        return 0.0


def _line_length(entity) -> float:
    """LINE 엔티티 길이 (m)"""
    try:
        s = entity.dxf.start
        e = entity.dxf.end
        return math.hypot(e.x - s.x, e.y - s.y) / 1000  # mm → m
    except Exception:
        return 0.0


def _polyline_length(entity) -> float:
    """LWPOLYLINE 전체 길이 (m)"""
    try:
        pts = list(entity.get_points("xy"))
        length = 0.0
        for i in range(len(pts) - 1):
            x1, y1 = pts[i]
            x2, y2 = pts[i + 1]
            length += math.hypot(x2 - x1, y2 - y1)
        if entity.is_closed and len(pts) > 1:
            x1, y1 = pts[-1]
            x2, y2 = pts[0]
            length += math.hypot(x2 - x1, y2 - y1)
        return length / 1000  # mm → m
    except Exception:
        return 0.0


def _get_text_content(entity) -> str:
    try:
        if entity.dxftype() == "TEXT":
            return entity.dxf.text.strip()
        if entity.dxftype() == "MTEXT":
            return entity.plain_mtext().strip()
    except Exception:
        pass
    return ""


# ─────────────────────────────────────────────
# 메인 분석 함수
# ─────────────────────────────────────────────
def analyze_dxf(filepath: str, progress_cb=None) -> dict:
    def report(p, s):
        if progress_cb:
            progress_cb(int(p), s)

    report(15, "도면 로딩 중...")
    doc = ezdxf.readfile(filepath)
    msp = doc.modelspace()

    # 레이어 수집
    report(35, "레이어 분류 중...")
    layers_raw = {layer.dxf.name: layer for layer in doc.layers}

    layer_classify = {}
    for name in layers_raw:
        layer_classify[name] = classify_layer(name)

    # 객체 순회
    type_counts: dict[str, int] = {}
    texts: list[dict] = []

    lines: list[dict] = []
    polylines_closed: list[dict] = []
    polylines_open: list[dict] = []

    wall_candidates: list[dict] = []  # 벽 후보 레이어의 폴리라인

    used_layers: set[str] = set()

    report(55, "객체 순회 / 면적·길이 계산 중...")
    for e in msp:
        etype = e.dxftype()
        type_counts[etype] = type_counts.get(etype, 0) + 1
        layer = e.dxf.get("layer", "0")
        used_layers.add(layer)

        if etype == "LINE":
            length = _line_length(e)
            lines.append({"layer": layer, "length_m": round(length, 4)})

        elif etype == "LWPOLYLINE":
            is_closed = e.is_closed
            if is_closed:
                area = _polyline_area(e)
                polylines_closed.append({"layer": layer, "area_m2": round(area, 4)})
                # 벽 후보 체크
                lc = layer_classify.get(layer, classify_layer(layer))
                if lc == "keep" or any(
                    kw in layer.lower() for kw in ["wall", "벽", "w-"]
                ):
                    wall_candidates.append(
                        {"layer": layer, "area_m2": round(area, 4), "type": "CLOSED_POLY"}
                    )
            else:
                length = _polyline_length(e)
                polylines_open.append({"layer": layer, "length_m": round(length, 4)})

        elif etype in ("TEXT", "MTEXT"):
            content = _get_text_content(e)
            if content:
                texts.append({"layer": layer, "content": content})

    # 레이어 분류 집계
    keep_layers = [n for n, c in layer_classify.items() if c == "keep"]
    delete_layers = [n for n, c in layer_classify.items() if c == "delete"]
    unknown_layers = [n for n, c in layer_classify.items() if c == "unknown"]

    # 사용 중인 레이어만 unknown에 별도 표시
    unknown_used = [n for n in unknown_layers if n in used_layers]

    # 면적 요약
    total_closed_area = sum(p["area_m2"] for p in polylines_closed)
    total_line_length = sum(l["length_m"] for l in lines)
    total_poly_length = sum(p["length_m"] for p in polylines_open)

    # 레이어별 면적/길이 집계
    layer_area_summary: dict[str, float] = {}
    for p in polylines_closed:
        layer_area_summary[p["layer"]] = (
            layer_area_summary.get(p["layer"], 0) + p["area_m2"]
        )

    layer_length_summary: dict[str, float] = {}
    for l in lines:
        layer_length_summary[l["layer"]] = (
            layer_length_summary.get(l["layer"], 0) + l["length_m"]
        )
    for p in polylines_open:
        layer_length_summary[p["layer"]] = (
            layer_length_summary.get(p["layer"], 0) + p["length_m"]
        )

    report(90, "결과 정리 중...")
    return {
        "summary": {
            "total_layers": len(layers_raw),
            "total_entities": sum(type_counts.values()),
            "type_counts": type_counts,
            "total_closed_area_m2": round(total_closed_area, 4),
            "total_line_length_m": round(total_line_length, 4),
            "total_polyline_length_m": round(total_poly_length, 4),
            "text_count": len(texts),
        },
        "layer_classify": {
            "keep": sorted(keep_layers),
            "delete": sorted(delete_layers),
            "unknown": sorted(unknown_layers),
            "unknown_used": sorted(unknown_used),
        },
        "layer_area_summary": {
            k: round(v, 4)
            for k, v in sorted(layer_area_summary.items(), key=lambda x: -x[1])
        },
        "layer_length_summary": {
            k: round(v, 4)
            for k, v in sorted(layer_length_summary.items(), key=lambda x: -x[1])
        },
        "texts": texts[:200],  # 최대 200개
        "wall_candidates": wall_candidates,
        "review_items": [
            {
                "type": "UNKNOWN_LAYER",
                "layer": n,
                "message": f"레이어 '{n}'의 용도를 판단할 수 없습니다.",
            }
            for n in unknown_used
        ],
    }
