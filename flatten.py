"""
DXF 블록/Xref 평탄화 모듈 - 2.5단계
중첩된 INSERT(블록 참조)를 재귀적으로 펼친다(virtual_entities).

설계 원칙:
- 전체 도면을 통째로 펼치면 장식 요소(해치/동적블록)가 수백만 선분으로 폭발한다.
  → 삭제 후보 레이어의 도형은 펼치는 단계에서 미리 버린다.
  → 한 블록이 과도하게 폭발하면(가드 초과) 그 블록은 펼치지 않고 원본 INSERT로 남긴다.
- 적산에 필요한 벽/실/개구부/문자만 남겨 객체 수를 현실적으로 유지한다.
"""
import logging
import ezdxf
from analyzer import classify_layer

# ezdxf의 BLOCKREPRESENTATION 복사 경고 등 잡음 억제
logging.getLogger("ezdxf").setLevel(logging.ERROR)

# 한 최상위 INSERT가 이 개수를 넘게 폭발하면 펼치지 않고 원본 유지
MAX_PER_TOP = 60_000
# 전체 안전 상한
MAX_ENTITIES = 1_000_000
DEFAULT_MAX_DEPTH = 6

# 펼친 도형 중 이 타입은 보존하되 더 분해하지 않음
KEEP_AS_IS = {"HATCH", "IMAGE", "WIPEOUT"}


def _count_inserts(msp) -> int:
    return sum(1 for e in msp if e.dxftype() == "INSERT")


def _iter_flattened(entity, depth, max_depth, counters):
    """INSERT면 재귀적으로 펼쳐 leaf 엔티티를 yield. 그 외엔 그대로 yield."""
    etype = entity.dxftype()
    if etype != "INSERT" or depth >= max_depth:
        yield entity
        return
    try:
        subs = list(entity.virtual_entities())
    except Exception:
        counters["failed"] += 1
        yield entity
        return

    counters["exploded"] += 1
    for sub in subs:
        yield from _iter_flattened(sub, depth + 1, max_depth, counters)


def _drop_leaf(entity) -> bool:
    """삭제 후보 레이어의 leaf 도형은 평탄화 결과에서 제외한다."""
    layer = entity.dxf.get("layer", "0")
    return classify_layer(layer) == "delete"


def flatten_dxf(src_path: str, out_path: str, max_depth: int = DEFAULT_MAX_DEPTH, progress_cb=None):
    def report(p, s):
        if progress_cb:
            progress_cb(int(p), s)

    report(3, "도면 로딩 중...")
    src = ezdxf.readfile(src_path)
    src_msp = src.modelspace()

    top_entities = list(src_msp)
    initial_inserts = sum(1 for e in top_entities if e.dxftype() == "INSERT")
    total_top = len(top_entities)
    report(10, f"블록 참조 {initial_inserts:,}개 / 최상위 객체 {total_top:,}개")

    # 결과 도면 (레이어 정의 복사)
    new = ezdxf.new(dxfversion=src.dxfversion)
    for layer in src.layers:
        name = layer.dxf.name
        if name not in new.layers:
            try:
                new.layers.add(name, color=layer.dxf.get("color", 7))
            except Exception:
                pass
    new_msp = new.modelspace()

    counters = {"exploded": 0, "failed": 0}
    written = 0
    dropped = 0          # 삭제 레이어로 제외된 leaf
    skipped_huge = 0     # 폭주로 펼치지 않은 최상위 블록
    capped = False

    def _add(ent):
        nonlocal written, dropped
        if _drop_leaf(ent):
            dropped += 1
            return
        try:
            new_msp.add_foreign_entity(ent, copy=True)
            written += 1
        except Exception:
            counters["failed"] += 1

    for idx, e in enumerate(top_entities):
        if e.dxftype() == "INSERT":
            # 먼저 임시로 펼쳐 개수를 세고, 폭주하면 원본 INSERT만 유지
            buf = []
            blew_up = False
            for flat in _iter_flattened(e, 0, max_depth, counters):
                buf.append(flat)
                if len(buf) > MAX_PER_TOP:
                    blew_up = True
                    break
            if blew_up:
                skipped_huge += 1
                _add(e)  # 원본 블록 참조 그대로 보존
            else:
                for flat in buf:
                    _add(flat)
        else:
            _add(e)

        if written >= MAX_ENTITIES:
            capped = True
            break

        if idx % 50 == 0 or idx == total_top - 1:
            pct = 10 + int(75 * (idx + 1) / max(total_top, 1))
            report(pct, f"블록 분해 중... ({written:,}개 생성)")

    report(88, "평탄화 결과 저장 중...")
    new.saveas(out_path)
    report(100, "평탄화 완료")

    return {
        "initial_inserts": initial_inserts,
        "exploded": counters["exploded"],
        "failed_explode": counters["failed"],
        "dropped_delete_layer": dropped,
        "skipped_huge_blocks": skipped_huge,
        "remaining_inserts": _count_inserts(new_msp),
        "depth_limit": max_depth,
        "total_entities_after": written,
        "capped": capped,
    }
