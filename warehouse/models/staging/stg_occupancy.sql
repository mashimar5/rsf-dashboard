{#
  Every reading, live and imported, in Berkeley local time with the day,
  weekday and half-hour bucket the rest of the project groups by.

  This is the only model that converts time zones, so the DST-correct local
  day exists in one place rather than in every query that needs it.
#}

with source as (

    select observed_at, count, capacity
    from {{ source('rsf', 'occupancy') }}
    where capacity > 0

),

localised as (

    select
        observed_at,
        {{ local_at('observed_at') }} as local_at,
        count as people,
        capacity,
        cast(count as double precision) / capacity as pct
    from source

)

select
    observed_at,
    local_at,
    cast(local_at as date) as local_day,
    {{ iso_weekday('local_at') }} as iso_weekday,
    {{ bucket_of('local_at') }} as bucket,
    cast(extract(hour from local_at) as integer) as local_hour,
    people,
    capacity,
    pct
from localised
