"""SQLite checks shared by fresh schemas and targeted legacy-table guards."""

MIN_INTEGER = -(2**63)


def baseline_balance_sql(prefix: str = '') -> str:
    revenue = prefix + 'monthly_revenue_minor'
    costs = [prefix + 'monthly_' + name + '_minor' for name in ('cogs', 'fulfillment', 'incentive', 'other_costs')]
    contribution = prefix + 'monthly_contribution_minor'
    invalid = ' OR '.join(f"typeof({column}) <> 'integer' OR {column} < 0" for column in [revenue, *costs])
    # revenue and first cost are both in [0, INT64_MAX], so their difference fits.
    # SQLite CASE short-circuits: never evaluate a subtraction until its lower
    # bound is checked using MIN_INTEGER + nonnegative cost (also always exact).
    expression = f'({revenue} - {costs[0]})'
    branches = [f"WHEN {invalid} OR typeof({contribution}) <> 'integer' THEN 0"]
    for cost in costs[1:]:
        branches.append(f'WHEN {expression} < ({MIN_INTEGER} + {cost}) THEN 0')
        expression = f'({expression} - {cost})'
    return 'CASE ' + ' '.join(branches) + f' ELSE {contribution} = {expression} END'


def utc_timestamp_sql(column: str) -> str:
    digit = '[0-9]'
    pattern = digit*4 + '-' + digit*2 + '-' + digit*2 + 'T' + digit*2 + ':' + digit*2 + ':' + digit*2 + '.' + digit*6 + '+00:00'
    base = f'substr({column}, 1, 19)'
    formatted = f"strftime('%Y-%m-%dT%H:%M:%S', {base})"
    return (f"(typeof({column}) = 'text' AND length({column}) = 32 "
            f"AND {column} GLOB '{pattern}' AND substr({column},1,4) >= '0001' "
            f"AND substr({column},12,2) BETWEEN '00' AND '23' "
            f"AND {formatted} IS NOT NULL AND {formatted} = {base})")


def nonblank_sql(column: str) -> str:
    # Python str.strip() whitespace, including Unicode spaces, for direct SQL.
    whitespace = 'char(9,10,11,12,13,28,29,30,31,32,133,160,5760,8192,8193,8194,8195,8196,8197,8198,8199,8200,8201,8202,8232,8233,8239,8287,12288)'
    return f"(typeof({column}) = 'text' AND length(trim({column}, {whitespace})) > 0)"


def mission_metadata_sql(prefix: str = '') -> str:
    start, end, at = (prefix + name for name in ('starts_at', 'ends_at', 'approved_at'))
    actor, digest, status = (prefix + name for name in ('approved_by', 'parameters_hash', 'status'))
    return (f"{utc_timestamp_sql(start)} AND {utc_timestamp_sql(end)} AND {end} > {start} "
            f"AND ({at} IS NULL OR {utc_timestamp_sql(at)}) "
            f"AND ({actor} IS NULL OR {nonblank_sql(actor)}) "
            f"AND ({digest} IS NULL OR {nonblank_sql(digest)}) "
            f"AND ({status} IN ('DRAFT','EXPIRED') OR "
            f"({actor} IS NOT NULL AND {at} IS NOT NULL AND {digest} IS NOT NULL))")
