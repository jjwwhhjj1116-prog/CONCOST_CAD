"""
실 추출 모듈 (내부 마감 적산)
사람이 작업한 '내부 마감 작업 도면'에서 실 경계 폴리라인 + 실명을 매칭하여
실별 [실코드 / 실명 / 면적(A) / 둘레(L)] 을 자동 추출한다.
검증: 안성 MAAC 100-A-평면도 → 348실 중 94% 식별.
"""
import re
import math
import collections
import ezdxf

# 실 경계/실명이 들어있는 레이어 후보 키워드
ROOM_LAYER_HINTS = ["room-iden", "room_iden", "room-name", "실명", "실_", "실구획", "면적"]


def _poly_area_perim(pts):
    n = len(pts)
    a = 0.0
    L = 0.0
    for i in range(n):
        x1, y1 = pts[i]
        x2, y2 = pts[(i + 1) % n]
        a += x1 * y2 - x2 * y1
        L += math.hypot(x2 - x1, y2 - y1)
    return abs(a) / 2.0 / 1_000_000.0, L / 1000.0  # m², m


def _point_in_poly(x, y, pts):
    inside = False
    n = len(pts)
    j = n - 1
    for i in range(n):
        xi, yi = pts[i]
        xj, yj = pts[j]
        if ((yi > y) != (yj > y)) and (x < (xj - xi) * (y - yi) / (yj - yi + 1e-12) + xi):
            inside = not inside
        j = i
    return inside


_DIM = re.compile(r'^[\d,\.\s\-]+$')
_CODE = re.compile(r'^[A-Za-z]{1,2}\d{2,4}(~\d{2,4})?$')


def _is_dim(t):
    return bool(_DIM.match(t))


def _is_label(t):
    t = t.replace(" ", "")
    return t.startswith("A:") or t.startswith("L:")


def _classify_texts(texts):
    """실 내부 텍스트들에서 실코드/실명 분리"""
    code = None
    name = None
    for t in texts:
        if _is_dim(t) or _is_label(t):
            continue
        if _CODE.match(t):
            code = code or t
        elif re.search(r'[가-힣A-Za-z]', t):
            name = name or t
    return code, name


def _detect_room_layers(msp):
    """실 경계 레이어 자동 탐지: 키워드 우선, 없으면 실 범위 닫힌폴리 최다 레이어"""
    # 키워드 매칭 레이어
    hinted = set()
    poly_by_layer = collections.Counter()
    for e in msp:
        if e.dxftype() == "LWPOLYLINE" and e.is_closed:
            ly = e.dxf.get("layer", "0")
            poly_by_layer[ly] += 1
            low = ly.lower()
            if any(h in low for h in ROOM_LAYER_HINTS):
                hinted.add(ly)
    if hinted:
        return hinted
    # 폴백: 닫힌 폴리라인 가장 많은 레이어
    if poly_by_layer:
        return {poly_by_layer.most_common(1)[0][0]}
    return set()


def extract_rooms(dxf_path: str, layers=None, progress_cb=None) -> dict:
    def report(p, s):
        if progress_cb:
            progress_cb(int(p), s)

    report(10, "도면 로딩 중...")
    doc = ezdxf.readfile(dxf_path)
    msp = doc.modelspace()

    report(25, "실 경계 레이어 탐지 중...")
    if not layers:
        layers = _detect_room_layers(msp)
    layers = set(layers)

    report(40, "실 경계 폴리라인 / 실명 수집 중...")
    polys = []
    texts = []
    for e in msp:
        ly = e.dxf.get("layer", "0")
        if ly not in layers:
            continue
        t = e.dxftype()
        if t == "LWPOLYLINE" and e.is_closed:
            pts = [(p[0], p[1]) for p in e.get_points("xy")]
            if len(pts) >= 3:
                a, L = _poly_area_perim(pts)
                if a >= 1.0:  # 1m² 이상만 실로 간주
                    polys.append({"pts": pts, "area": a, "perim": L})
        elif t in ("TEXT", "MTEXT"):
            try:
                txt = e.dxf.text.strip() if t == "TEXT" else e.plain_mtext().strip()
                ins = e.dxf.insert
                if txt:
                    texts.append((txt, ins[0], ins[1]))
            except Exception:
                pass

    report(60, "실명 ↔ 경계 매칭 중...")
    rooms = []
    matched = 0
    total = len(polys)
    for idx, p in enumerate(polys):
        inside = [t for (t, x, y) in texts if _point_in_poly(x, y, p["pts"])]
        code, name = _classify_texts(inside)
        if code or name:
            matched += 1
        rooms.append({
            "code": code or "",
            "name": name or "",
            "area_m2": round(p["area"], 2),
            "perim_m": round(p["perim"], 2),
            "text_count": len(inside),
        })
        if idx % 30 == 0:
            report(60 + int(30 * (idx + 1) / max(total, 1)), "실명 매칭 중...")

    rooms.sort(key=lambda r: -r["area_m2"])
    report(100, "실 추출 완료")

    return {
        "layers_used": sorted(layers),
        "room_count": len(rooms),
        "identified": matched,
        "unidentified": len(rooms) - matched,
        "total_area_m2": round(sum(r["area_m2"] for r in rooms), 2),
        "rooms": rooms,
    }
