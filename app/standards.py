"""货物温度上限分时生效标准：标准历程读取与“当时上限”解析。

每票货物的温度上限是一条**左闭右开阶梯函数**：

* 初始上限存于 ``cargo.temp_upper_limit``，作为标准**版本 0** 自货物起始
  时刻 ``cargo.started_at`` 生效（既有字段语义保持不变）；
* 质控人员可经 ``POST /cargo/{id}/temp-standards`` 追加变更
  （:class:`app.models.TempStandardChange`，append-only），每行带带时区
  生效时刻、新上限与提交版本；
* 一笔在 t 时刻**已采用**的测温读数（自有 / 车载）越温与否，取“生效时刻
  ``<= t`` 的最新一条标准”的上限——**恰在生效时刻的读数采用新上限**
  （左闭）；早于货物起始时刻不存在读数（写入侧已拒绝）。

允许补录（生效时刻早于提交时间）与预约（晚于提交时间），故标准历程按
``effective_at`` 排序后可能出现“版本号倒序”的历史条目；越温重建始终
按生效时刻取当时上限，与提交先后无关。标准版本（乐观锁）按提交序号：
``expected_version`` 必须等于该货物已提交的变更数（``max(version)``，
无变更为 0）。

本模块只负责读取与解析；写入事务见 :mod:`app.standard_crud`。
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import select

from app.models import TempStandardChange
from app.timeutil import ensure_utc


@dataclass(frozen=True)
class StandardEntry:
    """标准历程中的一个生效版本（初始版本或一次变更）。"""

    version: int
    effective_at: object          # 生效时刻（含），UTC
    temp_upper_limit: Decimal
    change_id: str | None = None  # 初始版本恒为 None；变更为其变更编号
    reason: str | None = None     # 初始版本恒为 None


def load_standard_changes(db, cargo_id: str) -> list[TempStandardChange]:
    """一票货物的全部标准变更行（按生效时刻升序；同刻至多一条）。"""
    rows = list(
        db.scalars(
            select(TempStandardChange)
            .where(TempStandardChange.cargo_id == cargo_id)
            .order_by(
                TempStandardChange.effective_at, TempStandardChange.change_id
            )
        ).all()
    )
    for r in rows:
        r.effective_at = ensure_utc(r.effective_at)
    return rows


def build_series(cargo, changes: list[TempStandardChange] | None = None) -> tuple[StandardEntry, ...]:
    """组装完整标准历程（含初始版本，按生效时刻升序）。

    变更行允许补录 / 预约，故按生效时刻而非提交版本排序；初始版本固定
    占据货物起始时刻（该时刻不允许变更，应用侧保证）。
    """
    started_at = ensure_utc(cargo.started_at)
    entries = [
        StandardEntry(
            version=0,
            effective_at=started_at,
            temp_upper_limit=Decimal(cargo.temp_upper_limit),
        )
    ]
    for r in changes or []:
        entries.append(
            StandardEntry(
                version=r.version,
                effective_at=ensure_utc(r.effective_at),
                temp_upper_limit=Decimal(r.new_upper_limit),
                change_id=r.change_id,
                reason=r.reason,
            )
        )
    return tuple(sorted(entries, key=lambda e: e.effective_at))


def load_series(db, cargo) -> tuple[StandardEntry, ...]:
    """读取一票货物的完整标准历程（含初始版本）。"""
    return build_series(cargo, load_standard_changes(db, cargo.cargo_id))


def limit_at(series: tuple[StandardEntry, ...], at) -> Decimal:
    """t 时刻生效的温度上限：生效时刻 <= t 的最新一条（左闭，边界取新上限）。

    早于首个生效时刻（货物起始时刻）的查询返回初始上限（该情形在正常
    重建中不会出现——读数不允许早于货物起始时刻）。
    """
    at = ensure_utc(at)
    current = series[0]
    for e in series:
        if e.effective_at <= at:
            current = e
        else:
            break
    return Decimal(current.temp_upper_limit)


def entry_at(series: tuple[StandardEntry, ...], at) -> StandardEntry:
    """t 时刻生效的标准条目（版本号 / 生效时刻 / 上限），左闭。"""
    at = ensure_utc(at)
    current = series[0]
    for e in series:
        if e.effective_at <= at:
            current = e
        else:
            break
    return current


def submitted_version(changes: list[TempStandardChange] | None = None) -> int:
    """当前标准版本（乐观锁口径）：已提交变更的最大版本号；无变更为 0。"""
    return max((r.version for r in (changes or [])), default=0)


def current_entry(series: tuple[StandardEntry, ...], at) -> StandardEntry:
    """给定“现在”时刻（通常为提交时刻）的当前生效条目——仅统计生效时刻
    已到达（含）的版本；预约版本尚未生效时返回前一条。
    """
    return entry_at(series, at)
