-- Occupancy above capacity is real: the sensor counts entries minus exits, and
-- 2026-09-03 genuinely peaked at 157 of 150. So the ceiling is generous rather
-- than 1.0, and only the impossible fails: negative, or past the import's own
-- 120% drift ceiling with room to spare.

select
    local_day,
    bucket,
    pct
from {{ ref('daily_bucket_means') }}
where pct < 0 or pct > 1.5
