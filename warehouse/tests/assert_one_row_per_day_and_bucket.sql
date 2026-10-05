-- The grain claim behind "every day gets one vote". If this returns rows, a
-- day could be counted twice in the median and a busy Monday would outvote
-- the others without anyone noticing.

select
    local_day,
    bucket,
    count(*) as rows_for_grain
from {{ ref('daily_bucket_means') }}
group by local_day, bucket
having count(*) > 1
