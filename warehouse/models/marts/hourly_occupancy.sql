{#
  The hourly grain the random forest trains on (forest.hourly_occupancy), kept
  here so the same shape is queryable without Python. `samples` is carried so
  a consumer can apply the forest's own rule: an hour counts as observed only
  with at least four readings behind it.
#}

select
    local_day,
    iso_weekday,
    local_hour,
    avg(pct) as pct,
    count(*) as samples
from {{ ref('stg_occupancy') }}
group by local_day, iso_weekday, local_hour
