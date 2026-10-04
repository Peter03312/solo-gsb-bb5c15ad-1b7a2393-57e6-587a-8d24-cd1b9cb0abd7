"""货物温度上限分时生效标准变更的写入 / 查询事务。

质控人员可凭**变更编号**、**货物编号**、**带时区生效时刻**、**新上限**、
**期望标准版本**与**原因**提交一次标准变更。初始上限存于
``cargo.temp_upper_limit``（版本 0，自货物起始时刻生效），每次成功变更
向 ``temp_standard_change`` 追加一行（append-only，不修改不删除）。

校验（持货物行锁的单事务内完成，失败整次回滚、不留部分写入）
----------------------------------------------------------------
* 货物不存在 → 404 ``cargo_not_found``；
* 生效时刻必须显式携带时区（路由侧）；早于货物起始时刻 →
  422 ``before_start``；等于货物起始时刻（初始版本已占该时刻）→
  422 ``standard_initial_slot``；
* 同一货物同一生效时刻至多一条变更 → 409 ``same_timestamp_conflict``
  （允许补录与预约）；
* ``expected_version`` 必须等于当前标准版本（已提交变更数，首次为 0）
  → 否则 409 ``stale_version``；
* 原因为空 / 空白 → 422 ``bad_standard_change_shape``。

加锁与串行
----------
变更写入只锁该票货物行，与该货物的自有事件、车载读数、责任表态、测温
更正、装卸登记 / 更正、证据封存严格串行——“并发变更与读数、装卸、封存
操作须保持一致视图”。变更**不产生**任何测温或交接事件，不改动车次
修订号（车次预检摘要绑定完整标准历程，标准变化经摘要过期体现）。

幂等
----
``change_id`` 同请求（货物、生效时刻、新上限、期望版本、原因全相同）
重传返回原结果（``200 replayed``）；同编号异内容 →
409 ``change_id_conflict``。
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, OperationalError

from app import standards as std_mod
from app.crud import ApiError, _load_cargo_locked
from app.models import TempStandardChange
from app.schemas import TempStandardChangeCreate
from app.timeutil import ensure_utc


def _conflict(message: str, code: str) -> ApiError:
    return ApiError(409, code, message)


def _unprocessable(message: str, code: str) -> ApiError:
    return ApiError(422, code, message)


@dataclass
class StandardChangeResult:
    change: TempStandardChange
    replayed: bool
    cargo_id: str
    # 变更后的当前标准版本（= 已提交变更数 = 本次行版本）
    version: int
    new_upper_limit: Decimal


def _same_content(
    existing: TempStandardChange,
    cargo_id: str,
    effective_at,
    new_upper_limit: Decimal,
    expected_version: int,
    reason: str,
) -> bool:
    """变更编号相同前提下逐字段比对请求内容。"""
    return (
        existing.cargo_id == cargo_id
        and ensure_utc(existing.effective_at) == effective_at
        and Decimal(existing.new_upper_limit) == Decimal(new_upper_limit)
        and existing.expected_version == expected_version
        and existing.reason == reason
    )


def _replay_view(row: TempStandardChange) -> StandardChangeResult:
    return StandardChangeResult(
        change=row,
        replayed=True,
        cargo_id=row.cargo_id,
        version=row.version,
        new_upper_limit=Decimal(row.new_upper_limit),
    )


def submit_change(db, cargo_id: str, payload: TempStandardChangeCreate) -> StandardChangeResult:
    try:
        return _submit_tx(db, cargo_id, payload)
    except OperationalError:
        # SQLite 文件库锁等待耗尽 / PG 死锁检测中止：整事务重放，语义不变。
        db.rollback()
        winner = db.get(TempStandardChange, payload.change_id)
        if winner is not None and _same_content(
            winner,
            cargo_id,
            ensure_utc(payload.effective_at),
            Decimal(payload.new_upper_limit),
            payload.expected_version,
            payload.reason,
        ):
            return _replay_view(winner)
        return _submit_tx(db, cargo_id, payload)


def _submit_tx(db, cargo_id: str, payload: TempStandardChangeCreate) -> StandardChangeResult:
    effective_at = ensure_utc(payload.effective_at)
    new_upper_limit = Decimal(payload.new_upper_limit).quantize(Decimal("0.001"))
    expected_version = payload.expected_version
    reason = payload.reason
    change_id = payload.change_id

    # 幂等快路径：变更编号已存在（通常是串行重传）时无需加锁即可裁决
    existing = db.get(TempStandardChange, change_id)
    if existing is not None:
        if not _same_content(
            existing, cargo_id, effective_at, new_upper_limit,
            expected_version, reason,
        ):
            raise _conflict(
                f"标准变更编号 {change_id} 已存在且请求内容不同，拒绝覆盖",
                code="change_id_conflict",
            )
        db.rollback()  # 仅结束只读事务，未做修改
        return _replay_view(existing)

    try:
        # 与同货物的事件 / 读数 / 表态 / 更正 / 装卸 / 封存串行：锁货物行
        cargo = _load_cargo_locked(db, cargo_id)

        # 持锁后复核幂等（并发首传竞争在此分出唯一赢家）
        winner = db.get(TempStandardChange, change_id)
        if winner is not None:
            if not _same_content(
                winner, cargo_id, effective_at, new_upper_limit,
                expected_version, reason,
            ):
                raise _conflict(
                    f"标准变更编号 {change_id} 已存在且请求内容不同，拒绝覆盖",
                    code="change_id_conflict",
                )
            db.rollback()
            return _replay_view(winner)

        changes = std_mod.load_standard_changes(db, cargo_id)

        # 生效时刻早于货物起始时刻：拒绝（初始上限覆盖起始时刻起全程）
        if effective_at < cargo.started_at:
            raise _unprocessable(
                f"标准变更生效时刻 {effective_at.isoformat()} 早于货物 "
                f"{cargo_id} 起始时刻 {cargo.started_at.isoformat()}，"
                "初始上限自货物起始时刻生效，不接受更早的标准",
                code="before_start",
            )
        # 货物起始时刻属于初始版本（版本 0），该时刻不可再登记变更
        if effective_at == cargo.started_at:
            raise _unprocessable(
                f"标准变更生效时刻 {effective_at.isoformat()} 等于货物 "
                f"{cargo_id} 的起始时刻：初始上限（版本 0）自该时刻生效，"
                "请选择严格晚于货物起始时刻的生效时刻",
                code="standard_initial_slot",
            )

        # 同刻仅一条：同一货物同一生效时刻不允许重复变更
        clash = next(
            (r for r in changes if ensure_utc(r.effective_at) == effective_at),
            None,
        )
        if clash is not None:
            raise _conflict(
                f"货物 {cargo_id} 在 {effective_at.isoformat()} 已存在标准变更 "
                f"{clash.change_id}（版本 {clash.version}），同一时刻仅允许一条；"
                "标准变更 append-only，不能覆盖同刻上限",
                code="same_timestamp_conflict",
            )

        # 乐观锁：期望版本必须等于当前标准版本（已提交变更数）
        current_version = std_mod.submitted_version(changes)
        if expected_version != current_version:
            raise _conflict(
                f"标准版本过期：货物 {cargo_id} 当前标准版本为 "
                f"{current_version}，请求期望版本为 {expected_version}，"
                "请先查询标准历程获取当前版本后再提交",
                code="stale_version",
            )

        row = TempStandardChange(
            change_id=change_id,
            cargo_id=cargo_id,
            effective_at=effective_at,
            new_upper_limit=new_upper_limit,
            expected_version=expected_version,
            version=current_version + 1,
            reason=reason,
        )
        db.add(row)
        db.commit()
    except ApiError:
        db.rollback()
        # SQLite 文件库 FOR UPDATE 为空操作：版本过期可能只因同内容并发变更
        # 已先提交——回滚后按幂等返回原结果。
        replay = _replay_after_conflict(
            db, change_id, cargo_id, effective_at, new_upper_limit,
            expected_version, reason,
        )
        if replay is not None:
            return replay
        raise
    except IntegrityError:
        db.rollback()
        replay = _replay_after_conflict(
            db, change_id, cargo_id, effective_at, new_upper_limit,
            expected_version, reason,
        )
        if replay is not None:
            return replay
        raise _conflict(
            f"标准变更 {change_id} 与既有数据冲突"
            "（变更编号冲突 / 同刻变更 / 版本竞争）",
            code="integrity_conflict",
        )

    row.effective_at = ensure_utc(row.effective_at)
    return StandardChangeResult(
        change=row,
        replayed=False,
        cargo_id=cargo_id,
        version=row.version,
        new_upper_limit=new_upper_limit,
    )


def _replay_after_conflict(
    db, change_id, cargo_id, effective_at, new_upper_limit,
    expected_version, reason,
) -> StandardChangeResult | None:
    """冲突回滚后：赢家是同变更编号同请求则按幂等返回，否则 None。"""
    winner = db.get(TempStandardChange, change_id)
    if winner is None:
        return None
    if not _same_content(
        winner, cargo_id, effective_at, new_upper_limit,
        expected_version, reason,
    ):
        return None
    return _replay_view(winner)


# ----------------------------------------------------------- 查询


def get_standard_history(db, cargo) -> tuple[tuple[std_mod.StandardEntry, ...], list[TempStandardChange], int]:
    """一票货物的完整标准历程（含初始版本）、变更行与当前标准版本。"""
    changes = std_mod.load_standard_changes(db, cargo.cargo_id)
    series = std_mod.build_series(cargo, changes)
    return series, changes, std_mod.submitted_version(changes)
