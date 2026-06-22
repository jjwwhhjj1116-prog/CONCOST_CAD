"""
DXF 도면 정리 모듈 - 2단계: 레이어 정리 + QTO 표준 레이어 생성
"""
import ezdxf
from analyzer import classify_layer, classify_wall

# ─────────────────────────────────────────────
# QTO 표준 레이어 정의 (이름: AutoCAD 색상번호)
# ─────────────────────────────────────────────
QTO_LAYERS = {
    # 실/면적
    "QTO_FIN_ROOM_BOUNDARY": 3,     # green
    "QTO_FIN_ROOM_NAME": 3,
    # 벽 길이 (바탕별)
    "QTO_FIN_WALL_LEN_CONC": 1,     # red
    "QTO_FIN_WALL_LEN_DRY": 4,      # cyan
    "QTO_FIN_WALL_LEN_MASONRY": 6,  # magenta
    "QTO_FIN_WALL_LEN_UNKNOWN": 2,  # yellow
    # 벽 면적 (바탕별)
    "QTO_FIN_WALL_AREA_CONC": 1,
    "QTO_FIN_WALL_AREA_DRY": 4,
    "QTO_FIN_WALL_AREA_MASONRY": 6,
    "QTO_FIN_WALL_AREA_UNKNOWN": 2,
    # 개구부
    "QTO_FIN_OPENING_DOOR": 5,      # blue
    "QTO_FIN_OPENING_WINDOW": 5,
    # 검토용
    "QTO_REVIEW_UNKNOWN_LAYER": 2,
    "QTO_REVIEW_UNKNOWN_WALL": 2,
    "QTO_REVIEW_OPEN_BOUNDARY": 30,
    "QTO_REVIEW_CONFLICT": 30,
}

WALL_KEYWORDS = ["wall", "벽", "w-", "_w_"]
ROOM_KEYWORDS = ["room", "실", "공간"]
DOOR_KEYWORDS = ["door", "문", "d-", "_d_"]
WINDOW_KEYWORDS = ["window", "창"]


def _ensure_qto_layers(doc):
    """QTO 표준 레이어를 도면에 생성 (없을 때만)"""
    created = []
    for name, color in QTO_LAYERS.items():
        if name not in doc.layers:
            doc.layers.add(name, color=color)
            created.append(name)
    return created


def _resolve_classify(layer_name: str, overrides: dict) -> str:
    """사용자 검토 선택(overrides)을 자동 분류보다 우선 적용"""
    if layer_name in overrides:
        ov = overrides[layer_name]
        if ov in ("유지", "keep"):
            return "keep"
        if ov in ("삭제", "delete"):
            return "delete"
        # '나중에' 등은 자동 분류로 폴백
    return classify_layer(layer_name)


def _layer_wall_type(layer_name: str) -> str:
    """벽 레이어명으로 바탕(CONC/DRY/MASONRY/UNKNOWN) 추정"""
    return classify_wall(layer_name, [])


def _match_any(layer_name: str, keywords: list[str]) -> bool:
    nm = layer_name.lower()
    return any(kw in nm for kw in keywords)


# ─────────────────────────────────────────────
# 1) 정리된 DXF: 원본 구조 유지 + 삭제 후보 레이어만 제거/OFF
#    - 블록(INSERT) 구조는 절대 건드리지 않는다 (평탄화 X)
#    - 삭제 후보 레이어: 최상위 객체 제거 + 레이어 OFF
#      → 블록 내부의 같은 레이어 객체도 화면에서 사라짐
# ─────────────────────────────────────────────
def build_cleaned_dxf(src_path: str, out_path: str, overrides: dict = None):
    overrides = overrides or {}
    doc = ezdxf.readfile(src_path)
    msp = doc.modelspace()

    delete_layers = set()
    for layer in doc.layers:
        name = layer.dxf.name
        if _resolve_classify(name, overrides) == "delete":
            delete_layers.add(name)

    # 최상위 모델스페이스에서 삭제 레이어 객체 제거
    removed = 0
    for e in list(msp):
        if e.dxf.get("layer", "0") in delete_layers:
            msp.delete_entity(e)
            removed += 1

    # 삭제 레이어를 OFF 처리 → 블록 내부의 같은 레이어 도형도 숨김
    turned_off = 0
    for name in delete_layers:
        if name == "0":
            continue
        try:
            doc.layers.get(name).off()
            turned_off += 1
        except Exception:
            pass

    doc.saveas(out_path)
    return {
        "deleted_layers": sorted(delete_layers),
        "removed_entities": removed,
        "turned_off_layers": turned_off,
        "kept_entities": len(list(msp)),
    }


# ─────────────────────────────────────────────
# 2) QTO 레이어드 DXF: 원본 + QTO 표준 빈 레이어 골격만 추가
#    - 원본 도형을 복사하지 않는다 (겹침 방지)
#    - 실제 객체 분류/배치는 3·4단계에서 채운다
#    - 정리(삭제 레이어 OFF) 상태도 함께 반영
# ─────────────────────────────────────────────
def build_qto_dxf(src_path: str, out_path: str, overrides: dict = None):
    overrides = overrides or {}
    doc = ezdxf.readfile(src_path)

    created_layers = _ensure_qto_layers(doc)

    # 정리 상태 동기화: 삭제 후보 레이어 OFF
    for layer in doc.layers:
        name = layer.dxf.name
        if name != "0" and _resolve_classify(name, overrides) == "delete":
            try:
                layer.off()
            except Exception:
                pass

    doc.saveas(out_path)
    return {
        "created_qto_layers": created_layers,
        "qto_layer_count": len(QTO_LAYERS),
        "note": "QTO 표준 레이어 골격 생성 완료. 객체 배치는 3·4단계에서 진행됩니다.",
    }


# ─────────────────────────────────────────────
# 3) 레이어 분류표 (CSV 텍스트)
# ─────────────────────────────────────────────
def build_classify_table(src_path: str, overrides: dict = None) -> str:
    overrides = overrides or {}
    doc = ezdxf.readfile(src_path)
    msp = doc.modelspace()

    # 레이어별 사용 객체 수
    usage = {}
    for e in msp:
        ly = e.dxf.get("layer", "0")
        usage[ly] = usage.get(ly, 0) + 1

    rows = ["레이어명,자동분류,최종분류,벽바탕추정,사용객체수,비고"]
    for layer in sorted(doc.layers, key=lambda l: l.dxf.name):
        name = layer.dxf.name
        auto = classify_layer(name)
        final = _resolve_classify(name, overrides)
        wt = _layer_wall_type(name) if _match_any(name, WALL_KEYWORDS) else ""
        cnt = usage.get(name, 0)
        note = "사용자선택" if name in overrides else ""
        safe = name.replace(",", " ")
        rows.append(f"{safe},{auto},{final},{wt},{cnt},{note}")

    return "\n".join(rows)
