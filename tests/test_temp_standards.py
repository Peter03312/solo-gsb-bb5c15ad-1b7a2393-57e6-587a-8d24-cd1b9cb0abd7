"""货物温度上限分时生效标准（temp-standards）端到端测试。"""
from decimal import Decimal

from app.chain import rebuild_chain


def _set_change(client, cargo_id, change_id, effective_at, new_limit,
                expected_version=0, reason="标准调整"):
    return client.post(
        f"/cargo/{cargo_id}/temp-standards",
        json={
            "change_id": change_id,
            "effective_at": effective_at,
            "new_upper_limit": new_limit,
            "expected_version": expected_version,
            "reason": reason,
        },
    )


def test_initial_standard_version_zero(make_cargo, client):
    make_cargo()
    r = client.get("/cargo/C1/temp-standards")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["current_version"] == 0
    assert body["initial_temp_upper_limit"] == "5.000"
    assert body["current_temp_upper_limit"] == "5.000"
    assert len(body["standards"]) == 1
    v0 = body["standards"][0]
    assert v0["version"] == 0
    assert v0["change_id"] is None
    assert v0["effective_at"].startswith("2026-01-01T10:00:00")
    assert v0["temp_upper_limit"] == "5.000"


def test_change_submit_advances_version(make_cargo, client):
    make_cargo()
    r = _set_change(client, "C1", "ch1", "2026-01-01T12:00:00+00:00", "3")
    assert r.status_code == 201, r.text
    ack = r.json()
    assert ack["version"] == 1
    assert ack["current_version"] == 1
    assert ack["change"]["version"] == 1
    assert ack["change"]["expected_version"] == 0
    assert ack["new_upper_limit"] == "3.000"

    hist = client.get("/cargo/C1/temp-standards").json()
    assert hist["current_version"] == 1
    assert [s["version"] for s in hist["standards"]] == [0, 1]
    assert hist["standards"][1]["change_id"] == "ch1"

    # 再提交一笔（期望版本 1）
    r2 = _set_change(client, "C1", "ch2", "2026-01-01T14:00:00+00:00", "4",
                     expected_version=1)
    assert r2.status_code == 201, r2.text
    assert r2.json()["version"] == 2


def test_same_change_id_same_request_replays(make_cargo, client):
    make_cargo()
    r1 = _set_change(client, "C1", "ch1", "2026-01-01T12:00:00+00:00", "3")
    assert r1.status_code == 201
    r2 = _set_change(client, "C1", "ch1", "2026-01-01T12:00:00+00:00", "3")
    assert r2.status_code == 200, r2.text
    assert r2.json()["replayed"] is True
    assert r2.json()["version"] == 1
    # 没有多写一条
    hist = client.get("/cargo/C1/temp-standards").json()
    assert [s["version"] for s in hist["standards"]] == [0, 1]


def test_same_change_id_different_content_conflicts(make_cargo, client):
    make_cargo()
    _set_change(client, "C1", "ch1", "2026-01-01T12:00:00+00:00", "3")
    # 异内容（不同上限）-> 409
    r = _set_change(client, "C1", "ch1", "2026-01-01T12:00:00+00:00", "2")
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "change_id_conflict"


def test_stale_version_rejected(make_cargo, client):
    make_cargo()
    _set_change(client, "C1", "ch1", "2026-01-01T12:00:00+00:00", "3")
    # 仍带期望版本 0 -> 过期
    r = _set_change(client, "C1", "ch2", "2026-01-01T13:00:00+00:00", "4",
                    expected_version=0)
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "stale_version"


def test_effective_before_cargo_start_rejected(make_cargo, client):
    make_cargo()
    r = _set_change(client, "C1", "ch1", "2026-01-01T09:59:00+00:00", "3")
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "before_start"


def test_effective_equal_cargo_start_rejected(make_cargo, client):
    make_cargo()
    r = _set_change(client, "C1", "ch1", "2026-01-01T10:00:00+00:00", "3")
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "standard_initial_slot"


def test_same_effective_at_conflicts(make_cargo, client):
    make_cargo()
    _set_change(client, "C1", "ch1", "2026-01-01T12:00:00+00:00", "3")
    r = _set_change(client, "C1", "ch2", "2026-01-01T12:00:00+00:00", "4",
                    expected_version=1)
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "same_timestamp_conflict"
    # 不留部分写入：版本仍为 1
    assert client.get("/cargo/C1/temp-standards").json()["current_version"] == 1


def test_timezone_required(make_cargo, client):
    make_cargo()
    r = client.post(
        "/cargo/C1/temp-standards",
        json={
            "change_id": "ch1",
            "effective_at": "2026-01-01T12:00:00",
            "new_upper_limit": "3",
            "expected_version": 0,
            "reason": "x",
        },
    )
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "timezone_required"


def test_blank_reason_rejected(make_cargo, client):
    r = _set_change(client, "C1", "ch1", "2026-01-01T12:00:00+00:00", "3",
                    reason="   ")
    assert r.status_code == 422


def test_unknown_cargo_404(client):
    r = _set_change(client, "NOPE", "ch1", "2026-01-01T12:00:00+00:00", "3")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "cargo_not_found"


# ----------------------------------------------------------- 重建：当时上限


def test_report_rebuilds_with_step_limits(make_cargo, client):
    # 初始上限 5，自 12:00 起降为 3
    make_cargo()
    _set_change(client, "C1", "ch1", "2026-01-01T12:00:00+00:00", "3")

    def meas(eid, at, temp):
        return client.post(
            "/events",
            json={"event_id": eid, "cargo_id": "C1",
                  "occurred_at": at, "temperature": temp},
        )

    # 11:00 温度 4：旧上限 5 下不越温（若误用新上限 3 则会越温）
    assert meas("m0", "2026-01-01T11:00:00+00:00", "4").status_code == 201
    # 13:00 温度 4：新上限 3 下越温开启
    assert meas("m1", "2026-01-01T13:00:00+00:00", "4").status_code == 201
    # 14:00 温度 2：闭合
    assert meas("m2", "2026-01-01T14:00:00+00:00", "2").status_code == 201

    report = client.get("/cargo/C1/report").json()
    assert len(report["excursions"]) == 1
    ex = report["excursions"][0]
    assert ex["start"].startswith("2026-01-01T13:00:00")
    assert ex["interval_id"] == "m1"


def test_boundary_reading_uses_new_limit(make_cargo, client):
    # 初始上限 5；12:00 起新上限 5（相等值不影响），另一组用 4
    make_cargo(limit="5")
    # 12:00 起上限变为 4；恰在 12:00 的读数温度 4.5 -> 新上限下越温，
    # 旧上限 5 下不越温
    _set_change(client, "C1", "ch1", "2026-01-01T12:00:00+00:00", "4")
    r = client.post(
        "/events",
        json={"event_id": "m1", "cargo_id": "C1",
              "occurred_at": "2026-01-01T12:00:00+00:00",
              "temperature": "4.5"},
    )
    assert r.status_code == 201
    report = client.get("/cargo/C1/report").json()
    assert len(report["excursions"]) == 1
    assert report["excursions"][0]["start"].startswith("2026-01-01T12:00:00")


def test_backfill_standard_reopens_history(make_cargo, client):
    """补录一笔过去生效的更严格上限：历史越温按新（更严）上限重建。"""
    make_cargo(limit="5")
    # 先按旧上限录数据：11:00 温度 4 不越温
    client.post(
        "/events",
        json={"event_id": "m1", "cargo_id": "C1",
              "occurred_at": "2026-01-01T11:00:00+00:00",
              "temperature": "4"},
    )
    assert client.get("/cargo/C1/report").json()["excursions"] == []
    # 补录：上限自 10:30 起为 3 -> 11:00 的 4 变成越温开启（未闭合）
    r = _set_change(client, "C1", "ch1", "2026-01-01T10:30:00+00:00", "3")
    assert r.status_code == 201, r.text
    report = client.get("/cargo/C1/report").json()
    assert len(report["excursions"]) == 1
    assert report["excursions"][0]["is_open"] is True
    assert report["excursions"][0]["start"].startswith("2026-01-01T11:00:00")


def test_scheduled_standard_not_effective_yet(make_cargo, client):
    """预约：未来生效的版本在到达前不影响当前上限与越温判定。"""
    make_cargo(
        cargo_id="CS",
        started_at="2026-01-01T10:00:00+00:00",
        limit="5",
    )
    # 生效时刻在很远的未来
    r = _set_change(client, "CS", "ch1", "2099-06-01T00:00:00+00:00", "1")
    assert r.status_code == 201
    body = client.get("/cargo/CS/temp-standards").json()
    # 版本已推进到 1，但当前上限仍是初始 5
    assert body["current_version"] == 1
    assert body["current_temp_upper_limit"] == "5.000"

    client.post(
        "/events",
        json={"event_id": "m1", "cargo_id": "CS",
              "occurred_at": "2026-01-02T08:00:00+00:00",
              "temperature": "4"},
    )
    report = client.get("/cargo/CS/report").json()
    assert report["excursions"] == []


def test_timeline_points_carry_limit(make_cargo, client):
    make_cargo(limit="5")
    _set_change(client, "C1", "ch1", "2026-01-01T12:00:00+00:00", "3")
    client.post(
        "/events",
        json={"event_id": "m0", "cargo_id": "C1",
              "occurred_at": "2026-01-01T11:00:00+00:00", "temperature": "2"},
    )
    client.post(
        "/events",
        json={"event_id": "m1", "cargo_id": "C1",
              "occurred_at": "2026-01-01T13:00:00+00:00", "temperature": "2"},
    )
    points = client.get("/cargo/C1/timeline").json()["points"]
    limits = {p["event_id"]: p["temp_upper_limit"] for p in points}
    assert limits["m0"] == "5.000"
    assert limits["m1"] == "3.000"


# ----------------------------------------------------------- 纯函数


def test_chain_rebuild_with_per_point_limits():
    """直接用时间线点验证：同一温度在不同时刻按各自上限判定。"""
    from app.chain import TimelinePoint

    t0 = "2026-01-01T"
    pts = [
        TimelinePoint(
            occurred_at=_dt("2026-01-01T11:00:00+00:00"),
            is_measurement=True, temperature=Decimal("4"),
            source="own", source_id="m0",
            temp_limit=Decimal("5"),
        ),
        TimelinePoint(
            occurred_at=_dt("2026-01-01T13:00:00+00:00"),
            is_measurement=True, temperature=Decimal("4"),
            source="own", source_id="m1",
            temp_limit=Decimal("3"),
        ),
        TimelinePoint(
            occurred_at=_dt("2026-01-01T14:00:00+00:00"),
            is_measurement=True, temperature=Decimal("2"),
            source="own", source_id="m2",
            temp_limit=Decimal("3"),
        ),
    ]
    r = rebuild_chain("A", Decimal("5"), pts)
    assert len(r.excursions) == 1
    assert r.excursions[0].start == _dt("2026-01-01T13:00:00+00:00")


def _dt(s):
    from datetime import datetime

    return datetime.fromisoformat(s)


# ----------------------------------------------------------- 预检分段纯函数


def test_split_segments_at_cut_points():
    from app.precheck import BoardSegment, split_segments_at

    seg = BoardSegment(
        start=_dt("2026-01-01T10:00:00+00:00"),
        end=_dt("2026-01-01T18:00:00+00:00"),
        cargo_ids=("A",), proposed_ids=(),
    )
    out = split_segments_at(
        [seg],
        [_dt("2026-01-01T12:00:00+00:00"),
         _dt("2026-01-01T15:00:00+00:00")],
    )
    assert [(s.start.hour, s.end.hour) for s in out] == [(10, 12), (12, 15), (15, 18)]
    # 边界外 / 恰在端点的切点不产生零长段
    out2 = split_segments_at(
        [seg],
        [_dt("2026-01-01T10:00:00+00:00"),   # 恰起点
         _dt("2026-01-01T18:00:00+00:00"),   # 恰终点
         _dt("2026-01-01T09:00:00+00:00")],  # 段外
    )
    assert [(s.start.hour, s.end.hour) for s in out2] == [(10, 18)]
    assert all(s.cargo_ids == ("A",) for s in out2)
