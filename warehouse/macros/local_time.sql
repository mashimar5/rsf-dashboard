{#
  The whole project turns on reading a stored UTC instant as Berkeley local
  time, and that is where the two warehouses actually differ. Postgres spells
  it `AT TIME ZONE`; Snowflake spells it `CONVERT_TIMEZONE`. ISO weekday
  numbering and integer division differ too.

  The Snowflake branches are written from the documentation and stay unproven
  until the first run against a real account; what each returns (notably
  whether CONVERT_TIMEZONE hands back a timestamp with or without an offset)
  is the first thing to check there.
#}

{% macro local_at(column) -%}
  {%- if target.type == 'snowflake' -%}
    convert_timezone('{{ var("local_zone") }}', {{ column }})
  {%- else -%}
    ({{ column }} at time zone '{{ var("local_zone") }}')
  {%- endif -%}
{%- endmacro %}


{% macro iso_weekday(column) -%}
  {%- if target.type == 'snowflake' -%}
    dayofweekiso({{ column }})
  {%- else -%}
    cast(extract(isodow from {{ column }}) as integer)
  {%- endif -%}
{%- endmacro %}


{% macro minutes_since_midnight(column) -%}
  {%- if target.type == 'snowflake' -%}
    (hour({{ column }}) * 60 + minute({{ column }}))
  {%- else -%}
    cast(extract(hour from {{ column }}) * 60 + extract(minute from {{ column }}) as integer)
  {%- endif -%}
{%- endmacro %}


{#  Postgres integer division truncates; Snowflake's `/` returns a decimal. #}
{% macro bucket_of(column) -%}
  cast(floor({{ minutes_since_midnight(column) }} / {{ var('bucket_minutes') }}) as integer)
{%- endmacro %}


{% macro days_before(column, days) -%}
  {%- if target.type == 'snowflake' -%}
    dateadd(day, -{{ days }}, {{ column }})
  {%- else -%}
    ({{ column }} - interval '{{ days }} days')
  {%- endif -%}
{%- endmacro %}
