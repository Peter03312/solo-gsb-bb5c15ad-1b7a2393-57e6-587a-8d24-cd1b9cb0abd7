"""责任证据封存：证据归集、内容摘要（规范化 JSON + SHA-256）与现况对照（纯逻辑）。

封存内容
--------
调查人员对一个**已闭合**越温区间封存责任证据。封存包（:func:`build_seal_content`）
在一致的已提交视图上保存：

* 货物温控参数（编号 / 起始时间 / 温度上限 / 起始保管方）；
* 区间责任片段、开启 / 闭合读数来源坐标与当前快照标识；
* **当时有效表态**：封存时各责任方最新一条仍对当前快照有效的表态；
* 重建该区间所用的**自有事件**（发生时刻不晚于区间闭合时刻的全部事件，
  测温附当前有效温度与更正版本）；
* **车载读数**：对该货物适用（同一车次且读数时刻在当前装卸窗口内，
  左闭右开）且读数时刻落在区间内的读数，附当前有效温度、更正版本与
  封存时是否被同刻自有测温取代（``adopted``）；**封存时已被生效探头
  故障隔离的读数同样随包封存**（``quarantined=true`` 并附覆盖故障编号
  ``covering_fault_ids``、``adopted=false``），使调查人员可据原包核对
  隔离依据——隔离读数不参与越温重建，但原始读数与隔离事实必须留痕；
* **当前装卸窗口及版本**：与区间时间范围相交的该货物全部装卸窗口
  （``window_version``，当前窗口时间即其当前版本）。

内容摘要
========
对封存内容（除 ``content_digest`` 自身外）取规范化 JSON 的 SHA-256：

    content_digest = "sha256:" + hex(sha256(canonical_json))

规范化 JSON 为单行、无多余空白（``separators=(",", ":")``），非 ASCII 不转义；
时间统一为 UTC ISO 8601 秒级（``YYYY-MM-DDTHH:MM:SSZ``），温度量化到
千分位十进制字符串。封存包原文与摘要一同落库，此后不可改。

现况对照（diff）
----------------
封存后再次重建：

* ``unchanged``：区间仍存在、已闭合且快照标识与封存时相同——证据项无
  增删变更（封存内容本就是该视图的确定性投影）；
* ``changed``：区间仍闭合但快照已变（边界 / 责任片段变化）；
* ``reopened``：同标识区间当前为开放区间；
* ``disappeared``：同标识区间当前既不闭合也不开放（已消失）。

对照按封存时相同的证据范围逐项比较，列出**证据项**（自有事件 / 车载读数
/ 装卸窗口）与**责任片段**的新增 / 删除 / 变更；晚到记录与测温 / 装卸
更正只影响对照结果，**绝不改写封存原包**。

隔离读数的对照语义
~~~~~~~~~~~~~~~~~~
车载读数是否参与重建随生效故障变化，封存 / 对照两侧都完整保留“适用”读数，
并以 ``quarantined`` / ``covering_fault_ids`` 标记各自时点的隔离状态：

* **封存后才被隔离**（封存时未隔离、当前被生效故障覆盖）：该读数不再参与
  重建，按既有语义记为 ``removed``（封存包不含隔离事实，无法以新旧值对照，
  与旧封存包行为一致）；
* **封存时已隔离、当前仍隔离**：读数恒在证据集中，隔离状态不变（覆盖集合
  也相同）则无差异；覆盖故障集合变化（重叠故障撤销一条后仍被另一条覆盖）
  记为 ``changed``（字段 ``covering_fault_ids``）；
* **封存时已隔离、当前撤销最后一条覆盖故障而恢复**：记为 ``changed``
  （字段 ``adopted`` / ``quarantined`` / ``covering_fault_ids``；同刻自有
  测温仍优先时 ``adopted`` 保持 false）；
* 恢复采用后又被新生效故障覆盖（或反之）的反复变化，同样只表现为该证据项
  的 ``changed``，封存原文与摘要永不改。
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from decimal import Decimal

from app import chain, responsibility as resp, timeline
from app.chain import interval_id
from app.timeutil import ensure_utc

# 封存后现况状态
STATUS_UNCHANGED = "unchanged"        # 区间仍闭合且快照相同
STATUS_CHANGED = "changed"            # 区间仍闭合但快照已变（边界/片段变化）
STATUS_REOPENED = "reopened"          # 同标识区间重新开放
STATUS_DISAPPEARED = "disappeared"    # 同标识区间已消失

# 证据项变更类型
ITEM_ADDED = "added"
ITEM_REMOVED = "removed"
ITEM_CHANGED = "changed"

_TEMP_SCALE = Decimal("0.001")


# ----------------------------------------------------------- 规范化原语


def ts(dt) -> str:
    """UTC ISO 8601 秒级，Z 后缀（封存内容时间的统一表示）。"""
    dt = ensure_utc(dt).replace(microsecond=0)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def temp(value) -> str:
    """温度指纹 / 表示：量化到千分位的十进制字符串。"""
    return str(Decimal(value).quantize(_TEMP_SCALE))


def _seg_triples(segments) -> list:
    out = []
    for seg in segments:
        party = getattr(seg, "responsible_party", None) or seg.party
        out.append(
            [
                ts(seg.start),
                ts(seg.end) if seg.end is not None else None,
                party,
            ]
        )
    return out


def canonical_json(obj) -> str:
    """封存内容的规范化 JSON 字符串（与内容摘要一一对应）。"""
    return json.dumps(
        obj, ensure_ascii=False, separators=(",", ":"), allow_nan=False
    )


def digest_of(canonical: str) -> str:
    return f"sha256:{hashlib.sha256(canonical.encode('utf-8')).hexdigest()}"


def request_fingerprint(cargo_id: str, interval_key: str, snapshot_id: str) -> str:
    """同案件编号幂等 / 冲突裁决用的请求指纹（货物 + 区间 + 当前快照）。"""
    payload = [cargo_id, interval_key, snapshot_id]
    return digest_of(canonical_json(payload))


# ----------------------------------------------------------- 证据归集


def _own_event_item(e, event_effective: dict, limit_for=None) -> dict:
    is_measurement = getattr(e.event_type, "name", e.event_type) == "measurement"
    eff = event_effective.get(e.event_id) if is_measurement else None
    return {
        "event_id": e.event_id,
        "event_type": "measurement" if is_measurement else "handover",
        "occurred_at": ts(e.occurred_at),
        "temperature": temp(e.temperature) if is_measurement else None,
        "effective_temperature": (
            temp(eff[0]) if eff is not None else (temp(e.temperature) if is_measurement else None)
        ),
        "version": eff[1] if eff is not None else 0,
        # 该测温时刻当时生效的温度上限（标准分时生效；交接恒为 null）
        "temp_upper_limit": (
            temp(limit_for(e.occurred_at))
            if is_measurement and limit_for is not None
            else None
        ),
        "giver": e.giver,
        "receiver": e.receiver,
    }


def _reading_item(r, reading_effective: dict, adopted: bool, covers=(), limit_for=None) -> dict:
    eff = reading_effective.get(r.reading_id)
    covers = tuple(covers or ())
    return {
        "reading_id": r.reading_id,
        "trip_id": r.trip_id,
        "read_at": ts(r.read_at),
        "temperature": temp(r.temperature),
        "effective_temperature": (
            temp(eff[0]) if eff is not None else temp(r.temperature)
        ),
        "version": eff[1] if eff is not None else 0,
        # 该读数时刻对本货物当时生效的温度上限（标准分时生效，左闭取新）
        "temp_upper_limit": (
            temp(limit_for(r.read_at)) if limit_for is not None else None
        ),
        "adopted": adopted,
        # 封存时是否被生效探头故障隔离；隔离读数不参与越温重建（adopted 恒为
        # false），但原始读数、有效温度 / 版本与覆盖故障编号随包封存留痕，
        # 供调查人员核对隔离依据（同刻自有测温仍优先于车载读数）。
        "quarantined": bool(covers),
        "covering_fault_ids": list(covers),
    }


def _loading_item(l) -> dict:
    return {
        "trip_id": l.trip_id,
        "cargo_id": l.cargo_id,
        "origin_loaded_at": ts(l.origin_loaded_at),
        "loaded_at": ts(l.loaded_at),
        "unloaded_at": ts(l.unloaded_at),
        "window_version": l.window_version or 0,
    }


def scoped_evidence(
    events,
    readings,
    loadings,
    effective,
    start_bound,
    span_end,
    apply_window_filter: bool = True,
    reading_quarantine: dict | None = None,
    standards: tuple = (),
):
    """按时间范围归集重建证据（封存与现况对照共用同一定义）。

    :param start_bound: 车载读数 / 装卸窗口的范围下界（含）；货物起始时间
        或区间起点。
    :param span_end: 范围上界（含）；封存时为区间闭合时刻，开放区间对照
        传入查询上界；为 None 时视为无上界。
    :param apply_window_filter: 自有事件是否同样施加下界——封存与对照的
        自有事件均取“不晚于闭合时刻”的**全部**事件（含开启前完整保管链，
        下界置 False）；车载读数 / 装卸窗口恒按下界与上界归集。
    :param reading_quarantine: 车载读数ID -> 覆盖该读数的**生效探头故障**
        编号元组；被覆盖的读数不参与越温重建（``adopted=false`` /
        ``quarantined=true``），但**仍是适用读数，照常计入证据集**并附
        覆盖故障编号，使封存包可核对隔离依据；封存后才登记故障时，旧读数
        在对照中因当前侧带隔离标记而按“封存后隔离”语义记为 ``removed``。
    :param standards: 货物完整温度标准历程（含初始版本）。每条测温 / 读数
        证据附其时刻**当时采用的上限**（左闭，边界读数取新上限）；为空时
        不附上限（旧口径）。
    :returns: ``(own_event_items, reading_items, loading_items)`` 规范化字典，
        分别按 事件ID / (读数时刻, 读数ID) / (车次, 原装车时刻) 升序。
    """
    event_effective, reading_effective = effective
    reading_quarantine = reading_quarantine or {}

    def _limit_for(at):
        if not standards:
            return None
        from app.standards import limit_at

        return limit_at(standards, at)

    lower = ensure_utc(start_bound) if start_bound is not None else None

    # 自有事件：封存与对照均取不晚于上界的全部事件（apply_window_filter 为
    # False 时不施下界）；开启前的测温/交接是重建“开启时刻保管方”所必需。
    own_items = []
    for e in events:
        at = ensure_utc(e.occurred_at)
        if span_end is not None and at > span_end:
            continue
        if apply_window_filter and lower is not None and at < lower:
            continue
        own_items.append(_own_event_item(e, event_effective, _limit_for))
    own_items.sort(key=lambda x: x["event_id"])

    own_measure_at = {
        ensure_utc(e.occurred_at)
        for e in events
        if getattr(e.event_type, "name", e.event_type) == "measurement"
    }

    # 车载读数：对该货物适用（同车次 + 当前窗口左闭右开在车）且读数时刻
    # 落在范围内即计入证据集，不按隔离剔除——
    # * 被同刻自有测温取代的原始读数同样封存（adopted=false），完整保留
    #   “同刻自有优先”的重建上下文；
    # * **被生效探头故障覆盖的读数虽不参与重建（adopted=false），也照常
    #   封存并标记 quarantined / covering_fault_ids**，使调查人员能从封存
    #   包核对“该读数为何不参与越温重建”的隔离依据（重叠故障撤销一条后
    #   覆盖集合的变化在对照中逐项体现）。
    # adopted 口径与 timeline.merged_timeline 一致：同刻无自有测温且未隔离。
    reading_items = []
    for r in readings:
        read_at = ensure_utc(r.read_at)
        if lower is not None and read_at < lower:
            continue
        if span_end is not None and read_at > span_end:
            continue
        applicable = any(timeline.is_reading_applicable(l, r) for l in loadings)
        if not applicable:
            continue
        covers = tuple(reading_quarantine.get(r.reading_id, ()))
        adopted = read_at not in own_measure_at and not covers
        reading_items.append(
            _reading_item(r, reading_effective, adopted, covers, _limit_for)
        )
    reading_items.sort(key=lambda x: (x["read_at"], x["reading_id"]))

    # 装卸窗口：与 [lower, span_end] 相交（unload>下界 且 load<=上界）。
    loading_items = []
    for l in loadings:
        lo, un = ensure_utc(l.loaded_at), ensure_utc(l.unloaded_at)
        if lower is not None and un <= lower:
            continue
        if span_end is not None and lo > span_end:
            continue
        loading_items.append(_loading_item(l))
    loading_items.sort(key=lambda x: (x["trip_id"], x["origin_loaded_at"]))

    return own_items, reading_items, loading_items


# ----------------------------------------------------------- 封存包


def _stance_item(view) -> dict:
    """封存时一条“当前有效”的最新表态（取自责任确认视图）。"""
    return {
        "stance_id": view.stance_id,
        "responsible_party": view.responsible_party,
        "stance": view.kind.value,
        "reason": view.reason,
        "created_at": ts(view.created_at),
        "snapshot_id": view.snapshot_id,
        "segments": _seg_triples(view.segments),
    }


def _interval_dict(ex) -> dict:
    return {
        "interval_id": interval_id(ex),
        "start": ts(ex.start),
        "end": ts(ex.end),
        "is_open": False,
        "start_source": ex.start_source,
        "end_source": ex.end_source,
        "start_event_id": ex.start_event_id,
        "end_event_id": ex.end_event_id,
        "start_reading_id": ex.start_reading_id,
        "end_reading_id": ex.end_reading_id,
        "start_trip_id": ex.start_trip_id,
        "end_trip_id": ex.end_trip_id,
        "snapshot_id": resp.snapshot_id(ex),
        "segments": _seg_triples(ex.segments),
    }


def _standard_item(entry) -> dict:
    """标准历程中的一个版本（初始版本或一次变更）的规范化字典。"""
    return {
        "version": int(entry.version),
        "effective_at": ts(entry.effective_at),
        "temp_upper_limit": temp(entry.temp_upper_limit),
        "change_id": entry.change_id,
        "reason": entry.reason,
    }


@dataclass(frozen=True)
class SealPayload:
    """封存组装结果：规范化封存包（含摘要）与摘要（落库 / 回执用）。"""

    content: dict
    canonical: str
    content_digest: str
    snapshot_id: str
    sealed_at: str


def build_seal_payload(
    case_id: str,
    cargo,
    ex,
    interval_view,
    events,
    readings,
    loadings,
    effective,
    sealed_at,
    reading_quarantine: dict | None = None,
    standards: tuple = (),
) -> SealPayload:
    """在一个一致的已提交视图上组装封存包并计算内容摘要。

    :param ex: 已闭合区间 :class:`app.chain.ExcursionResult`。
    :param interval_view: :meth:`responsibility.build_interval_view` 的视图
        （提供当时各责任方最新有效表态）。
    :param sealed_at: 封存时刻（datetime，统一转 UTC 秒级）。
    :param reading_quarantine: 车载读数ID -> 生效探头故障编号元组；
        被覆盖的读数不参与重建（adopted=false / quarantined=true），但仍
        计入封存证据并附覆盖故障编号，供核对隔离依据。
    :param standards: 货物完整温度标准历程（含初始版本）：封存包纳入完整
        标准历程，且每条测温 / 读数证据记录其时刻当时采用的上限。
    """
    start, end = ensure_utc(ex.start), ensure_utc(ex.end)
    # 封存自有事件取“不晚于闭合时刻”的全部事件（含开启前完整保管链，
    # apply_window_filter=False 不施下界）；车载读数 / 装卸窗口按区间范围归集。
    own_items, reading_items, loading_items = scoped_evidence(
        events,
        readings,
        loadings,
        effective,
        start,
        end,
        apply_window_filter=False,
        reading_quarantine=reading_quarantine,
        standards=standards,
    )

    sealed_at_s = ts(sealed_at)
    content = {
        "case_id": case_id,
        "sealed_at": sealed_at_s,
        "cargo": {
            "cargo_id": cargo.cargo_id,
            "started_at": ts(cargo.started_at),
            # 初始上限（版本 0，自货物起始时刻生效；完整历程见 temp_standards）
            "temp_upper_limit": temp(cargo.temp_upper_limit),
            "initial_holder": cargo.initial_holder,
        },
        "interval": _interval_dict(ex),
        "current_valid_stances": [
            _stance_item(sv) for sv in interval_view.stances
        ],
        "summary": interval_view.summary,
        "own_events": own_items,
        "vehicle_readings": reading_items,
        "loading_windows": loading_items,
        # 封存时的完整温度标准历程（初始版本 + 历次变更，按生效时刻升序）；
        # append-only，封存后新变更绝不改写本包，只在现况对照中体现差异。
        "temp_standards": [_standard_item(e) for e in standards],
    }
    canonical = canonical_json(content)
    content_with_digest = dict(content)
    content_with_digest["content_digest"] = digest_of(canonical)
    return SealPayload(
        content=content_with_digest,
        canonical=canonical,
        content_digest=content_with_digest["content_digest"],
        snapshot_id=resp.snapshot_id(ex),
        sealed_at=sealed_at_s,
    )


# ----------------------------------------------------------- 现况重建


@dataclass(frozen=True)
class CurrentState:
    """封存区间在当前数据下的重建状态。"""

    status: str
    current_ex: object | None        # 仍闭合时的 ExcursionResult
    open_ex: object | None           # 重开时的开放 ExcursionResult
    current_snapshot_id: str | None
    current_view: object | None      # 仍闭合时的 IntervalView
    cargo_start: object              # 货物起始时间（消失对照的证据下界）
    events: list
    readings: list
    loadings: list
    effective: tuple
    quarantine: dict                 # 车载读数ID -> 生效探头故障编号元组
    standards: tuple = ()            # 当前完整温度标准历程（含初始版本）


def rebuild_current(db, cargo, interval_key: str) -> CurrentState:
    """按当前数据重建目标区间状态（读事务内调用；不抛 404/422）。"""
    events, loadings, readings, effective, quarantine, standards = (
        timeline.load_cargo_context(db, cargo.cargo_id)
    )
    cargo_start = ensure_utc(cargo.started_at)
    chain_result, closed = resp.rebuild(
        cargo, events, loadings, readings, effective, quarantine, standards
    )
    ex = closed.get(interval_key)
    open_ex = next(
        (e for e in chain_result.excursions if interval_id(e) == interval_key),
        None,
    )

    from app import stance_crud

    common = dict(
        cargo_start=cargo_start,
        events=events,
        readings=readings,
        loadings=loadings,
        effective=effective,
        quarantine=quarantine,
        standards=standards,
    )
    if ex is not None:
        rows = stance_crud.load_interval_stances(
            db, cargo.cargo_id, interval_key
        )
        view = resp.build_interval_view(cargo.cargo_id, ex, rows)
        return CurrentState(
            status=STATUS_UNCHANGED,
            current_ex=ex,
            open_ex=None,
            current_snapshot_id=resp.snapshot_id(ex),
            current_view=view,
            **common,
        )
    if open_ex is not None:
        return CurrentState(
            status=STATUS_REOPENED,
            current_ex=None,
            open_ex=open_ex,
            current_snapshot_id=None,
            current_view=None,
            **common,
        )
    return CurrentState(
        status=STATUS_DISAPPEARED,
        current_ex=None,
        open_ex=None,
        current_snapshot_id=None,
        current_view=None,
        **common,
    )


# ----------------------------------------------------------- 现况对照


def _measurement_evidence_diff(
    sealed_items: list[dict],
    current_items: list[dict],
    key: str,
    default_limit,
):
    """测温类证据（自有测温 / 车载读数）逐项比对。

    既有封存包可能没有每条证据的 ``temp_upper_limit`` 字段（标准分时生效
    本轮新增）：比较前对封存侧缺失值按“初始上限”补齐——旧包封存时标准
    历程只有版本 0，任何时刻的当时上限都等于初始上限；交接项恒为 null。
    这样标准变化引起的差异呈现为该证据项 ``changed``（字段
    ``temp_upper_limit``），既有封存包的其它对照语义完全不变。

    ``default_limit`` 为量化后的初始上限字符串；为 None 时不补齐（理论不
    发生：封存包 cargo.temp_upper_limit 恒有值）。
    """
    def _norm(item: dict) -> dict:
        if "temp_upper_limit" in item:
            return item
        is_measurement = item.get("event_type", "measurement") != "handover"
        return {
            **item,
            "temp_upper_limit": default_limit if is_measurement else None,
        }

    sealed_map = {item[key]: _norm(item) for item in sealed_items}
    current_map = {item[key]: _norm(item) for item in current_items}
    diffs = []
    for k in sorted(set(sealed_map) | set(current_map)):
        old = sealed_map.get(k)
        new = current_map.get(k)
        if old is None:
            diffs.append({"key": k, "change": ITEM_ADDED,
                          "sealed": None, "current": current_map[k]})
        elif new is None:
            diffs.append({"key": k, "change": ITEM_REMOVED,
                          "sealed": sealed_map[k], "current": None})
        elif old != new:
            changed_fields = sorted(
                {f for f in set(old) | set(new) if old.get(f) != new.get(f)}
            )
            diffs.append(
                {
                    "key": k,
                    "change": ITEM_CHANGED,
                    "fields": changed_fields,
                    # 输出仍给原始封存项（旧包不被凭空补出字段）
                    "sealed": next(i for i in sealed_items if i[key] == k),
                    "current": next(i for i in current_items if i[key] == k),
                }
            )
    return diffs


def _evidence_diff(sealed_items: list[dict], current_items: list[dict], key: str):
    """按键归集的证据项逐项比对：added / removed / changed（含变更字段）。"""
    sealed_map = {item[key]: item for item in sealed_items}
    current_map = {item[key]: item for item in current_items}
    diffs = []
    for k in sorted(set(sealed_map) | set(current_map)):
        old = sealed_map.get(k)
        new = current_map.get(k)
        if old is None:
            diffs.append({"key": k, "change": ITEM_ADDED, "sealed": None, "current": new})
        elif new is None:
            diffs.append({"key": k, "change": ITEM_REMOVED, "sealed": old, "current": None})
        elif old != new:
            changed_fields = sorted({f for f in old if old.get(f) != new.get(f)})
            diffs.append(
                {
                    "key": k,
                    "change": ITEM_CHANGED,
                    "fields": changed_fields,
                    "sealed": old,
                    "current": new,
                }
            )
    return diffs


def _vehicle_reading_diff(
    sealed_items: list[dict],
    current_items: list[dict],
    default_limit=None,
):
    """车载读数证据项对照（键 ``reading_id``），在通用增删变更之上叠加
    探头故障隔离语义：

    * **封存后才隔离**：封存时未隔离（``quarantined`` 缺省 / false）、当前
      被生效故障覆盖——读数不再参与重建，记为 ``removed``（沿用既有封存包
      “读数被隔离即 removed”的语义，封存侧并无隔离事实可作新旧对照）；
    * **封存时已隔离、当前恢复**：记为 ``changed``（adopted / quarantined /
      covering_fault_ids 变化；同刻自有测温仍优先时 adopted 保持 false）；
    * **封存时已隔离、当前仍隔离**：覆盖故障集合变化（重叠故障撤销一条后
      仍被另一条覆盖）记为 ``changed``（covering_fault_ids），否则无差异；
    * 其余（适用性消失、温度更正、**标准变化引起的当时上限变化**）按通用
      added / removed / changed 处理。

    旧封存包的读数项没有 ``quarantined`` / ``covering_fault_ids`` 字段，
    比较前按缺省值（false / 空）归一化，保证旧包对照语义完全不变；
    ``temp_upper_limit`` 缺失（标准分时生效本轮新增）按初始上限补齐，
    使标准变化呈现为 ``changed``（字段 temp_upper_limit）。
    """
    def _norm(item: dict) -> dict:
        return {
            **item,
            "quarantined": bool(item.get("quarantined", False)),
            "covering_fault_ids": list(item.get("covering_fault_ids", [])),
            "temp_upper_limit": (
                item["temp_upper_limit"]
                if "temp_upper_limit" in item
                else default_limit
            ),
        }

    # 归一化副本仅用于字段比较；diff 输出的 sealed / current 仍为原始项
    # （旧封存包读数不被凭空补出 quarantined 键）。
    raw_sealed = {item["reading_id"]: item for item in sealed_items}
    raw_current = {item["reading_id"]: item for item in current_items}
    sealed_map = {k: _norm(v) for k, v in raw_sealed.items()}
    current_map = {k: _norm(v) for k, v in raw_current.items()}
    diffs = []
    for k in sorted(set(sealed_map) | set(current_map)):
        old = sealed_map.get(k)
        new = current_map.get(k)
        if old is None:
            diffs.append({"key": k, "change": ITEM_ADDED,
                          "sealed": None, "current": raw_current[k]})
            continue
        if new is None:
            diffs.append({"key": k, "change": ITEM_REMOVED,
                          "sealed": raw_sealed[k], "current": None})
            continue

        old_q = old["quarantined"]
        new_q = new["quarantined"]
        if not old_q and new_q:
            # 封存后才被生效故障覆盖：读数退出重建 -> 既有 removed 语义
            diffs.append({"key": k, "change": ITEM_REMOVED,
                          "sealed": raw_sealed[k], "current": None})
            continue

        if old != new:
            changed_fields = sorted(
                {f for f in set(old) | set(new) if old.get(f) != new.get(f)}
            )
            diffs.append(
                {
                    "key": k,
                    "change": ITEM_CHANGED,
                    "fields": changed_fields,
                    "sealed": raw_sealed[k],
                    "current": raw_current[k],
                }
            )
    return diffs


def _segment_diff(sealed_segments: list[list], current_segments: list[list] | None):
    """责任片段增删变更：以片段起点为稳定键，比对终点与责任方。

    current_segments 为 None（区间重开 / 消失）时，封存片段全部为 removed。
    开放区间当前片段也一并给出（其末段终点为 null）。
    """
    current_segments = current_segments or []
    sealed_map = {s[0]: s for s in sealed_segments}
    current_map = {s[0]: s for s in current_segments}
    diffs = []
    for start in sorted(set(sealed_map) | set(current_map)):
        old = sealed_map.get(start)
        new = current_map.get(start)
        if old is None:
            diffs.append({"start": start, "change": ITEM_ADDED,
                          "sealed": None, "current": new})
        elif new is None:
            diffs.append({"start": start, "change": ITEM_REMOVED,
                          "sealed": old, "current": None})
        elif old != new:
            diffs.append({"start": start, "change": ITEM_CHANGED,
                          "sealed": old, "current": new})
    return diffs


def _current_evidence(state: CurrentState, start_bound, span_end):
    """按封存相同的范围定义归集当前证据。

    自有事件与封存一致：不施下界（不晚于上界的全部事件——含开启前保管链），
    ``start_bound`` 只约束车载读数与装卸窗口。被生效探头故障覆盖的读数不
    参与重建，但**仍计入当前证据**并带 quarantined / covering_fault_ids
    标记；与封存项之间的增 / 删 / 变更（含“封存后才隔离”）由
    :func:`_vehicle_reading_diff` 按隔离语义裁决。
    """
    return scoped_evidence(
        state.events,
        state.readings,
        state.loadings,
        state.effective,
        start_bound,
        span_end,
        apply_window_filter=False,
        reading_quarantine=state.quarantine,
        standards=state.standards,
    )


def _temp_standard_diff(sealed_standards: list[dict] | None, current_standards: list[dict]):
    """标准历程对照：以版本号（提交序号）为稳定键，逐项 added / removed /
    changed（标准 append-only，正常只有 added）。

    旧封存包无 ``temp_standards`` 段时按“仅有初始版本（版本 0）”解释：
    初始版本内容取自封存包 cargo 段（起始时刻 + 初始上限），变更项全部
    表现为 added——完整呈现标准变化，且绝不改写原包。
    """
    if sealed_standards is None:
        sealed_standards = []

    sealed_map = {int(s["version"]): s for s in sealed_standards}
    current_map = {int(s["version"]): s for s in current_standards}
    diffs = []
    for v in sorted(set(sealed_map) | set(current_map)):
        old = sealed_map.get(v)
        new = current_map.get(v)
        if old is None:
            diffs.append({"key": str(v), "change": ITEM_ADDED,
                          "sealed": None, "current": new})
        elif new is None:
            diffs.append({"key": str(v), "change": ITEM_REMOVED,
                          "sealed": old, "current": None})
        elif old != new:
            changed_fields = sorted(
                {f for f in set(old) | set(new) if old.get(f) != new.get(f)}
            )
            diffs.append({"key": str(v), "change": ITEM_CHANGED,
                          "fields": changed_fields, "sealed": old, "current": new})
    return diffs


def build_diff(content: dict, state: CurrentState, now_ub) -> dict:
    """封存包内容 vs 当前重建状态的对照结果。

    :param content: 封存时的内容字典（含 interval / 各证据项列表）。
    :param now_ub: 区间重开时的证据范围上界（查询时刻，UTC）；
        区间消失时无上界（开放结束的区间可能因晚到记录而消失，
        取当时全部自有事件 / 在车读数 / 相交窗口比对）。
    """
    sealed_interval = content["interval"]
    sealed_segments = sealed_interval["segments"]

    # 旧封存包（标准分时生效本轮之前）没有 temp_standards 段：按“仅有初始
    # 版本”解释，初始版本取自封存包 cargo 段；新包直接使用封存历程。
    sealed_standards = content.get("temp_standards")
    if sealed_standards is None:
        sealed_standards = [
            {
                "version": 0,
                "effective_at": content["cargo"]["started_at"],
                "temp_upper_limit": content["cargo"]["temp_upper_limit"],
                "change_id": None,
                "reason": None,
            }
        ]
    default_limit = content["cargo"]["temp_upper_limit"]
    current_standards = [_standard_item(e) for e in state.standards]

    if state.status == STATUS_UNCHANGED:
        ex = state.current_ex
        cur_start, cur_end = ensure_utc(ex.start), ensure_utc(ex.end)
        own_now, read_now, load_now = _current_evidence(
            state, cur_start, cur_end
        )
        current_interval = _interval_dict(ex)
        current_segments = current_interval["segments"]
        status = (
            STATUS_UNCHANGED
            if state.current_snapshot_id == sealed_interval["snapshot_id"]
            else STATUS_CHANGED
        )
    elif state.status == STATUS_REOPENED:
        ex = state.open_ex
        cur_start = ensure_utc(ex.start)
        own_now, read_now, load_now = _current_evidence(
            state, cur_start, now_ub
        )
        current_interval = _open_interval_dict(ex)
        current_segments = current_interval["segments"]
        status = STATUS_REOPENED
    else:
        # 区间消失：证据范围下界放宽到货物起始时间（新的开启点 / 窗口更正
        # 可能把适用读数带到封存起点之前），上界不限——晚到记录 / 测温更正 /
        # 窗口更正都可能是区间消失的原因，需逐项列出。
        own_now, read_now, load_now = _current_evidence(
            state, state.cargo_start, None
        )
        current_interval = None
        current_segments = None
        status = STATUS_DISAPPEARED

    return {
        "case_id": content["case_id"],
        "cargo_id": content["cargo"]["cargo_id"],
        "interval_id": sealed_interval["interval_id"],
        "status": status,
        "sealed_snapshot_id": sealed_interval["snapshot_id"],
        "current_snapshot_id": state.current_snapshot_id,
        "sealed_at": content["sealed_at"],
        "compared_at": ts(now_ub),
        "current_interval": current_interval,
        "segment_changes": _segment_diff(sealed_segments, current_segments),
        "evidence_changes": {
            "own_events": _measurement_evidence_diff(
                content["own_events"], own_now, "event_id", default_limit
            ),
            "vehicle_readings": _vehicle_reading_diff(
                content["vehicle_readings"], read_now, default_limit
            ),
            "loading_windows": _loading_window_diff(
                content["loading_windows"], load_now
            ),
        },
        # 完整温度标准历程的现况与增 / 删 / 变更（append-only，标准变化只
        # 影响对照结果，绝不改写封存原包；旧包按“仅初始版本”对照）
        "sealed_temp_standards": sealed_standards,
        "current_temp_standards": current_standards,
        "temp_standard_changes": _temp_standard_diff(
            content.get("temp_standards"), current_standards
        ),
    }


def _loading_window_diff(sealed_items: list[dict], current_items: list[dict]):
    """装卸窗口以 (trip_id, origin_loaded_at) 为稳定键比对。"""
    def key_of(item):
        return f"{item['trip_id']}|{item['origin_loaded_at']}"

    sealed_pairs = [(key_of(i), i) for i in sealed_items]
    current_pairs = [(key_of(i), i) for i in current_items]
    sealed_map = dict(sealed_pairs)
    current_map = dict(current_pairs)
    diffs = []
    for k in sorted(set(sealed_map) | set(current_map)):
        old = sealed_map.get(k)
        new = current_map.get(k)
        if old is None:
            diffs.append({"key": k, "change": ITEM_ADDED, "sealed": None, "current": new})
        elif new is None:
            diffs.append({"key": k, "change": ITEM_REMOVED, "sealed": old, "current": None})
        elif old != new:
            changed_fields = sorted({f for f in old if old.get(f) != new.get(f)})
            diffs.append(
                {
                    "key": k,
                    "change": ITEM_CHANGED,
                    "fields": changed_fields,
                    "sealed": old,
                    "current": new,
                }
            )
    return diffs


def _open_interval_dict(ex) -> dict:
    return {
        "interval_id": interval_id(ex),
        "start": ts(ex.start),
        "end": None,
        "is_open": True,
        "start_source": ex.start_source,
        "end_source": None,
        "start_event_id": ex.start_event_id,
        "end_event_id": None,
        "start_reading_id": ex.start_reading_id,
        "end_reading_id": None,
        "start_trip_id": ex.start_trip_id,
        "end_trip_id": None,
        "snapshot_id": None,
        "segments": _seg_triples(ex.segments),
    }


def _parse_sealed_ts(value: str):
    from datetime import datetime

    return datetime.fromisoformat(value.replace("Z", "+00:00"))
