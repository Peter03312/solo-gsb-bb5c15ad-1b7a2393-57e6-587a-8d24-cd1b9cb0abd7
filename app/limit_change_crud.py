"""货物温度上限标准变更的写入事务（分时生效，append-only）。

质控人员可凭**变更编号**、货物编号、**带时区生效时刻**、新上限、
**期望标准版本**与原因登记一条温度上限标准变更。初始上限
（``cargo.temp_upper_limit``）自货物起始时刻生效，不在本表建行；每条
变更把自生效时刻（左闭，边界读数采用新上限）起的上限切换为新值，
允许补录（生效时刻早于当前时刻）与预约（晚于当前时刻）。变更本身
不产生测温或交接。

校验（全部在持货物行锁事务内，失败整次回滚、不留部分写入）
----------------------------------------------------------
* 货物不存在 → ``404 cargo_not_found``；
* 生效时刻必须显式带时区 → ``422 timezone_required``（路由层）；
* 生效时刻早于货物起始时刻 → ``422 before_start``；
* 新上限须为有限数值 → ``422 bad_temp_limit_shape``；原因必填非空白
  （请求模型校验）；
* ``expected_version`` 须等于该货物标准当前版本（变更行数）→ 否则
  ``409 stale_standard_version``；
* 同一货物同一生效时刻至多一条变更 → ``409 same_timestamp_conflict``。

加锁与串行
----------
变更只锁所属货物行（``SELECT ... FOR UPDATE``），与同货物的事件写入、
车载读数写入（其货物行锁集合含本货物时）、测温更正、责任表态、装卸
登记 / 更正、探头故障（在车货物行锁集合含本货物时）与责任封存严格
串行，重建所见必为持锁期间的最新标准。标准变更**不推进车次修订号**：
它不改变车次读数 / 装卸，而装前预检摘要绑定涉及货物的完整标准历程，
标准变化后旧摘要自然过期（确认时 ``stale_precheck_digest``）。

幂等 / 冲突
-----------
``change_id`` 同请求（货物、生效时刻、期望版本、新上限、原因全相同）
重传返回原结果（``200 replayed``）；同编号异请求
→ ``409 limit_change_id_conflict``。
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, OperationalError

from app import standards as standards_mod
from app.crud import ApiError, _load_cargo_locked
from app.models import TemperatureLimitChange
from app.schemas import TempLimitChangeCreate
from app.timeutil import ensure_utc


def _conflict(message: str, code: str) -> ApiError:
    return ApiError(409, code, message)


def _unprocessable(message: str, code: str) -> ApiError:
    return ApiError(422, code, message)


@dataclass
class LimitChangeResult:
    change: TemperatureLimitChange
    replayed: bool
    cargo_id: str
    # 变更后的标准当前版本（= change.version）
    version: int


def _same_content(
    existing: TemperatureLimitChange,
    cargo_id: str,
    effective_at,
    expected_version: int,
    new_limit: Decimal,
    reason: str,
) -> bool:
    """变更编号相同前提下逐字段比对请求内容。"""
    return (
        existing.cargo_id == cargo_id
        and ensure_utc(existing.effective_at) == effective_at
        and existing.expected_version == expected_version
        and Decimal(existing.new_limit) == Decimal(new_limit)
        and existing.reason == reason
    )


def _replay_view(row: TemperatureLimitChange) -> LimitChangeResult:
    return LimitChangeResult(
        change=row,
        replayed=True,
        cargo_id=row.cargo_id,
        version=row.version,
    )


def submit_limit_change(
    db, cargo_id: str, payload: TempLimitChangeCreate
) -> LimitChangeResult:
    try:
        return _submit_tx(db, cargo_id, payload)
    except OperationalError:
        # SQLite 文件库锁等待耗尽 / PG 死锁检测中止：整事务重放，语义不变。
        # 重放前先按变更编号幂等检测，已落库的同请求返回原结果。
        db.rollback()
        winner = db.get(TemperatureLimitChange, payload.change_id)
        if winner is not None and _same_content(
            winner,
            cargo_id,
            ensure_utc(payload.effective_at),
            payload.expected_version,
            Decimal(payload.new_limit),
            payload.reason,
        ):
            return _replay_view(winner)
        return _submit_tx(db, cargo_id, payload)


def _submit_tx(
    db, cargo_id: str, payload: TempLimitChangeCreate
) -> LimitChangeResult:
    effective_at = ensure_utc(payload.effective_at)
    expected_version = payload.expected_version
    new_limit = Decimal(payload.new_limit).quantize(Decimal("0.001"))
    reason = payload.reason
    change_id = payload.change_id

    if not new_limit.is_finite():
        raise _unprocessable(
            f"新温度上限 {payload.new_limit!r} 非法：必须是有限数值（摄氏度）",
            code="bad_temp_limit_shape",
        )

    # 幂等快路径：变更编号已存在（通常是串行重传）时无需加锁即可裁决。
    existing = db.get(TemperatureLimitChange, change_id)
    if existing is not None:
        if not _same_content(
            existing, cargo_id, effective_at, expected_version, new_limit, reason
        ):
            raise _conflict(
                f"温度上限变更编号 {change_id} 已存在且请求内容不同，拒绝覆盖",
                code="limit_change_id_conflict",
            )
        db.rollback()  # 仅结束只读事务，未做修改
        return _replay_view(existing)

    try:
        # 与同货物的事件 / 读数 / 表态 / 更正 / 装卸 / 封存写入串行：锁货物行
        cargo = _load_cargo_locked(db, cargo_id)

        # 生效时刻不得早于货物起始时刻（初始上限自起始时刻生效）
        if effective_at < ensure_utc(cargo.started_at):
            raise _unprocessable(
                f"标准生效时刻 {effective_at.isoformat()} 早于货物起始时间 "
                f"{ensure_utc(cargo.started_at).isoformat()}：初始上限自货物"
                "起始时刻生效，变更不得早于该时刻",
                code="before_start",
            )

        # 持锁后复核幂等（并发首传竞争在此分出唯一赢家）
        winner = db.get(TemperatureLimitChange, change_id)
        if winner is not None:
            if not _same_content(
                winner, cargo_id, effective_at, expected_version,
                new_limit, reason,
            ):
                raise _conflict(
                    f"温度上限变更编号 {change_id} 已存在且请求内容不同，拒绝覆盖",
                    code="limit_change_id_conflict",
                )
            db.rollback()
            return _replay_view(winner)

        changes = standards_mod.load_limit_changes(db, cargo_id)
        current_version = standards_mod.current_standard_version(changes)

        # 乐观锁：期望标准版本须等于当前版本
        if expected_version != current_version:
            raise _conflict(
                f"标准版本过期：货物 {cargo_id} 温度上限标准当前版本为 "
                f"{current_version}，请求期望版本为 {expected_version}，"
                "请重新查询标准历程后再提交变更",
                code="stale_standard_version",
            )

        # 同一货物同一生效时刻至多一条变更
        if any(
            ensure_utc(row.effective_at) == effective_at for row in changes
        ):
            raise _conflict(
                f"货物 {cargo_id} 在 {effective_at.isoformat()} 已存在一条"
                "温度上限标准变更，同一时刻不允许重复",
                code="same_timestamp_conflict",
            )

        row = TemperatureLimitChange(
            change_id=change_id,
            cargo_id=cargo_id,
            effective_at=effective_at,
            expected_version=expected_version,
            version=current_version + 1,
            new_limit=new_limit,
            reason=reason,
        )
        db.add(row)
        db.commit()
    except ApiError:
        db.rollback()
        replay = _replay_after_conflict(
            db, change_id, cargo_id, effective_at, expected_version,
            new_limit, reason,
        )
        if replay is not None:
            return replay
        raise
    except IntegrityError:
        db.rollback()
        replay = _replay_after_conflict(
            db, change_id, cargo_id, effective_at, expected_version,
            new_limit, reason,
        )
        if replay is not None:
            return replay
        # 同刻 / 版本唯一约束兜底（锁串行下不应触发）
        winner2 = db.get(TemperatureLimitChange, change_id)
        if winner2 is None:
            raise _conflict(
                f"温度上限变更 {change_id} 与既有数据冲突"
                "（同货物同刻冲突 / 标准版本号竞争）",
                code="integrity_conflict",
            )
        raise _conflict(
            f"温度上限变更编号 {change_id} 已存在且请求内容不同，拒绝覆盖",
            code="limit_change_id_conflict",
        )

    row.effective_at = ensure_utc(row.effective_at)
    return LimitChangeResult(
        change=row,
        replayed=False,
        cargo_id=cargo_id,
        version=row.version,
    )


def _replay_after_conflict(
    db, change_id, cargo_id, effective_at, expected_version,
    new_limit, reason,
) -> LimitChangeResult | None:
    """回滚后：赢家是同编号同请求则按幂等返回原结果；否则 None。"""
    winner = db.get(TemperatureLimitChange, change_id)
    if winner is None:
        return None
    if not _same_content(
        winner, cargo_id, effective_at, expected_version, new_limit, reason
    ):
        return None
    return _replay_view(winner)
