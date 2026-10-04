"""货物温度上限标准（分时生效）：读取、阶梯函数与纯逻辑。

货物的温度上限是一条**左闭右开阶梯函数**：

* **初始上限**（``cargo.temp_upper_limit``）自货物起始时刻
  ``cargo.started_at`` 起生效；
* 质控人员提交的每条标准变更（:class:`app.models.TemperatureLimitChange`，
  append-only）把自其带时区生效时刻（**含边界**——恰在生效时刻的读数采用
  新上限）起的上限切换为新值；变更允许**补录**（生效时刻早于当前时刻，
  历史重建随之改变）与**预约**（晚于当前时刻，到达时刻前重建仍用旧上限）。

标准版本：初始为 0，每条成功变更 +1；``expected_version`` 为提交变更时的
乐观锁。查询接口返回**完整标准历程**（初始上限条目 version=0 + 各变更条目
按生效时刻 / 版本升序）与当前版本。

所有越温判定（货物时间线、越温报告、责任确认、车次同时越温、封存证据中
每笔已采用读数）都按读数**当时生效的上限**重建：

    limit_at(t) = 生效时刻 <= t 的最后一条标准（无则初始上限）
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import select

from app.timeutil import ensure_utc

# 标准条目来源
SOURCE_INITIAL = "initial"   # 货物初始上限（自货物起始时刻生效）
SOURCE_CHANGE = "change"     # 质控人员登记的标准变更


@dataclass(frozen=True)
class LimitEntry:
    """标准历程中的一个条目（阶梯函数的一个台阶）。"""

    effective_at: object          # 生效时刻（含边界），UTC
    temp_upper_limit: Decimal
    version: int                  # 0=初始上限；n>=1=第 n 条变更后的标准版本
    source: str = SOURCE_INITIAL
    change_id: str | None = None  # 变更条目编号；初始条目为 None
    reason: str | None = None     # 变更原因；初始条目为 None


def build_limit_history(cargo, changes) -> tuple[LimitEntry, ...]:
    """由货物行（初始上限）与变更行组装完整标准历程（按生效时刻 / 版本升序）。

    初始条目恒为首条（生效时刻 = 货物起始时刻、version=0）；变更条目按
    ``(effective_at, version)`` 升序——补录 / 预约都只按生效时刻决定上限，
    与登记先后无关。
    """
    started = ensure_utc(cargo.started_at)
    initial = LimitEntry(
        effective_at=started,
        temp_upper_limit=Decimal(cargo.temp_upper_limit),
        version=0,
        source=SOURCE_INITIAL,
    )
    entries = [initial]
    for row in changes:
        entries.append(
            LimitEntry(
                effective_at=ensure_utc(row.effective_at),
                temp_upper_limit=Decimal(row.new_limit),
                version=row.version,
                source=SOURCE_CHANGE,
                change_id=row.change_id,
                reason=row.reason,
            )
        )
    entries.sort(key=lambda e: (e.effective_at, e.version))
    return tuple(entries)


def current_standard_version(changes) -> int:
    """货物标准的当前版本：变更行数（每条成功变更 +1）。"""
    return len(changes)


def limit_resolver(history):
    """由标准历程构造 ``at -> 当时生效上限`` 的阶梯函数（左闭边界）。

    返回的闭包对任意时刻返回 ``effective_at <= at`` 的最后一条标准的上限；
    货物起始时刻之前同样返回初始上限（早于起始时刻的测温 / 变更均在写入侧
    拒绝，重建数据不会落在该区间，此处仅作确定性兜底）。
    """
    entries = tuple(history)

    def resolve(at) -> Decimal:
        at = ensure_utc(at)
        chosen = entries[0]
        for entry in entries:
            if entry.effective_at <= at:
                chosen = entry
            else:
                break
        return Decimal(chosen.temp_upper_limit)

    return resolve


def entry_at(history, at) -> LimitEntry:
    """时刻 at 生效的标准条目（边界读数采用新上限）。"""
    at = ensure_utc(at)
    chosen = history[0]
    for entry in history:
        if entry.effective_at <= at:
            chosen = entry
        else:
            break
    return chosen


def load_limit_changes(db, cargo_id: str):
    """读取一票货物的全部温度上限标准变更（按生效时刻 / 版本升序）。"""
    from app.models import TemperatureLimitChange

    rows = list(
        db.scalars(
            select(TemperatureLimitChange)
            .where(TemperatureLimitChange.cargo_id == cargo_id)
            .order_by(
                TemperatureLimitChange.effective_at,
                TemperatureLimitChange.version,
            )
        ).all()
    )
    for row in rows:
        row.effective_at = ensure_utc(row.effective_at)
    return rows


def load_limit_history(db, cargo):
    """一次读齐一票货物的完整标准历程（初始上限 + 变更条目）。"""
    return build_limit_history(cargo, load_limit_changes(db, cargo.cargo_id))
