"""装卸登记前的温控兼容性预检：分段分析与预检摘要指纹（纯函数，便于单测）。

预检针对“既有装卸时段 ∪ 本批拟登记时段”做扫描线叠加，输出车上货物集合
恒定的各个时段（非空集合才成段），并对每段给出：

* 货物集合（含本批拟登记的货物子集）；
* 温度上限差异：集合内各票货物温度上限的最小 / 最大 / 极差；
* 已有车载读数覆盖：落在该时段内的车次既有车载读数（有效温度与更正版本）；
* 潜在越温风险：时段内既有读数的有效温度高于集合中某票货物自身上限的
  （货物 × 读数）组合；若该货物在读数时刻有自有测温（同刻自有优先），
  标记 ``supplanted_by_own``——车载读数对该货物实际会被取代，仅供参考。

预检摘要（digest）
-----------------
预检摘要把“本次分析所依据的全部输入”绑定为一个指纹：

    digest = "sha256:" + hex(sha256(规范化 JSON))

规范化 JSON 为单行、无多余空白，内容按固定顺序排列：

    [trip_id, trip_revision,
     [[cargo_id, loaded_at, unloaded_at], ...],            -- 本批时段（排序）
     [[cargo_id, [version, effective_at, limit], ...], ...], -- 涉及货物完整标准历程
     [[reading_id, read_at, version, effective_temp], ...]]-- 车次读数（排序）

其中“涉及货物”为本批货物与该车次既有装卸货物之并集；每票货物绑定其
**完整温度上限标准历程**（初始上限 + 各分时变更，货物标准版本指纹）；
“车次读数”为该车次全部既有读数及其更正版本与有效温度（读数版本指纹）。
确认时服务端按当前数据重算指纹：车次修订号、货物温度上限标准（含补录 /
新增分时变更）或读数版本任一变化都会使指纹改变，过期摘要即被拒绝。

温度上限支持**分时生效**后，预检还须在各涉及货物的**标准变更时刻**把车上
货物集合恒定的分段进一步切开：切分后每个子段内各票货物的上限均恒定，
上限差异（min/max/极差）按子段起点（左闭，边界读数采用新上限）取值；
潜在风险按读数时刻该货物**当时生效**的上限判定。
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from decimal import Decimal

from app.timeutil import ensure_utc

# 温度指纹统一量化到 NUMERIC(10,3) 的精度，避免存储层标度差异影响指纹
_TEMP_SCALE = Decimal("0.001")


def _canon_dt(dt) -> str:
    """UTC ISO 8601（保留微秒），供指纹使用。"""
    return ensure_utc(dt).isoformat()


def _canon_temp(value) -> str:
    """温度指纹：量化到千分位后的十进制字符串。"""
    return str(Decimal(value).quantize(_TEMP_SCALE))


@dataclass(frozen=True)
class BoardWindow:
    """一段“在车”窗口：既有时段或本批拟登记时段。"""

    cargo_id: str
    start: object
    end: object
    proposed: bool  # True=本批拟登记；False=车次既有时段


@dataclass(frozen=True)
class BoardSegment:
    """车上货物集合恒定的一段时段 [start, end)（左闭右开，正长度）。"""

    start: object
    end: object
    cargo_ids: tuple[str, ...]           # 该时段车上全部货物（排序）
    proposed_ids: tuple[str, ...]        # 其中来自本批拟登记的货物（排序）


def boarding_segments(windows: list[BoardWindow]) -> list[BoardSegment]:
    """扫描线叠加全部在车窗口，输出货物集合非空且恒定的分段。

    同一时刻先处理“下车”再处理“上车”，保证左闭右开语义：恰在 t 卸货的
    货物不计入 t 起的新段。同一货物可能有多段窗口（甚至与本批/既有窗口
    重叠——重叠冲突由确认时的批量原子校验拒绝），此处按引用计数判定
    “是否在车”，保证预检对任意输入都有确定结果。相邻且集合相同的段合并。
    """
    events: list[tuple] = []
    for w in windows:
        events.append((w.start, 1, w.cargo_id, w.proposed))
        events.append((w.end, 0, w.cargo_id, w.proposed))
    # 排序键：时刻；同刻下车(0)先于上车(1)；再按货物编号稳定排序
    events.sort(key=lambda e: (e[0], e[1], e[2]))

    onboard: dict[str, int] = {}          # cargo_id -> 在车窗口计数
    proposed_onboard: dict[str, int] = {}  # 其中拟登记窗口的计数
    raw: list[BoardSegment] = []
    seg_start = None
    for at, kind, cargo_id, proposed in events:
        if seg_start is not None and seg_start < at:
            cargos = tuple(sorted(c for c, n in onboard.items() if n > 0))
            if cargos:
                raw.append(
                    BoardSegment(
                        seg_start,
                        at,
                        cargos,
                        tuple(
                            c
                            for c in cargos
                            if proposed_onboard.get(c, 0) > 0
                        ),
                    )
                )
        delta = 1 if kind == 1 else -1
        onboard[cargo_id] = onboard.get(cargo_id, 0) + delta
        if proposed:
            proposed_onboard[cargo_id] = (
                proposed_onboard.get(cargo_id, 0) + delta
            )
        seg_start = at

    # 相邻且货物集合（含拟登记子集）相同的段合并
    segments: list[BoardSegment] = []
    for seg in raw:
        if (
            segments
            and segments[-1].end == seg.start
            and segments[-1].cargo_ids == seg.cargo_ids
            and segments[-1].proposed_ids == seg.proposed_ids
        ):
            prev = segments[-1]
            segments[-1] = BoardSegment(
                prev.start, seg.end, prev.cargo_ids, prev.proposed_ids
            )
        else:
            segments.append(seg)
    return segments


@dataclass(frozen=True)
class ReadingInfo:
    """一笔车载读数的预检视图：有效温度（含更正）与更正版本。"""

    reading_id: str
    read_at: object
    effective_temperature: Decimal
    version: int
    quarantined: bool = False       # 是否被本车次生效探头故障覆盖
    covering_fault_ids: tuple = ()  # 覆盖该读数的生效故障编号


@dataclass(frozen=True)
class RiskEntry:
    """一个潜在越温组合：时段内既有读数的有效温度高于该货物当时上限。"""

    cargo_id: str
    reading_id: str
    read_at: object
    effective_temperature: Decimal
    temp_upper_limit: Decimal
    supplanted_by_own: bool  # 该货物在读数时刻有自有测温（车载读数被取代）
    quarantined: bool = False  # 该读数被生效探头故障覆盖（实际不参与重建）
    covering_fault_ids: tuple = ()


@dataclass(frozen=True)
class SegmentAnalysis:
    """一个分段的完整预检分析。"""

    segment: BoardSegment
    temp_limit_min: Decimal
    temp_limit_max: Decimal
    temp_limit_spread: Decimal
    readings: tuple[ReadingInfo, ...]   # 落在该时段内的既有车载读数
    risks: tuple[RiskEntry, ...]


def split_segments_at_standard_changes(
    segments: list[BoardSegment],
    limit_histories: dict,
) -> list[BoardSegment]:
    """在涉及货物的温度上限标准变更时刻把分段进一步切开（左闭右开）。

    切分后每个子段内各票货物适用的上限均恒定（子段内无任何标准变更时刻），
    故上限差异可按子段起点取值；恰在切分点（标准生效时刻）的读数归入右侧
    新子段并采用新上限，与“边界读数采用新上限”一致。货物集合 / 拟登记
    子集保持不变（相邻同集合段的合并不受影响——此处不重新合并，切分点本就
    可能改变上限差异，必须保留边界）。

    :param limit_histories: 货物编号 -> 标准条目序列
        （:class:`app.standards.LimitEntry`，含初始条目）。
    """
    # 各货物的标准变更时刻（初始条目在货物起始时刻，通常落在所有在车段之前；
    # 若货物起始时刻落在段内也一并作为切点——该时刻起初始上限才“生效”）
    split_points: set = set()
    for entries in limit_histories.values():
        for entry in entries:
            split_points.add(entry.effective_at)

    result: list[BoardSegment] = []
    for seg in segments:
        cuts = sorted(t for t in split_points if seg.start < t < seg.end)
        start = seg.start
        for cut in cuts:
            result.append(
                BoardSegment(
                    start,
                    cut,
                    seg.cargo_ids,
                    seg.proposed_ids,
                )
            )
            start = cut
        result.append(
            BoardSegment(start, seg.end, seg.cargo_ids, seg.proposed_ids)
        )
    return result


def analyze_segments(
    segments: list[BoardSegment],
    readings: list[ReadingInfo],
    limit_histories: dict,
    own_measure_at: dict[str, set] | None = None,
) -> list[SegmentAnalysis]:
    """对每个分段计算温度上限差异、读数覆盖与潜在越温风险。

    :param segments: 经 :func:`boarding_segments` 与
        :func:`split_segments_at_standard_changes` 输出的分段（每个子段内
        各票货物上限恒定）。
    :param readings: 该车次的全部既有车载读数（有效温度 + 版本）。
    :param limit_histories: 货物编号 -> 标准条目序列
        （:class:`app.standards.LimitEntry`）；段内每票货物的上限取子段起点
        （左闭）生效的标准，风险按各读数时刻**当时生效**的上限判定。
    :param own_measure_at: 货物编号 -> 该货物自有测温时刻集合
        （用于标记同刻自有测温取代车载读数的情形）。
    """
    from app import standards as standards_mod

    own_measure_at = own_measure_at or {}
    resolvers = {
        cid: standards_mod.limit_resolver(entries)
        for cid, entries in limit_histories.items()
    }
    analyses: list[SegmentAnalysis] = []
    for seg in segments:
        # 子段内上限恒定：按子段起点（左闭）生效的标准取差异
        limits = [Decimal(resolvers[c](seg.start)) for c in seg.cargo_ids]
        lo, hi = min(limits), max(limits)
        covered = tuple(
            r for r in readings if seg.start <= r.read_at < seg.end
        )
        risks: list[RiskEntry] = []
        for r in covered:
            for cargo_id in seg.cargo_ids:
                # 边界读数采用新上限：按读数时刻解析当时标准
                limit = Decimal(resolvers[cargo_id](r.read_at))
                if Decimal(r.effective_temperature) > limit:
                    risks.append(
                        RiskEntry(
                            cargo_id=cargo_id,
                            reading_id=r.reading_id,
                            read_at=r.read_at,
                            effective_temperature=Decimal(
                                r.effective_temperature
                            ),
                            temp_upper_limit=limit,
                            supplanted_by_own=(
                                r.read_at in own_measure_at.get(cargo_id, set())
                            ),
                            quarantined=r.quarantined,
                            covering_fault_ids=r.covering_fault_ids,
                        )
                    )
        analyses.append(
            SegmentAnalysis(
                segment=seg,
                temp_limit_min=lo,
                temp_limit_max=hi,
                temp_limit_spread=hi - lo,
                readings=covered,
                risks=tuple(risks),
            )
        )
    return analyses


def manifest_digest(
    trip_id: str,
    trip_revision: int,
    items,
    limit_histories: dict,
    readings: list[ReadingInfo],
) -> str:
    """计算预检摘要指纹（``sha256:`` 前缀）。

    :param items: 本批拟登记时段 ``(cargo_id, loaded_at, unloaded_at)``
        （已归一化到 UTC）。
    :param limit_histories: 涉及货物（本批 ∪ 车次既有装卸）编号 -> 标准
        条目序列（:class:`app.standards.LimitEntry`）：摘要绑定每票货物的
        **完整温度上限标准历程**（初始上限 + 各分时变更），任一条补录 /
        新增 / 变化都使摘要过期。
    :param readings: 该车次全部既有车载读数（有效温度 + 更正版本）。
    """
    standards_payload = []
    for cargo_id in sorted(limit_histories):
        history = list(limit_histories[cargo_id])
        standards_payload.append(
            [
                cargo_id,
                [
                    [
                        int(e.version),
                        _canon_dt(e.effective_at),
                        _canon_temp(e.temp_upper_limit),
                        e.source,
                        e.change_id,
                    ]
                    for e in sorted(history, key=lambda e: (e.effective_at, e.version))
                ],
            ]
        )
    payload = [
        trip_id,
        int(trip_revision),
        sorted(
            [cargo_id, _canon_dt(loaded_at), _canon_dt(unloaded_at)]
            for cargo_id, loaded_at, unloaded_at in items
        ),
        standards_payload,
        sorted(
            [
                r.reading_id,
                _canon_dt(r.read_at),
                int(r.version),
                _canon_temp(r.effective_temperature),
            ]
            for r in readings
        ),
    ]
    canonical = json.dumps(
        payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False
    )
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return f"sha256:{digest}"
