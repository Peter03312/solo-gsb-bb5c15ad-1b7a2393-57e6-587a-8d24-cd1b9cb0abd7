"""货物分时生效温度上限（温度标准变更）API 集成测试。

覆盖：
* 提交：初始上限自货物起始时刻生效（历程首条 version=0）；变更成功推进
  标准版本，允许预约（未来生效）与补录（过去生效）；
* 边界读数采用新上限：恰在生效时刻的测温按新上限判定，时刻前按旧上限；
* 拒绝场景：货物不存在 404、缺时区 / 早于货物起始 / 原因为空 422、
  同刻冲突 / 期望版本过期 / 变更编号异内容 409，失败不留部分写入
  （编号可复用、版本不递增）；
* 幂等：同编号同请求重传返回原结果（200 replayed，含原期望版本）；
* 查询：完整标准历程 + 当前版本（初始条目 + 变更按生效时刻升序，
  current_limit 不含未来预约）；
* 重建：货物时间线逐点当时上限、越温报告按分时上限开闭区间、车载读数
  按读数时刻在车货物各自当时上限、车次详情 over_limit / temp_upper_limit、
  责任确认快照随标准变化失效与重建、车次同时越温按各自上限、
  装前预检在标准变更时刻分段且摘要纳入标准（补录后确认过期）、
  封存包逐笔记录当时上限且原包 / 摘要不变、现况对照体现标准变化。
"""
from fastapi.testclient import TestClient  # noqa: F401  (保持与其它测试一致的依赖)

from app.database import Base, SessionLocal, engine  # noqa: F401
from app.main import app  # noqa: F401
from app.models import TemperatureLimitChange


def setup_cargo(client, cargo="C1", limit="5", holder="A",
                start="2026-01-01T10:00:00+00:00"):
    r = client.post(
        "/cargo",
        json={
            "cargo_id": cargo,
            "started_at": start,
            "temp_upper_limit": limit,
            "initial_holder": holder,
        },
    )
    assert r.status_code == 201, r.text


def m(client, eid, minute, temp, cargo="C1", hour=10):
    return client.post(
        "/events",
        json={
            "event_id": eid,
            "cargo_id": cargo,
            "occurred_at": f"2026-01-01T{int(hour):02d}:{int(minute):02d}:00+00:00",
            "temperature": str(temp),
        },
    )


def h(client, eid, minute, giver, receiver, cargo="C1"):
    return client.post(
        "/events",
        json={
            "event_id": eid,
            "cargo_id": cargo,
            "occurred_at": f"2026-01-01T10:{minute:02d}:00+00:00",
            "giver": giver,
            "receiver": receiver,
        },
    )


def change_limit(
    client,
    change_id,
    effective,
    new_limit,
    expected_version=0,
    reason="产品要求收紧温控",
    cargo="C1",
):
    return client.post(
        f"/cargo/{cargo}/temp-limits",
        json={
            "change_id": change_id,
            "effective_at": effective,
            "new_limit": new_limit,
            "expected_version": expected_version,
            "reason": reason,
        },
    )


def history(client, cargo="C1"):
    return client.get(f"/cargo/{cargo}/temp-limits")


def report(client, cargo="C1"):
    return client.get(f"/cargo/{cargo}/report")


# --------------------------------------------------------------- 历程查询


def test_initial_history_has_initial_entry_version_zero(client):
    setup_cargo(client, limit="4")
    r = history(client)
    assert r.status_code == 200
    body = r.json()
    assert body["cargo_id"] == "C1"
    assert body["version"] == 0
    assert body["current_limit"] == "4.000"
    assert len(body["standards"]) == 1
    entry = body["standards"][0]
    assert entry == {
        "effective_at": "2026-01-01T10:00:00Z",
        "temp_upper_limit": "4.000",
        "version": 0,
        "source": "initial",
        "change_id": None,
        "expected_version": None,
        "reason": None,
        "created_at": None,
    }
    # 货物 temp_upper_limit 字段仍表示初始上限
    cargo = client.get("/cargo/C1").json()
    assert cargo["temp_upper_limit"] == "4.000"


def test_history_unknown_cargo_404(client):
    assert history(client, "NOPE").status_code == 404


# --------------------------------------------------------------- 提交成功


def test_submit_change_advances_version_and_history(client):
    setup_cargo(client, limit="5")
    r = change_limit(
        client, "tc-1", "2026-01-01T12:00:00+00:00", "3", expected_version=0
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["version"] == 1
    ch = body["change"]
    assert ch["change_id"] == "tc-1"
    assert ch["version"] == 1
    assert ch["expected_version"] == 0
    assert ch["new_limit"] == "3.000"
    assert ch["effective_at"] == "2026-01-01T12:00:00Z"

    hist = body["history"]
    assert hist["version"] == 1
    assert [s["version"] for s in hist["standards"]] == [0, 1]
    assert hist["standards"][1]["source"] == "change"
    assert hist["standards"][1]["change_id"] == "tc-1"
    assert hist["standards"][1]["temp_upper_limit"] == "3.000"

    # GET 与回执历程一致
    got = history(client).json()
    assert got == hist


def test_scheduled_future_change_not_yet_current_limit(client):
    setup_cargo(client, limit="5", start="2026-01-01T10:00:00+00:00")
    r = change_limit(
        client, "tc-future", "2027-06-01T00:00:00+00:00", "2", 0
    )
    assert r.status_code == 201, r.text
    body = history(client).json()
    assert body["version"] == 1
    # 预约未到生效时刻：current_limit 仍是初始上限
    assert body["current_limit"] == "5.000"
    assert len(body["standards"]) == 2


# --------------------------------------------------------------- 边界采用新上限


def test_boundary_reading_uses_new_limit(client):
    setup_cargo(client, limit="5")
    # 11:00 起上限收紧为 4
    assert change_limit(
        client, "tc-1", "2026-01-01T11:00:00+00:00", "4", 0
    ).status_code == 201
    # 10:30 测温 4.5：旧上限 5 下不越温
    assert m(client, "m0", 30, "4.5").status_code == 201
    # 11:00 测温 4.5（恰在生效时刻）：新上限 4 下越温 -> 开启区间
    assert m(client, "m1", "00", "4.5", hour=11).status_code == 201
    # 11:30 测温 4（等于上限不算越温）-> 闭合
    assert m(client, "m2", "30", "4", hour=11).status_code == 201

    body = report(client).json()
    assert body["standard_version"] == 1
    assert len(body["excursions"]) == 1
    ex = body["excursions"][0]
    assert ex["start"] == "2026-01-01T11:00:00Z"
    assert ex["end"] == "2026-01-01T11:30:00Z"
    assert ex["interval_id"] == "m1"


def test_reading_just_before_change_uses_old_limit(client):
    setup_cargo(client, limit="5")
    assert change_limit(
        client, "tc-1", "2026-01-01T11:00:00+00:00", "4", 0
    ).status_code == 201
    # 10:59 测温 4.5 在旧上限 5 下不越温
    client.post(
        "/events",
        json={
            "event_id": "m0",
            "cargo_id": "C1",
            "occurred_at": "2026-01-01T10:59:00+00:00",
            "temperature": "4.5",
        },
    )
    assert report(client).json()["excursions"] == []


def test_loosening_limit_closes_open_interval_at_change_boundary(client):
    # 10:30 测温 6 在初始上限 5 下越温（开放区间）；11:00 上限放宽到 7 后，
    # 区间状态由“每笔读数当时上限”重建——11:30 一笔 6 不越温闭合，
    # 而恰在变更时刻 11:00 若无测温，开放区间仍开放，直到不超限读数闭合。
    setup_cargo(client, limit="5")
    assert m(client, "m1", 30, "6").status_code == 201
    rep = report(client).json()
    assert len(rep["excursions"]) == 1 and rep["excursions"][0]["is_open"]

    # 放宽上限到 6（等于上限不算越温）：同刻 11:00 测温 6 采用新上限 -> 闭合
    assert change_limit(
        client, "tc-1", "2026-01-01T11:00:00+00:00", "6", 0
    ).status_code == 201
    client.post(
        "/events",
        json={
            "event_id": "m2",
            "cargo_id": "C1",
            "occurred_at": "2026-01-01T11:00:00+00:00",
            "temperature": "6",
        },
    )
    ex = report(client).json()["excursions"][0]
    assert ex["is_open"] is False
    assert ex["end"] == "2026-01-01T11:00:00Z"


# --------------------------------------------------------------- 补录


def test_backfill_change_rebuilds_history(client):
    setup_cargo(client, limit="5")
    assert m(client, "m1", 10, "4.5").status_code == 201
    assert m(client, "m2", 20, "4.5").status_code == 201
    # 补录：10:05 起上限收紧为 4（早于当前所有测温之后补登记）
    r = change_limit(
        client, "tc-back", "2026-01-01T10:05:00+00:00", "4", 0,
        reason="事后确认起始即执行更严标准",
    )
    assert r.status_code == 201, r.text
    # 两笔 4.5 测温都落在新上限 4 下 -> 越温区间 [10:10, 开放（无闭合）)
    excursions = report(client).json()["excursions"]
    assert len(excursions) == 1
    assert excursions[0]["start"] == "2026-01-01T10:10:00Z"
    assert excursions[0]["is_open"] is True
    # 历程按生效时刻排序（补录条目在初始之后）
    stds = history(client).json()["standards"]
    assert [s["version"] for s in stds] == [0, 1]
    assert stds[1]["effective_at"] == "2026-01-01T10:05:00Z"


# --------------------------------------------------------------- 拒绝场景


def test_change_before_cargo_start_rejected(client):
    setup_cargo(client, start="2026-01-01T10:00:00+00:00", limit="5")
    r = change_limit(
        client, "tc-x", "2026-01-01T09:59:00+00:00", "4", 0
    )
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "before_start"
    assert history(client).json()["version"] == 0  # 不留写入


def test_change_equal_cargo_start_allowed(client):
    # 货物起始时刻与初始上限同时生效：允许登记（同刻冲突的是另一条变更）
    setup_cargo(client)
    r = change_limit(
        client, "tc-0", "2026-01-01T10:00:00+00:00", "4", 0
    )
    assert r.status_code == 201, r.text
    # 两条标准自同一时刻生效时以版本更高（后登记）者为准
    assert history(client).json()["current_limit"] == "4.000"


def test_change_same_timestamp_conflict(client):
    setup_cargo(client)
    assert change_limit(
        client, "tc-1", "2026-01-01T12:00:00+00:00", "4", 0
    ).status_code == 201
    r = change_limit(
        client, "tc-2", "2026-01-01T12:00:00+00:00", "3", 1
    )
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "same_timestamp_conflict"
    assert history(client).json()["version"] == 1


def test_stale_expected_version_rejected_and_id_reusable(client):
    setup_cargo(client)
    assert change_limit(
        client, "tc-1", "2026-01-01T12:00:00+00:00", "4", 0
    ).status_code == 201
    # 仍带 expected_version=0 -> 过期
    r = change_limit(
        client, "tc-2", "2026-01-01T13:00:00+00:00", "3", 0
    )
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "stale_standard_version"
    # 失败不留部分写入：编号 tc-2 可复用、版本不递增
    assert history(client).json()["version"] == 1
    r2 = change_limit(
        client, "tc-2", "2026-01-01T13:00:00+00:00", "3", 1
    )
    assert r2.status_code == 201, r2.text
    assert history(client).json()["version"] == 2


def test_change_id_different_content_conflict(client):
    setup_cargo(client)
    assert change_limit(
        client, "tc-1", "2026-01-01T12:00:00+00:00", "4", 0
    ).status_code == 201
    r = change_limit(
        client, "tc-1", "2026-01-01T13:00:00+00:00", "4", 0
    )
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "limit_change_id_conflict"


def test_change_requires_timezone(client):
    setup_cargo(client)
    r = client.post(
        "/cargo/C1/temp-limits",
        json={
            "change_id": "tc-nt",
            "effective_at": "2026-01-01T12:00:00",  # 无时区
            "new_limit": "4",
            "expected_version": 0,
            "reason": "x",
        },
    )
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "timezone_required"


def test_change_blank_reason_rejected(client):
    setup_cargo(client)
    r = client.post(
        "/cargo/C1/temp-limits",
        json={
            "change_id": "tc-nr",
            "effective_at": "2026-01-01T12:00:00+00:00",
            "new_limit": "4",
            "expected_version": 0,
            "reason": "   ",
        },
    )
    assert r.status_code == 422


def test_change_unknown_cargo_404(client):
    r = change_limit(
        client, "tc-x", "2026-01-01T12:00:00+00:00", "4", 0, cargo="NOPE"
    )
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "cargo_not_found"


# --------------------------------------------------------------- 幂等重传


def test_same_request_replay_returns_original(client):
    setup_cargo(client)
    payload = {
        "change_id": "tc-1",
        "effective_at": "2026-01-01T12:00:00+00:00",
        "new_limit": "4",
        "expected_version": 0,
        "reason": "首次变更",
    }
    r1 = client.post("/cargo/C1/temp-limits", json=payload)
    assert r1.status_code == 201
    # 同请求重传（即使标准版本已推进）：返回原结果
    r2 = client.post("/cargo/C1/temp-limits", json=payload)
    assert r2.status_code == 200
    body = r2.json()
    assert body["replayed"] is True
    assert body["change"]["expected_version"] == 0
    assert body["change"]["version"] == 1
    assert body["version"] == 1  # 回执附当前历程版本（仍为 1）
    assert history(client).json()["version"] == 1  # 不重复写入


# --------------------------------------------------------------- 时间线展示


def test_timeline_points_carry_applied_limit(client):
    setup_cargo(client, limit="5")
    assert m(client, "m1", 10, "6").status_code == 201
    assert change_limit(
        client, "tc-1", "2026-01-01T10:20:00+00:00", "4", 0
    ).status_code == 201
    assert m(client, "m2", 30, "6").status_code == 201
    points = client.get("/cargo/C1/timeline").json()["points"]
    by_id = {p["event_id"]: p for p in points}
    assert by_id["m1"]["temp_upper_limit"] == "5.000"
    assert by_id["m2"]["temp_upper_limit"] == "4.000"


# --------------------------------------------------------------- 车载读数


def _setup_trip_with_cargo(client, cargo="CA", cargo_limit="5"):
    setup_cargo(client, cargo=cargo, limit=cargo_limit)
    r = client.post("/trips", json={"trip_id": "T1", "vehicle_no": "京B-1"})
    assert r.status_code == 201, r.text
    r = client.post(
        "/trips/T1/manifest",
        json={
            "items": [
                {
                    "cargo_id": cargo,
                    "loaded_at": "2026-01-01T10:00:00+00:00",
                    "unloaded_at": "2026-01-01T18:00:00+00:00",
                }
            ]
        },
    )
    assert r.status_code == 201, r.text


def test_vehicle_reading_judged_by_limit_at_read_time(client):
    _setup_trip_with_cargo(client, cargo_limit="5")
    # 11:00 起上限收紧到 4
    assert change_limit(
        client, "tc-1", "2026-01-01T11:00:00+00:00", "4", 0, cargo="CA"
    ).status_code == 201
    # 10:30 车载读数 4.5（旧上限 5，不越温）
    assert client.post(
        "/vehicle-readings",
        json={
            "reading_id": "vr-0",
            "trip_id": "T1",
            "read_at": "2026-01-01T10:30:00+00:00",
            "temperature": "4.5",
        },
    ).status_code == 201
    # 11:00 车载读数 4.5（恰在生效时刻，新上限 4，越温）
    assert client.post(
        "/vehicle-readings",
        json={
            "reading_id": "vr-1",
            "trip_id": "T1",
            "read_at": "2026-01-01T11:00:00+00:00",
            "temperature": "4.5",
        },
    ).status_code == 201

    detail = client.get("/trips/T1").json()
    apps = {
        r["reading_id"]: r["applications"][0] for r in detail["readings"]
    }
    assert apps["vr-0"]["over_limit"] is False
    assert apps["vr-0"]["temp_upper_limit"] == "5.000"
    assert apps["vr-1"]["over_limit"] is True
    assert apps["vr-1"]["temp_upper_limit"] == "4.000"

    # 货物时间线车载点也带当时上限
    points = client.get("/cargo/CA/timeline").json()["points"]
    vr1 = next(p for p in points if p["reading_id"] == "vr-1")
    assert vr1["temp_upper_limit"] == "4.000"
    assert vr1["adopted"] is True


# --------------------------------------------------------------- 责任确认


def test_responsibility_snapshot_changes_when_standard_changes(client):
    setup_cargo(client, limit="5")
    assert m(client, "m1", 10, "8").status_code == 201
    assert h(client, "h1", 30, "A", "B").status_code == 201
    assert m(client, "m2", 50, "4").status_code == 201
    snap_before = (
        client.get("/cargo/C1/responsibilities").json()["intervals"][0]["snapshot_id"]
    )
    # 表态基于旧快照可成功
    r = client.post(
        "/cargo/C1/responsibilities/m1/stances",
        json={
            "snapshot_id": snap_before,
            "responsible_party": "A",
            "stance": "confirm",
            "stance_id": "st-a-1",
        },
    )
    assert r.status_code == 201, r.text

    # 补录标准变更（10:05 起上限 9）：区间消失 -> 快照体系变化
    assert change_limit(
        client, "tc-1", "2026-01-01T10:05:00+00:00", "9", 0
    ).status_code == 201
    assert client.get("/cargo/C1/responsibilities").json()["intervals"] == []
    # 旧快照表态被拒绝
    r = client.post(
        "/cargo/C1/responsibilities/m1/stances",
        json={
            "snapshot_id": snap_before,
            "responsible_party": "B",
            "stance": "confirm",
            "stance_id": "st-b-1",
        },
    )
    assert r.status_code == 404
    # 历史表态保留但已失效
    hist = client.get(
        "/cargo/C1/responsibilities/m1/stances"
    ).json()
    assert hist["interval_exists"] is False
    assert all(s["is_current"] is False for s in hist["history"])
    assert len(hist["history"]) == 1


# --------------------------------------------------------------- 同时越温


def test_simultaneous_excursions_uses_per_cargo_limit_at_time(client):
    # 两票货物同车：CA 初始上限 5，CB 初始上限 5；CA 在 11:00 收紧到 4。
    for cid in ("CA", "CB"):
        setup_cargo(client, cargo=cid, limit="5", holder="H")
    client.post("/trips", json={"trip_id": "T1", "vehicle_no": "京B-1"})
    client.post(
        "/trips/T1/manifest",
        json={
            "items": [
                {
                    "cargo_id": cid,
                    "loaded_at": "2026-01-01T10:00:00+00:00",
                    "unloaded_at": "2026-01-01T18:00:00+00:00",
                }
                for cid in ("CA", "CB")
            ]
        },
    )
    assert change_limit(
        client, "tc-a", "2026-01-01T11:00:00+00:00", "4", 0, cargo="CA"
    ).status_code == 201
    # 10:30 读数 4.5：二者上限 5 都不越温 -> 无同时段
    client.post(
        "/vehicle-readings",
        json={
            "reading_id": "vr-a", "trip_id": "T1",
            "read_at": "2026-01-01T10:30:00+00:00", "temperature": "4.5",
        },
    )
    # 11:00 读数 4.5：CA 上限 4 越温，CB 上限 5 不越温 -> 仅一票，不达门槛
    client.post(
        "/vehicle-readings",
        json={
            "reading_id": "vr-b", "trip_id": "T1",
            "read_at": "2026-01-01T11:00:00+00:00", "temperature": "4.5",
        },
    )
    # 11:30 读数 6：二者都越温 -> 同时段
    client.post(
        "/vehicle-readings",
        json={
            "reading_id": "vr-c", "trip_id": "T1",
            "read_at": "2026-01-01T11:30:00+00:00", "temperature": "6",
        },
    )
    # 12:30 读数 3：都不越温（闭合）
    client.post(
        "/vehicle-readings",
        json={
            "reading_id": "vr-d", "trip_id": "T1",
            "read_at": "2026-01-01T12:30:00+00:00", "temperature": "3",
        },
    )
    r = client.get(
        "/trips/T1/simultaneous-excursions",
        params={
            "start_at": "2026-01-01T10:00:00+00:00",
            "end_at": "2026-01-02T00:00:00+00:00",
        },
    )
    assert r.status_code == 200, r.text
    segs = r.json()["segments"]
    assert len(segs) == 1
    assert segs[0]["start"] == "2026-01-01T11:30:00Z"
    assert segs[0]["end"] == "2026-01-01T12:30:00Z"
    assert segs[0]["cargo_ids"] == ["CA", "CB"]


# --------------------------------------------------------------- 装前预检


def _precheck_split_scenario(client):
    # 车次上先有一票货物 CA（上限恒 5），拟登记 CB：CB 已登记标准 11:00 起 3。
    setup_cargo(client, cargo="CA", limit="5", holder="H")
    setup_cargo(client, cargo="CB", limit="8", holder="H")
    client.post("/trips", json={"trip_id": "T1", "vehicle_no": "京B-1"})
    assert client.post(
        "/trips/T1/manifest",
        json={
            "items": [
                {
                    "cargo_id": "CA",
                    "loaded_at": "2026-01-01T10:00:00+00:00",
                    "unloaded_at": "2026-01-01T18:00:00+00:00",
                }
            ]
        },
    ).status_code == 201
    assert change_limit(
        client, "tc-cb", "2026-01-01T11:00:00+00:00", "3", 0, cargo="CB"
    ).status_code == 201
    # 11:00 一笔读数 4.5（落在 CB 收紧后）：对 CB 风险；10:30 一笔 4.5 无风险
    for rid, at in (
        ("vr-early", "2026-01-01T10:30:00+00:00"),
        ("vr-late", "2026-01-01T11:30:00+00:00"),
    ):
        client.post(
            "/vehicle-readings",
            json={"reading_id": rid, "trip_id": "T1", "read_at": at,
                  "temperature": "4.5"},
        )
    # 车次修订号在既有装卸 + 两笔读数后为 3（标准变更不推进车次修订号）
    rev = client.get("/trips/T1").json()["revision"]
    assert rev == 3

    r = client.post(
        "/trips/T1/manifest/precheck",
        json={
            "expected_trip_revision": rev,
            "items": [
                {
                    "cargo_id": "CB",
                    "loaded_at": "2026-01-01T10:00:00+00:00",
                    "unloaded_at": "2026-01-01T18:00:00+00:00",
                }
            ],
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    # [10:00,11:00) 与 [11:00,18:00) 两段（CB 标准变更时刻切分）
    bounds = [(s["start"], s["end"]) for s in body["segments"]]
    assert ("2026-01-01T10:00:00Z", "2026-01-01T11:00:00Z") in bounds
    assert ("2026-01-01T11:00:00Z", "2026-01-01T18:00:00Z") in bounds
    late_seg = next(
        s for s in body["segments"]
        if s["start"] == "2026-01-01T11:00:00Z"
    )
    assert late_seg["temp_limit_min"] == "3.000"
    assert late_seg["temp_limit_max"] == "5.000"
    risk_cargos = {rk["cargo_id"] for rk in late_seg["risks"]}
    assert risk_cargos == {"CB"}
    assert late_seg["risks"][0]["temp_upper_limit"] == "3.000"
    early_seg = next(
        s for s in body["segments"]
        if s["start"] == "2026-01-01T10:00:00Z"
    )
    assert early_seg["temp_limit_min"] == "5.000"
    assert early_seg["temp_limit_max"] == "8.000"
    assert early_seg["risks"] == []
    return body["digest"], rev


def test_precheck_segments_split_at_standard_change(client):
    _precheck_split_scenario(client)


def test_precheck_digest_stale_after_standard_backfill(client):
    digest, rev = _precheck_split_scenario(client)
    # 预检后为 CA 补录标准变更（10:00 起上限 4）：摘要应过期
    assert change_limit(
        client, "tc-ca", "2026-01-01T10:00:00+00:00", "4", 0, cargo="CA"
    ).status_code == 201
    r = client.post(
        "/trips/T1/manifest/confirm",
        json={
            "digest": digest,
            "items": [
                {
                    "cargo_id": "CB",
                    "loaded_at": "2026-01-01T10:00:00+00:00",
                    "unloaded_at": "2026-01-01T18:00:00+00:00",
                }
            ],
        },
    )
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "stale_precheck_digest"
    # 确认失败不留装卸记录
    detail = client.get("/trips/T1").json()
    assert {l["cargo_id"] for l in detail["loadings"]} == {"CA"}
    # 车次修订号未因标准变更而递增（标准不修订车次）
    assert detail["revision"] == rev


# --------------------------------------------------------------- 封存


def test_seal_records_per_reading_limits_and_diff_shows_standard_change(client):
    setup_cargo(client, limit="5")
    assert m(client, "m1", 10, "8").status_code == 201
    assert h(client, "h1", 30, "A", "B").status_code == 201
    assert m(client, "m2", 50, "4").status_code == 201
    iv = client.get("/cargo/C1/responsibilities").json()["intervals"][0]
    r = client.post(
        "/evidence-seals",
        json={
            "case_id": "CASE-TL",
            "cargo_id": "C1",
            "interval_id": iv["interval_id"],
            "snapshot_id": iv["snapshot_id"],
        },
    )
    assert r.status_code == 201, r.text
    sealed = r.json()
    # 封存包记录标准历程与逐笔读数当时上限
    cargo_block = sealed["content"]["cargo"]
    assert cargo_block["temp_upper_limit"] == "5.000"  # 初始上限字段不变
    assert cargo_block["standard_version"] == 0
    assert len(cargo_block["temp_limit_standards"]) == 1
    own = {e["event_id"]: e for e in sealed["content"]["own_events"]}
    assert own["m1"]["temp_upper_limit"] == "5.000"
    assert own["m2"]["temp_upper_limit"] == "5.000"
    # 摘要对封存原文（含标准历程）取 SHA-256；原文与摘要均落库、GET 恒一致
    from app.models import EvidenceSeal

    db = SessionLocal()
    try:
        row = db.get(EvidenceSeal, "CASE-TL")
        assert row.content_digest == sealed["content_digest"]
        # 内容摘要确实覆盖新增的标准历程：规范化原文中含标准与逐笔上限
        assert "temp_limit_standards" in row.content
        assert '"temp_upper_limit":"5.000"' in row.content
    finally:
        db.close()

    # 封存后补录标准变更（10:05 起上限 9）：区间消失
    assert change_limit(
        client, "tc-1", "2026-01-01T10:05:00+00:00", "9", 0
    ).status_code == 201

    # 原封存包恒不变
    got = client.get("/evidence-seals/CASE-TL").json()
    assert got == sealed

    diff = client.get("/evidence-seals/CASE-TL/diff").json()
    assert diff["status"] == "disappeared"
    changes = {
        c["key"]: c
        for c in diff["evidence_changes"]["own_events"]
        if c["change"] == "changed"
    }
    assert "m1" in changes
    assert "temp_upper_limit" in changes["m1"]["fields"]
    assert changes["m1"]["sealed"]["temp_upper_limit"] == "5.000"
    assert changes["m1"]["current"]["temp_upper_limit"] == "9.000"


def test_seal_after_standard_change_uses_current_limits(client):
    setup_cargo(client, limit="5")
    # 先变更标准，再产生区间：新封存包直接按当时上限记录
    assert change_limit(
        client, "tc-1", "2026-01-01T10:00:00+00:00", "4", 0
    ).status_code == 201
    assert m(client, "m1", 10, "6").status_code == 201
    assert m(client, "m2", 20, "3").status_code == 201
    iv = client.get("/cargo/C1/responsibilities").json()["intervals"][0]
    r = client.post(
        "/evidence-seals",
        json={
            "case_id": "CASE-NEW",
            "cargo_id": "C1",
            "interval_id": iv["interval_id"],
            "snapshot_id": iv["snapshot_id"],
        },
    )
    assert r.status_code == 201, r.text
    content = r.json()["content"]
    assert content["cargo"]["standard_version"] == 1
    own = {e["event_id"]: e for e in content["own_events"]}
    assert own["m1"]["temp_upper_limit"] == "4.000"
    # 无后续变化 -> unchanged，且无证据项变更
    diff = client.get("/evidence-seals/CASE-NEW/diff").json()
    assert diff["status"] == "unchanged"
    assert diff["evidence_changes"]["own_events"] == []


def test_seal_idempotent_after_standard_change_returns_original(client):
    setup_cargo(client, limit="5")
    assert m(client, "m1", 10, "8").status_code == 201
    assert m(client, "m2", 20, "4").status_code == 201
    snap = client.get("/cargo/C1/responsibilities").json()["intervals"][0]["snapshot_id"]
    payload = {
        "case_id": "CASE-RP",
        "cargo_id": "C1",
        "interval_id": "m1",
        "snapshot_id": snap,
    }
    first = client.post("/evidence-seals", json=payload)
    assert first.status_code == 201
    # 补录标准使区间消失
    assert change_limit(
        client, "tc-1", "2026-01-01T10:05:00+00:00", "9", 0
    ).status_code == 201
    # 同案件同请求仍返回原包
    r = client.post("/evidence-seals", json=payload)
    assert r.status_code == 200
    assert r.json()["seal"] == first.json()


# --------------------------------------------------------------- 变更不产生测温 / 交接


def test_limit_change_creates_no_events(client):
    setup_cargo(client)
    assert change_limit(
        client, "tc-1", "2026-01-01T12:00:00+00:00", "4", 0
    ).status_code == 201
    assert client.get("/cargo/C1/events").json() == []
    db = SessionLocal()
    try:
        assert db.query(TemperatureLimitChange).count() == 1
    finally:
        db.close()


def test_successive_changes_walk_versions(client):
    setup_cargo(client, limit="5")
    assert change_limit(
        client, "tc-1", "2026-01-01T11:00:00+00:00", "4", 0
    ).status_code == 201
    assert change_limit(
        client, "tc-2", "2026-01-01T12:00:00+00:00", "3", 1
    ).status_code == 201
    assert change_limit(
        client, "tc-3", "2026-01-01T13:00:00+00:00", "2", 2
    ).status_code == 201
    body = history(client).json()
    assert body["version"] == 3
    assert [s["temp_upper_limit"] for s in body["standards"]] == [
        "5.000", "4.000", "3.000", "2.000",
    ]


def test_seal_diff_unchanged_handover_limit_null_not_flagged(client):
    """区间跨交接封存：无标准变化时交接项 temp_upper_limit=null 不应误报变更。"""
    setup_cargo(client, limit="5")
    assert m(client, "m1", 10, "8").status_code == 201
    assert h(client, "h1", 30, "A", "B").status_code == 201
    assert m(client, "m2", 50, "4").status_code == 201
    iv = client.get("/cargo/C1/responsibilities").json()["intervals"][0]
    r = client.post(
        "/evidence-seals",
        json={
            "case_id": "CASE-HX",
            "cargo_id": "C1",
            "interval_id": iv["interval_id"],
            "snapshot_id": iv["snapshot_id"],
        },
    )
    assert r.status_code == 201, r.text
    diff = client.get("/evidence-seals/CASE-HX/diff").json()
    assert diff["status"] == "unchanged"
    assert diff["evidence_changes"]["own_events"] == []
