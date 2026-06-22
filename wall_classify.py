"""
3단계 벽체 분류 엔진
건축도면 레이어(AIA 표준)로 벽 바탕재(CONC/DRY/MASONRY/PANEL)를 1차 분류하고,
바탕재별 벽 길이를 산출한다. 구조도면은 RC 검증 보조로 사용(선택).

핵심 관찰(현대ENG 도면 기준):
- 레이어가 Xref 바인딩 형태 'XR-...$0$A-WALL-DRY1' → '$0$' 뒤 실제명으로 분류
- 평탄화 시 벽 관련 레이어만 수집(전체 수십~백만 객체 폭발 방지)
"""
import math
import collections
import ezdxf

# ─────────────────────────────────────────────
# 벽 바탕재 분류 키워드 (실제 레이어명 기준, 소문자)
# ─────────────────────────────────────────────
SUBTYPE_KEYWORDS = [
    # (분류, [키워드...]) — 위에서부터 우선 매칭
    ("MASONRY", ["wall-masn", "masn", "block", "블럭", "벽돌", "brick", "조적"]),
    ("DRY",     ["wall-dry", "건식", "석고", "gb", "stud", "경량", "방습벽", "lgs"]),
    ("PANEL",   ["wall-panel", "panel", "글라스울", "허니컴", "커튼월", "cw-"]),
    ("CONC",    ["st-conc", "콘크리트", "concrete", "---wall", "wall-ext", "측량_rc"]),
]

# 벽으로 간주할 레이어 판단 (개구부/마크/주석 제외)
WALL_POSITIVE = ["wall", "st-conc", "---wall"]
WALL_EXCLUDE = ["wallopen", "오프닝", "open", "mark", "anno", "nplt", "dim", "axis", "hat"]

# 실명 / 창호 레이어
ROOM_LAYERS = ["room-iden", "실명", "room-name"]
WIN_LAYERS = ["a-win", "-win", "창호"]
DOOR_LAYERS = ["a-door", "-door", "문 "]


def real_layer(name: str) -> str:
    """Xref 바인딩 레이어명에서 실제 레이어명 추출 ('$0$' 뒤)"""
    if "$0$" in name:
        return name.split("$0$")[-1]
    return name


def wall_subtype(layer_name: str) -> str:
    n = real_layer(layer_name).lower()
    for subtype, kws in SUBTYPE_KEYWORDS:
        if any(k in n for k in kws):
            return subtype
    return "UNKNOWN"


def is_wall_layer(layer_name: str) -> bool:
    n = real_layer(layer_name).lower()
    if any(x in n for x in WALL_EXCLUDE):
        return False
    return any(p in n for p in WALL_POSITIVE)


def _is_relevant(layer_name: str) -> bool:
    """평탄화 시 수집할 레이어인지 (벽/실/창호) — 폭발 방지 필터"""
    n = real_layer(layer_name).lower()
    if is_wall_layer(layer_name):
        return True
    if any(k in n for k in ROOM_LAYERS):
        return True
    if any(k in n for k in WIN_LAYERS + DOOR_LAYERS):
        return True
    return False


def _entity_length_m(e) -> float:
    """LINE/LWPOLYLINE/ARC 길이 (m). mm 단위 도면 가정."""
    t = e.dxftype()
    try:
        if t == "LINE":
            s, en = e.dxf.start, e.dxf.end
            return math.hypot(en.x - s.x, en.y - s.y) / 1000.0
        if t == "LWPOLYLINE":
            pts = list(e.get_points("xy"))
            L = 0.0
            for i in range(len(pts) - 1):
                L += math.hypot(pts[i+1][0]-pts[i][0], pts[i+1][1]-pts[i][1])
            if e.is_closed and len(pts) > 1:
                L += math.hypot(pts[0][0]-pts[-1][0], pts[0][1]-pts[-1][1])
            return L / 1000.0
        if t == "ARC":
            r = e.dxf.radius
            ang = abs(e.dxf.end_angle - e.dxf.start_angle)
            return (math.radians(ang) * r) / 1000.0
    except Exception:
        return 0.0
    return 0.0


def _walk_collect(entity, depth, max_depth, out):
    """블록을 재귀로 펼치며 '관련 레이어' leaf만 out에 수집."""
    if entity.dxftype() == "INSERT" and depth < max_depth:
        try:
            for v in entity.virtual_entities():
                _walk_collect(v, depth + 1, max_depth, out)
        except Exception:
            pass
        return
    layer = entity.dxf.get("layer", "0")
    if _is_relevant(layer):
        out.append(entity)


def classify_walls(arch_dxf: str, struct_dxf: str = None, progress_cb=None, max_depth: int = 5) -> dict:
    """
    건축도면에서 벽을 바탕재별로 분류하고 길이 산출.
    struct_dxf 주어지면 RC 길이를 참고치로 함께 보고.
    """
    def report(p, s):
        if progress_cb:
            progress_cb(int(p), s)

    report(5, "건축도면 로딩 중...")
    doc = ezdxf.readfile(arch_dxf)
    msp = doc.modelspace()

    report(15, "블록 펼치며 벽/실/창호 수집 중...")
    collected = []
    top = list(msp)
    for i, e in enumerate(top):
        _walk_collect(e, 0, max_depth, collected)
        if i % 20 == 0:
            report(15 + int(45 * (i + 1) / max(len(top), 1)), "벽/실/창호 수집 중...")

    report(65, "벽 바탕재 분류 / 길이 산출 중...")
    # 바탕재별 길이 + 레이어별 집계
    subtype_len = collections.defaultdict(float)
    subtype_layers = collections.defaultdict(lambda: collections.Counter())
    wall_layer_len = collections.Counter()

    rooms = []       # 실명 텍스트
    windows = 0
    doors = 0

    for e in collected:
        layer = e.dxf.get("layer", "0")
        rl = real_layer(layer)
        rln = rl.lower()
        t = e.dxftype()

        if is_wall_layer(layer):
            st = wall_subtype(layer)
            L = _entity_length_m(e)
            if L > 0:
                subtype_len[st] += L
                subtype_layers[st][rl] += 1
                wall_layer_len[rl] += L
        elif any(k in rln for k in ROOM_LAYERS):
            if t in ("TEXT", "MTEXT"):
                try:
                    txt = e.dxf.text.strip() if t == "TEXT" else e.plain_mtext().strip()
                    if txt:
                        rooms.append(txt)
                except Exception:
                    pass
        elif any(k in rln for k in WIN_LAYERS):
            windows += 1
        elif any(k in rln for k in DOOR_LAYERS):
            doors += 1

    # 구조도면 RC 참고치 (선택)
    struct_rc = None
    if struct_dxf:
        report(80, "구조도면 RC 검증 중...")
        try:
            sdoc = ezdxf.readfile(struct_dxf)
            smsp = sdoc.modelspace()
            scoll = []
            for e in list(smsp):
                _walk_collect(e, 0, max_depth, scoll)
            rc_len = 0.0
            for e in scoll:
                rl = real_layer(e.dxf.get("layer", "0")).lower()
                if "st-conc" in rl or "---wall" in rl or "콘크리트" in rl:
                    rc_len += _entity_length_m(e)
            struct_rc = round(rc_len, 2)
        except Exception:
            struct_rc = None

    report(95, "결과 정리 중...")

    result = {
        "subtype_length_m": {k: round(v, 2) for k, v in sorted(subtype_len.items(), key=lambda x: -x[1])},
        "subtype_layers": {
            st: dict(cnt.most_common()) for st, cnt in subtype_layers.items()
        },
        "wall_layer_length_m": {k: round(v, 2) for k, v in wall_layer_len.most_common(40)},
        "rooms": rooms[:300],
        "room_count": len(rooms),
        "window_entities": windows,
        "door_entities": doors,
        "struct_rc_length_m": struct_rc,
        "collected_entities": len(collected),
    }
    report(100, "벽체 분류 완료")
    return result
