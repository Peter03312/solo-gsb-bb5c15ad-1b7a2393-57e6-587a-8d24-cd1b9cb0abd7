"""分时温度标准与车载读数、车次同时越温、预检、封存的集成测试。"""


def _std(client, cargo_id, change_id, at, limit, expected=0, reason="标准调整"):
    return client.post(
        f"/cargo/{cargo_id}/temp-standards",
        json={
            "change_id": change_id,
            "effective_at": at,
            "new_upper_limit": limit,
            "expected_version": expected,
            "reason": reason,
        },
    )


def _setup_trip(client, cargo_id="C1", limit="5", loaded="2026-01-01T10:00:00+00:00",
                unloaded="2026-01-01T18:00:00+00:00", trip_id="T1"):
    client.post(
        "/cargo",
        json={"cargo_id": cargo_id, "started_at": "2026-01-01T08:00:00+00:00",
              "temp_upper_limit": limit, "initial_holder": "A"},
    )
    client.post("/trips", json={"trip_id": trip_id, "vehicle_no": "京A1"})
    r = client.post(
        f"/trips/{trip_id}/manifest",
        json={"items": [{"cargo_id": cargo_id, "loaded_at": loaded,
                         "unloaded_at": unloaded}]},
    )
    assert r.status_code == 201, r.text
    return trip_id


# ----------------------------------------------------------- 车载读数


def test_vehicle_reading_uses_step_limit(client):
    _setup_trip(client, cargo_id="C1", limit="5")
    # 12:00 起上限降为 3
    assert _std(client, "C1", "ch1", "2026-01-01T12:00:00+00:00", "3").status_code == 201

    def reading(rid, at, temp):
        return client.post(
            "/vehicle-readings",
            json={"reading_id": rid, "trip_id": "T1", "read_at": at,
                  "temperature": temp},
        )

    # 11:00 读数 4：旧上限 5 不越温
    reading("r0", "2026-01-01T11:00:00+00:00", "4")
    # 恰在 12:00 读数 4：新上限 3 下越温（边界取新）
    reading("r1", "2026-01-01T12:00:00+00:00", "4")
    # 13:00 读数 2：闭合
    reading("r2", "2026-01-01T13:00:00+00:00", "2")

    report = client.get("/cargo/C1/report").json()
    assert len(report["excursions"]) == 1
    ex = report["excursions"][0]
    assert ex["start"].startswith("2026-01-01T12:00:00")
    assert ex["start_source"] == "vehicle"
    assert ex["start_reading_id"] == "r1"

    # 车次详情的适用性按当时上限标注
    detail = client.get("/trips/T1").json()
    apps = {
        ro["reading_id"]: ro["applications"][0]
        for ro in detail["readings"]
    }
    assert apps["r0"]["over_limit"] is False
    assert apps["r0"]["temp_upper_limit"] == "5.000"
    assert apps["r1"]["over_limit"] is True
    assert apps["r1"]["temp_upper_limit"] == "3.000"
    assert apps["r2"]["temp_upper_limit"] == "3.000"


def test_timeline_vehicle_point_limit(client):
    _setup_trip(client, cargo_id="C2", limit="5", trip_id="T2")
    _std(client, "C2", "ch1", "2026-01-01T12:00:00+00:00", "3")
    client.post(
        "/vehicle-readings",
        json={"reading_id": "r1", "trip_id": "T2",
              "read_at": "2026-01-01T13:00:00+00:00", "temperature": "4"},
    )
    points = client.get("/cargo/C2/timeline").json()["points"]
    (p,) = points
    assert p["reading_id"] == "r1"
    assert p["temp_upper_limit"] == "3.000"


# ----------------------------------------------------------- 同时越温


def test_simultaneous_excursions_step_limits(client):
    # 两票货物初始上限不同、标准变更时刻不同；验证同一车次同时越温按各自
    # 当时上限重建
    client.post("/trips", json={"trip_id": "T9", "vehicle_no": "京A9"})
    for cid, limit in [("A", "5"), ("B", "5")]:
        client.post(
            "/cargo",
            json={"cargo_id": cid, "started_at": "2026-01-01T08:00:00+00:00",
                  "temp_upper_limit": limit, "initial_holder": "H"},
        )
    client.post(
        "/trips/T9/manifest",
        json={"items": [
            {"cargo_id": "A", "loaded_at": "2026-01-01T10:00:00+00:00",
             "unloaded_at": "2026-01-01T18:00:00+00:00"},
            {"cargo_id": "B", "loaded_at": "2026-01-01T10:00:00+00:00",
             "unloaded_at": "2026-01-01T18:00:00+00:00"},
        ]},
    )
    # 货物 A：12:00 起上限 3；货物 B：13:00 起上限 3
    assert _std(client, "A", "a1", "2026-01-01T12:00:00+00:00", "3").status_code == 201
    assert _std(client, "B", "b1", "2026-01-01T13:00:00+00:00", "3").status_code == 201

    def reading(rid, at, temp):
        client.post(
            "/vehicle-readings",
            json={"reading_id": rid, "trip_id": "T9", "read_at": at,
                  "temperature": temp},
        )

    reading("v1", "2026-01-01T12:00:00+00:00", "4")  # 仅 A 越温
    reading("v2", "2026-01-01T13:00:00+00:00", "4")  # A、B 同时越温
    reading("v3", "2026-01-01T14:00:00+00:00", "2")  # 都闭合

    r = client.get(
        "/trips/T9/simultaneous-excursions"
        "?start_at=2026-01-01T10:00:00%2B00:00"
        "&end_at=2026-01-01T18:00:00%2B00:00&min_cargo=2"
    )
    assert r.status_code == 200, r.text
    segs = r.json()["segments"]
    assert len(segs) == 1
    assert segs[0]["start"].startswith("2026-01-01T13:00:00")
    assert segs[0]["end"].startswith("2026-01-01T14:00:00")
    assert segs[0]["cargo_ids"] == ["A", "B"]


# ----------------------------------------------------------- 预检


def test_precheck_segments_split_at_standard_change(client):
    # 车次上先装一票上限 5 的货物，其标准在 12:00 降为 3；预检另一票货物时
    # 输出分段应在 12:00 处切开（货物集合不变也分段），且风险按读数时刻上限。
    client.post("/trips", json={"trip_id": "TP", "vehicle_no": "京AP"})
    client.post(
        "/cargo",
        json={"cargo_id": "EX", "started_at": "2026-01-01T08:00:00+00:00",
              "temp_upper_limit": "5", "initial_holder": "A"},
    )
    client.post(
        "/trips/TP/manifest",
        json={"items": [{"cargo_id": "EX",
                         "loaded_at": "2026-01-01T10:00:00+00:00",
                         "unloaded_at": "2026-01-01T18:00:00+00:00"}]},
    )
    assert _std(client, "EX", "ch1", "2026-01-01T12:00:00+00:00", "3").status_code == 201
    # 11:30 读数 4（旧上限 5 安全）；12:30 读数 4（新上限 3 越温风险）
    for rid, at in [("rv1", "2026-01-01T11:30:00+00:00"),
                    ("rv2", "2026-01-01T12:30:00+00:00")]:
        client.post(
            "/vehicle-readings",
            json={"reading_id": rid, "trip_id": "TP", "read_at": at,
                  "temperature": "4"},
        )

    client.post(
        "/cargo",
        json={"cargo_id": "NEW", "started_at": "2026-01-01T08:00:00+00:00",
              "temp_upper_limit": "5", "initial_holder": "A"},
    )
    r = client.post(
        "/trips/TP/manifest/precheck",
        json={
            "expected_trip_revision": client.get("/trips/TP").json()["revision"],
            "items": [{"cargo_id": "NEW",
                       "loaded_at": "2026-01-01T10:00:00+00:00",
                       "unloaded_at": "2026-01-01T18:00:00+00:00"}],
        },
    )
    assert r.status_code == 200, r.text
    segs = r.json()["segments"]
    # 必存在以 12:00 为界的两段
    bounds = [(s["start"][11:16], s["end"][11:16]) for s in segs]
    assert ("10:00", "12:00") in bounds
    assert ("12:00", "18:00") in bounds
    # 风险只出现在 12:00 之后的段，且采用当时上限 3
    risks = [risk for s in segs for risk in s["risks"] if risk["cargo_id"] == "EX"]
    assert {rk["reading_id"] for rk in risks} == {"rv2"}
    assert risks[0]["temp_upper_limit"] == "3.000"


def test_precheck_digest_expires_after_standard_change(client):
    client.post("/trips", json={"trip_id": "TD", "vehicle_no": "京AD"})
    client.post(
        "/cargo",
        json={"cargo_id": "D1", "started_at": "2026-01-01T08:00:00+00:00",
              "temp_upper_limit": "5", "initial_holder": "A"},
    )
    revision = client.get("/trips/TD").json()["revision"]
    r = client.post(
        "/trips/TD/manifest/precheck",
        json={
            "expected_trip_revision": revision,
            "items": [{"cargo_id": "D1",
                       "loaded_at": "2026-01-01T10:00:00+00:00",
                       "unloaded_at": "2026-01-01T18:00:00+00:00"}],
        },
    )
    assert r.status_code == 200, r.text
    digest = r.json()["digest"]
    # 预检后补录标准变更（车次修订号不变），摘要应过期
    assert _std(client, "D1", "ch1", "2026-01-01T12:00:00+00:00", "3").status_code == 201
    c = client.post(
        "/trips/TD/manifest/confirm",
        json={
            "digest": digest,
            "items": [{"cargo_id": "D1",
                       "loaded_at": "2026-01-01T10:00:00+00:00",
                       "unloaded_at": "2026-01-01T18:00:00+00:00"}],
        },
    )
    assert c.status_code == 409
    assert c.json()["error"]["code"] == "stale_precheck_digest"


# ----------------------------------------------------------- 封存


def _closed_excursion(client, cargo_id="C1"):
    """造一个已闭合越温区间，返回开启事件ID。"""
    client.post(
        "/events",
        json={"event_id": "m1", "cargo_id": cargo_id,
              "occurred_at": "2026-01-01T11:00:00+00:00", "temperature": "8"},
    )
    client.post(
        "/events",
        json={"event_id": "m2", "cargo_id": cargo_id,
              "occurred_at": "2026-01-01T12:00:00+00:00", "temperature": "2"},
    )


def test_seal_records_limits_and_standards(make_cargo, client):
    make_cargo(limit="5")
    # 11:30 起上限 4（m1 在 11:00 仍按 5；本用例重点验证历程与逐证据上限）
    assert _std(client, "C1", "ch1", "2026-01-01T11:30:00+00:00", "4").status_code == 201
    _closed_excursion(client)
    interval = client.get("/cargo/C1/responsibilities").json()["intervals"][0]
    snap = interval["snapshot_id"]

    r = client.post(
        "/evidence-seals",
        json={"case_id": "CASE-1", "cargo_id": "C1", "interval_id": "m1",
              "snapshot_id": snap},
    )
    assert r.status_code == 201, r.text
    content = r.json()["content"]
    versions = [s["version"] for s in content["temp_standards"]]
    assert versions == [0, 1]
    own = {e["event_id"]: e for e in content["own_events"]}
    assert own["m1"]["temp_upper_limit"] == "5.000"
    assert own["m2"]["temp_upper_limit"] == "4.000"


def test_seal_diff_shows_standard_change(make_cargo, client):
    make_cargo(limit="5")
    _closed_excursion(client)
    snap = client.get("/cargo/C1/responsibilities").json()["intervals"][0]["snapshot_id"]
    r = client.post(
        "/evidence-seals",
        json={"case_id": "CASE-2", "cargo_id": "C1", "interval_id": "m1",
              "snapshot_id": snap},
    )
    assert r.status_code == 201, r.text

    # 封存后补录一笔更严格的标准（10:30 起上限 3）——m1 的 8 仍越温、m2 的 2
    # 仍闭合，区间几何不变（unchanged），但证据当时上限与标准历程应出现差异
    assert _std(client, "C1", "ch1", "2026-01-01T10:30:00+00:00", "3").status_code == 201
    diff = client.get("/evidence-seals/CASE-2/diff").json()
    assert diff["status"] == "unchanged"
    # 标准历程新增版本 1
    added = [c for c in diff["temp_standard_changes"] if c["change"] == "added"]
    assert any(c["key"] == "1" and c["current"]["change_id"] == "ch1" for c in added)
    assert {s["version"] for s in diff["current_temp_standards"]} == {0, 1}
    # 测温证据的当时上限变化
    own_changes = {c["key"]: c for c in diff["evidence_changes"]["own_events"]
                   if c["change"] == "changed"}
    assert set(own_changes) == {"m1", "m2"}
    for c in own_changes.values():
        assert "temp_upper_limit" in c["fields"]
        assert c["sealed"]["temp_upper_limit"] == "5.000"
        assert c["current"]["temp_upper_limit"] == "3.000"
    # 原封存包不变
    pack = client.get("/evidence-seals/CASE-2").json()
    assert [s["version"] for s in pack["content"]["temp_standards"]] == [0]
    own_pack = {e["event_id"]: e for e in pack["content"]["own_events"]}
    assert own_pack["m1"]["temp_upper_limit"] == "5.000"


def test_seal_replay_same_request_after_standard_change(make_cargo, client):
    make_cargo(limit="5")
    _closed_excursion(client)
    snap = client.get("/cargo/C1/responsibilities").json()["intervals"][0]["snapshot_id"]
    body = {
        "case_id": "CASE-3", "cargo_id": "C1", "interval_id": "m1",
        "snapshot_id": snap,
    }
    assert client.post("/evidence-seals", json=body).status_code == 201
    # 标准变化后原请求重传仍返回原包
    _std(client, "C1", "ch1", "2026-01-01T10:30:00+00:00", "3")
    r = client.post("/evidence-seals", json=body)
    assert r.status_code == 200
    assert r.json()["replayed"] is True
    assert [s["version"] for s in r.json()["seal"]["content"]["temp_standards"]] == [0]


# ----------------------------------------------------------- 责任快照


def test_stance_snapshot_invalidated_by_standard_change(make_cargo, client):
    """标准变化改变区间闭合边界后，旧快照表态被拒（stale_snapshot）；
    表态历史保留并标记 is_current=false。"""
    make_cargo(limit="5")
    # 11:00 温度 8 开启；11:30 温度 4（<=5）闭合 -> 区间 [11:00, 11:30)
    for eid, at, t in [
        ("s-m1", "2026-01-01T11:00:00+00:00", "8"),
        ("s-m2", "2026-01-01T11:30:00+00:00", "4"),
    ]:
        assert client.post(
            "/events",
            json={"event_id": eid, "cargo_id": "C1",
                  "occurred_at": at, "temperature": t},
        ).status_code == 201
    interval = client.get("/cargo/C1/responsibilities").json()["intervals"][0]
    snap_v1 = interval["snapshot_id"]
    assert interval["end"].startswith("2026-01-01T11:30:00")
    # 旧快照表态成功
    r = client.post(
        "/cargo/C1/responsibilities/s-m1/stances",
        json={"stance_id": "st1", "responsible_party": "A仓库",
              "stance": "confirm", "snapshot_id": snap_v1},
    )
    assert r.status_code == 201, r.text

    # 补录：11:20 起上限降为 3 -> 11:30 的 4 不再闭合（4>3），区间延到
    # 之后首个不超限读数；加一笔 12:00 温度 2 使区间在 12:00 闭合
    assert _std(client, "C1", "ch1", "2026-01-01T11:20:00+00:00", "3").status_code == 201
    assert client.post(
        "/events",
        json={"event_id": "s-m3", "cargo_id": "C1",
              "occurred_at": "2026-01-01T12:00:00+00:00", "temperature": "2"},
    ).status_code == 201
    interval2 = client.get("/cargo/C1/responsibilities").json()["intervals"][0]
    assert interval2["end"].startswith("2026-01-01T12:00:00")
    snap_v2 = interval2["snapshot_id"]
    assert snap_v2 != snap_v1

    # 用旧快照对新区间表态 -> 409 stale_snapshot
    r = client.post(
        "/cargo/C1/responsibilities/s-m1/stances",
        json={"stance_id": "st2", "responsible_party": "A仓库",
              "stance": "confirm", "snapshot_id": snap_v1},
    )
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "stale_snapshot"
    # 新快照可以表态
    r = client.post(
        "/cargo/C1/responsibilities/s-m1/stances",
        json={"stance_id": "st3", "responsible_party": "A仓库",
              "stance": "confirm", "snapshot_id": snap_v2},
    )
    assert r.status_code == 201, r.text
    # 表态历史保留：旧表态 is_current=false，新表态 is_current=true
    hist = client.get("/cargo/C1/responsibilities/s-m1/stances").json()
    currents = {h["stance_id"]: h["is_current"] for h in hist["history"]}
    assert currents == {"st1": False, "st3": True}


def test_interval_disappears_after_relaxed_standard(make_cargo, client):
    """补录放宽标准使既有区间消失：责任列表为空，旧快照表态 404。"""
    make_cargo(limit="5")
    for eid, at, t in [
        ("d-m1", "2026-01-01T11:00:00+00:00", "8"),
        ("d-m2", "2026-01-01T11:30:00+00:00", "2"),
    ]:
        client.post(
            "/events",
            json={"event_id": eid, "cargo_id": "C1",
                  "occurred_at": at, "temperature": t},
        )
    snap = client.get("/cargo/C1/responsibilities").json()["intervals"][0]["snapshot_id"]
    # 10:30 起上限放宽到 9：11:00 的 8 不再越温 -> 区间消失
    assert _std(client, "C1", "ch1", "2026-01-01T10:30:00+00:00", "9").status_code == 201
    assert client.get("/cargo/C1/responsibilities").json()["intervals"] == []
    r = client.post(
        "/cargo/C1/responsibilities/d-m1/stances",
        json={"stance_id": "stX", "responsible_party": "A仓库",
              "stance": "confirm", "snapshot_id": snap},
    )
    assert r.status_code == 404


def test_precheck_confirm_succeeds_with_standards_bound_digest(client):
    """含标准历程的预检摘要在无变化时确认成功，装卸正常登记。"""
    client.post("/trips", json={"trip_id": "TOK", "vehicle_no": "京OK"})
    client.post(
        "/cargo",
        json={"cargo_id": "OK1", "started_at": "2026-01-01T08:00:00+00:00",
              "temp_upper_limit": "5", "initial_holder": "A"},
    )
    # 先做一笔标准变更（预约未来生效）再预检
    r = client.post(
        "/cargo/OK1/temp-standards",
        json={"change_id": "ok-ch1",
              "effective_at": "2099-01-01T00:00:00+00:00",
              "new_upper_limit": "2", "expected_version": 0, "reason": "未来"},
    )
    assert r.status_code == 201
    revision = client.get("/trips/TOK").json()["revision"]
    pre = client.post(
        "/trips/TOK/manifest/precheck",
        json={
            "expected_trip_revision": revision,
            "items": [{"cargo_id": "OK1",
                       "loaded_at": "2026-01-01T10:00:00+00:00",
                       "unloaded_at": "2026-01-01T18:00:00+00:00"}],
        },
    )
    assert pre.status_code == 200, pre.text
    digest = pre.json()["digest"]
    conf = client.post(
        "/trips/TOK/manifest/confirm",
        json={
            "digest": digest,
            "items": [{"cargo_id": "OK1",
                       "loaded_at": "2026-01-01T10:00:00+00:00",
                       "unloaded_at": "2026-01-01T18:00:00+00:00"}],
        },
    )
    assert conf.status_code == 201, conf.text
    # 同一摘要不能复用（车次修订号已 +1）
    again = client.post(
        "/trips/TOK/manifest/confirm",
        json={
            "digest": digest,
            "items": [{"cargo_id": "OK1",
                       "loaded_at": "2026-01-02T10:00:00+00:00",
                       "unloaded_at": "2026-01-02T18:00:00+00:00"}],
        },
    )
    assert again.status_code == 409
