"""业务逻辑：事件入库、幂等去重、乱序后的最新状态查询、时钟回拨校正、高温暴露积分。"""
from datetime import datetime, timedelta, timezone
from fractions import Fraction

from fastapi import HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from . import models, schemas

_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def _content_equal(existing: models.BoxEvent, payload: schemas.EventIn) -> bool:
    """内容是否一致（温度 + 事件时间）。

    event_time 在 schema 层已统一换算为 UTC naive，可直接比较；
    设备重试会原样重发 JSON，float 解析结果相同，故直接判等。
    """
    return (
        existing.temperature == payload.temperature
        and existing.event_time == payload.event_time
    )


def submit_event(db: Session, payload: schemas.EventIn) -> tuple[models.BoxEvent, bool]:
    """写入一个事件。

    返回 (事件行, 是否重复)。
    - 同箱号同序号且内容相同：视为设备重试/延迟的重复上报，幂等返回原行，不改状态
    - 同箱号同序号但内容不同：拒绝（409）
    - 新序号：直接入库（即使乱序，不要求 seq 连续）
    """
    existing = db.scalar(
        select(models.BoxEvent).where(
            models.BoxEvent.box_code == payload.box_code,
            models.BoxEvent.seq == payload.seq,
        )
    )

    if existing is not None:
        if not _content_equal(existing, payload):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "error": "seq_conflict",
                    "message": (
                        f"箱号 {payload.box_code} 的序号 {payload.seq} 已存在，"
                        "但温度/事件时间与本次上报不一致，已拒绝"
                    ),
                    "existing": {
                        "temperature": existing.temperature,
                        "event_time": existing.event_time.isoformat() + "Z",
                    },
                    "received": {
                        "temperature": payload.temperature,
                        "event_time": payload.event_time.isoformat() + "Z",
                    },
                },
            )
        return existing, True

    event = models.BoxEvent(
        box_code=payload.box_code,
        seq=payload.seq,
        temperature=payload.temperature,
        event_time=payload.event_time,
    )
    db.add(event)
    try:
        db.flush()  # 触发唯一约束，兜住并发下的竞态
    except IntegrityError:
        db.rollback()
        # 并发请求抢先写入：以库中记录为准重新判定
        existing = db.scalar(
            select(models.BoxEvent).where(
                models.BoxEvent.box_code == payload.box_code,
                models.BoxEvent.seq == payload.seq,
            )
        )
        if existing is None:  # 理论上不会发生
            raise
        if not _content_equal(existing, payload):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={"error": "seq_conflict", "message": "同序号内容冲突，已拒绝"},
            )
        return existing, True

    db.commit()
    db.refresh(event)
    return event, False


def list_boxes(db: Session) -> list[str]:
    rows = db.scalars(
        select(models.BoxEvent.box_code)
        .distinct()
        .order_by(models.BoxEvent.box_code)
    ).all()
    return list(rows)


def _latest_subquery():
    """每个箱号当前最大序号（即最新状态），乱序入库也不受影响。"""
    return (
        select(
            models.BoxEvent.box_code.label("box_code"),
            func.max(models.BoxEvent.seq).label("max_seq"),
        )
        .group_by(models.BoxEvent.box_code)
        .subquery()
    )


def latest_states(db: Session) -> list[models.BoxEvent]:
    latest = _latest_subquery()
    rows = db.scalars(
        select(models.BoxEvent)
        .join(
            latest,
            (models.BoxEvent.box_code == latest.c.box_code)
            & (models.BoxEvent.seq == latest.c.max_seq),
        )
        .order_by(models.BoxEvent.box_code)
    ).all()
    return list(rows)


def latest_state(db: Session, box_code: str) -> models.BoxEvent | None:
    latest = _latest_subquery()
    return db.scalar(
        select(models.BoxEvent)
        .join(
            latest,
            (models.BoxEvent.box_code == latest.c.box_code)
            & (models.BoxEvent.seq == latest.c.max_seq),
        ).where(models.BoxEvent.box_code == box_code)
    )


def list_events(db: Session, box_code: str) -> list[models.BoxEvent]:
    return list(
        db.scalars(
            select(models.BoxEvent)
            .where(models.BoxEvent.box_code == box_code)
            .order_by(models.BoxEvent.seq)
        ).all()
    )


def _to_epoch_micros(dt: datetime) -> int:
    """UTC naive datetime -> 自 Unix 纪元的整数微秒（精确，无浮点误差）。"""
    delta = dt.replace(tzinfo=timezone.utc) - _EPOCH
    return (delta.days * 86400 + delta.seconds) * 1_000_000 + delta.microseconds


def _from_epoch_micros(micros: int) -> datetime:
    """整数微秒 -> UTC naive datetime。"""
    secs, rem = divmod(micros, 1_000_000)
    return (_EPOCH + timedelta(seconds=secs, microseconds=rem)).replace(tzinfo=None)


def _isotonic_blocks(values: list[int]) -> list[tuple[int, int, int]]:
    """等权保序回归（PAVA，Pool Adjacent Violators Algorithm）。

    输入按 seq 升序的整数微秒事件时间；在“建议值非递减”约束下最小化
    sum((建议值 - 原值)^2)，等价于把相邻违规块不断合并、块内取算术均值。

    为避免大整数除法的浮点误差，块以 (序号起点, 序号终点, 总和, 个数)
    维护；块间比较与均值输出均用整数运算。均值四舍五入到微秒
    （半数远离零；微秒均值不可能出现半数情形时同样正确）。

    返回 [(index_from, index_to, rounded_mean_micros), ...]，
    下标对应输入 values 的位置，由调用方映射回事件 seq。
    """
    # 每个元素：[seq_from, seq_to, total_micros, count]
    blocks: list[list[int]] = []
    for i, v in enumerate(values):
        block = [i, i, v, 1]
        # 前一块均值 > 当前块均值即为违规，向前合并直到满足非递减
        while blocks:
            prev = blocks[-1]
            # prev.total / prev.count > block.total / block.count
            if prev[2] * block[3] <= block[2] * prev[3]:
                break
            blocks.pop()
            block = [
                prev[0],
                block[1],
                prev[2] + block[2],
                prev[3] + block[3],
            ]
        blocks.append(block)

    def _round_half_away(total: int, count: int) -> int:
        q, r = divmod(abs(total), count)
        # 余数 * 2 >= 分母即小数部分 >= 0.5，向远离零方向进位
        if 2 * r >= count:
            q += 1
        return q if total >= 0 else -q

    return [
        (b[0], b[1], _round_half_away(b[2], b[3])) for b in blocks
    ]


def clock_audit(
    db: Session, box_code: str, seq_from: int, seq_to: int
) -> list[schemas.ClockAuditItem] | None:
    """对指定序号区间（含端点）生成设备时钟回拨校正建议。

    - 箱号不存在（从未上报过）：返回 None，由路由层转 404
    - 箱号存在但区间内无事件：返回 []
    - 校正只在本次查询中即时计算，不改动任何原始记录；乱序补报后再次查询
      会基于最新数据重新计算
    """
    box_exists = db.scalar(
        select(func.count())
        .select_from(models.BoxEvent)
        .where(models.BoxEvent.box_code == box_code)
    )
    if not box_exists:
        return None

    events = list(
        db.scalars(
            select(models.BoxEvent)
            .where(
                models.BoxEvent.box_code == box_code,
                models.BoxEvent.seq >= seq_from,
                models.BoxEvent.seq <= seq_to,
            )
            .order_by(models.BoxEvent.seq)
        ).all()
    )
    if not events:
        return []

    values = [_to_epoch_micros(e.event_time) for e in events]
    suggested, block_range = _isotonic_suggest(values, events)

    return [
        schemas.ClockAuditItem(
            seq=event.seq,
            original_time=event.event_time,
            suggested_time=_from_epoch_micros(sugg),
            offset_micros=sugg - values[i],
            block_seq_from=block_range[i][0],
            block_seq_to=block_range[i][1],
        )
        for i, (event, sugg) in enumerate(zip(events, suggested))
    ]


def _isotonic_suggest(
    values: list[int], events: list[models.BoxEvent]
) -> tuple[list[int], list[tuple[int, int]]]:
    """对整数微秒事件时间做等权保序回归，返回 (逐事件建议微秒, 逐事件块序号范围)。

    clock-audit 与 heat-exposure 共用同一套校正逻辑：PAVA 合并违规块、
    块内取算术均值并四舍五入到微秒，建议序列保证非递减。
    """
    blocks = _isotonic_blocks(values)
    suggested: list[int] = [0] * len(events)
    block_range: list[tuple[int, int]] = [(0, 0)] * len(events)
    for idx_from, idx_to, mean_micros in blocks:
        seq_from_block = events[idx_from].seq
        seq_to_block = events[idx_to].seq
        for i in range(idx_from, idx_to + 1):
            suggested[i] = mean_micros
            block_range[i] = (seq_from_block, seq_to_block)
    return suggested, block_range


def _range_events(
    db: Session, box_code: str, seq_from: int, seq_to: int
) -> tuple[bool, list[models.BoxEvent]]:
    """取某箱号序号区间（含端点）内按 seq 升序的事件。

    返回 (箱号是否存在, 事件列表)。箱号存在但区间无事件时返回 (True, [])。
    """
    box_exists = db.scalar(
        select(func.count())
        .select_from(models.BoxEvent)
        .where(models.BoxEvent.box_code == box_code)
    )
    if not box_exists:
        return False, []
    events = list(
        db.scalars(
            select(models.BoxEvent)
            .where(
                models.BoxEvent.box_code == box_code,
                models.BoxEvent.seq >= seq_from,
                models.BoxEvent.seq <= seq_to,
            )
            .order_by(models.BoxEvent.seq)
        ).all()
    )
    return True, events


def _linear_excess_degree_seconds(
    temp_from: Fraction,
    temp_to: Fraction,
    upper_limit: Fraction,
    gap_micros: int,
) -> Fraction:
    """相邻事件间温度视为线性变化时，超过上限部分的精确积分（摄氏度·秒）。

    用有理数精确计算，避免浮点误差：
    - 两端都不超限：0
    - 两端都超限：梯形面积 ((t0-L)+(t1-L))/2 * 时长
    - 恰好一端超限：超限侧的三角形面积，线性过上限点按比例切出时长
    """
    if temp_from <= upper_limit and temp_to <= upper_limit:
        return Fraction(0)
    if temp_from > upper_limit and temp_to > upper_limit:
        excess = (temp_from - upper_limit) + (temp_to - upper_limit)
        return excess * gap_micros / 2 / 1_000_000
    if temp_from > upper_limit:
        return (
            (temp_from - upper_limit)
            * (temp_from - upper_limit)
            * gap_micros
            / (temp_from - temp_to)
            / 2
            / 1_000_000
        )
    # 仅终点超限
    return (
        (temp_to - upper_limit)
        * (temp_to - upper_limit)
        * gap_micros
        / (temp_to - temp_from)
        / 2
        / 1_000_000
    )


def heat_exposure(
    db: Session,
    box_code: str,
    seq_from: int,
    seq_to: int,
    upper_limit: float,
    max_gap_seconds: float,
) -> schemas.HeatExposureReport | None:
    """计算序号区间内温度超过 upper_limit 的精确暴露量（摄氏度·秒）。

    - 箱号不存在（从未上报过）：返回 None，由路由层转 404
    - 箱号存在但区间内无事件：返回总计为 0、segments 为空的零值报告
    - 校正时间与积分均在查询时即时计算，不改动任何原始记录；补报后重查即重算
    - 校正后间隔为零或超过 max_gap_seconds 的相邻事件段不计入积分；
      缺号不补点，仅按区间内实际事件两两成段
    """
    box_exists, events = _range_events(db, box_code, seq_from, seq_to)
    if not box_exists:
        return None

    # 上限/最大间隔用十进制字符串构造有理数，忠实于请求中的数值表示
    limit = Fraction(str(upper_limit))
    max_gap = Fraction(str(max_gap_seconds))

    report = schemas.HeatExposureReport(
        box_code=box_code,
        seq_from=seq_from,
        seq_to=seq_to,
        upper_limit=upper_limit,
        max_gap_seconds=max_gap_seconds,
        event_count=len(events),
        total_degree_seconds=0.0,
        segments=[],
    )
    if len(events) < 2:
        # 无事件或仅单条事件：没有可积分的时间段
        return report

    values = [_to_epoch_micros(e.event_time) for e in events]
    suggested, _ = _isotonic_suggest(values, events)

    total = Fraction(0)
    segments: list[schemas.HeatExposureSegment] = []
    for i in range(len(events) - 1):
        left, right = events[i], events[i + 1]
        gap_micros = suggested[i + 1] - suggested[i]  # 保序回归保证非负
        gap_seconds = gap_micros / 1_000_000

        counted = True
        excluded_reason: str | None = None
        if gap_micros == 0:
            counted = False
            excluded_reason = "zero_interval"
        elif gap_seconds > max_gap:
            counted = False
            excluded_reason = "gap_too_large"

        if counted:
            excess = _linear_excess_degree_seconds(
                Fraction(str(left.temperature)),
                Fraction(str(right.temperature)),
                limit,
                gap_micros,
            )
            total += excess
            excess_value = float(excess)
        else:
            excess_value = 0.0

        segments.append(
            schemas.HeatExposureSegment(
                seq_from=left.seq,
                seq_to=right.seq,
                corrected_time_from=_from_epoch_micros(suggested[i]),
                corrected_time_to=_from_epoch_micros(suggested[i + 1]),
                interval_seconds=float(gap_seconds),
                temperature_from=left.temperature,
                temperature_to=right.temperature,
                excess_degree_seconds=excess_value,
                counted=counted,
                excluded_reason=excluded_reason,
            )
        )

    report.segments = segments
    report.total_degree_seconds = float(total)
    return report
