{#
  What a typical weekday looks like within each kind of academic period: the
  median across days, with quartiles and range beside it.

  This is deliberately not the dashboard's curve. The dashboard answers "what
  should today look like" with a rolling window of the eight most recent
  matching instances; this answers the slower question, "what does a Monday in
  term look like over the past year", which is the shape the chat reasons over.

  The window is measured from the newest reading rather than from today, so the
  model gives the same answer on a copy of the data that has stopped updating.
#}

with bounds as (

    select max(local_day) as newest from {{ ref('daily_bucket_means') }}

),

recent as (

    select d.*
    from {{ ref('daily_bucket_means') }} d
    cross join bounds b
    where d.local_day > {{ days_before('b.newest', var('profile_days')) }}

),

labelled as (

    select
        r.local_day,
        r.iso_weekday,
        r.bucket,
        r.pct,
        coalesce(c.kind, 'unlabelled') as kind
    from recent r
    left join {{ ref('calendar_days') }} c on c.day = r.local_day

)

select
    kind,
    iso_weekday,
    bucket,
    percentile_cont(0.5) within group (order by pct) as median_pct,
    percentile_cont(0.25) within group (order by pct) as q1_pct,
    percentile_cont(0.75) within group (order by pct) as q3_pct,
    min(pct) as low_pct,
    max(pct) as high_pct,
    count(distinct local_day) as instances
from labelled
group by kind, iso_weekday, bucket
