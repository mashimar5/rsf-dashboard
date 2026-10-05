{#
  One value per day and half-hour bucket, which is what gives every day one
  vote. Live collection samples every four minutes and the imported history
  every ten, so a median over raw readings would let a live day outweigh a
  historical one about 2.5 to 1.

  store.weekday_bands does the same aggregation inline for one target date.
  This is the standalone table the warehouse models build on; the dashboard
  does not read it.
#}

select
    local_day,
    iso_weekday,
    bucket,
    avg(pct) as pct,
    count(*) as samples
from {{ ref('stg_occupancy') }}
group by local_day, iso_weekday, bucket
