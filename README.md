# check_influxdb_data_age

A Nagios/Icinga plugin that checks how old the newest data is that an
InfluxDB 2 Flux query returns, to catch measurements that stopped arriving.
Python 3.7 or newer, standard library only.

It posts the query to InfluxDB's
[query API](https://docs.influxdata.com/influxdb/v2/query-data/execute-queries/influx-api/)
and takes the newest `_time` over all tables and rows of the answer.

## Usage

```sh
check_influxdb_data_age -H influx.example.org -f /etc/icinga2/influx.token -o 0123456789abcdef \
    --query 'from(bucket: "sensors") |> range(start: -1d) |> filter(fn: (r) => r._measurement == "room") |> last()'
```

```
INFLUXDB DATA AGE OK - newest entry is 1m 40s old (2026-10-02 07:10:45 UTC) | age=100s;600;1800
```

| Option | Meaning |
|---|---|
| `-H`, `--host` | InfluxDB host (default: localhost) |
| `-p`, `--port` | Port (default: 8086) |
| `-S`, `--ssl` | Use HTTPS; `--ca-cert` and `--insecure` control the certificate check |
| `-f`, `--token-file` | File holding the API token |
| `-o`, `--org-id` / `--org` | ID or name of the organization (one is required) |
| `--query` | Flux query; its answer needs a `_time` column (required) |
| `-w`, `-c` | Ranges for the age of the newest entry (default: `10m` and `30m`) |
| `--empty-state` | State if the query returns no data (default: critical) |
| `--allow-timestamps-in-future` | Accept entries in the future, optionally only up to a duration |
| `-T`, `--timeout` | Timeout in seconds (default: 10) |
| `-v`, `--verbose` | Print the request, without the token, and the answer to stderr |
| `-V`, `--version` | Show the version |

### Token

Never pass the token with `-t` in a service definition: command lines are
visible to every user via `ps`. Put it in a file that only the monitoring
user can read and pass it with `-f`, or set `$INFLUX_TOKEN_FILE` or
`$INFLUX_TOKEN`. A read-only token for the buckets of the query is enough.

### Thresholds

`-w` and `-c` take the
[monitoring plugins range format](https://www.monitoring-plugins.org/doc/guidelines.html#THRESHOLDFORMAT)
in seconds, and a unit `s`, `m`, `h` or `d` may follow each number: the
defaults `-w 10m -c 30m` alert when the newest entry is older than 10 and 30
minutes.

### Timestamps in the future

A newest entry with a timestamp in the future means that the clock of the
writer, or of the host running the check, is off. It is reported as WARNING,
unless `--allow-timestamps-in-future` is given. With a duration, e.g.
`--allow-timestamps-in-future 30s`, only entries up to that far in the
future are accepted, which allows for small clock offsets. An accepted entry
counts as current: `-w` and `-c` apply to old data only.

### Queries

Restrict the query with `range()`, so InfluxDB does not scan the whole
bucket, and end it with `last()`, so it returns one row per series. A query
whose `range()` contains no data returns nothing, which is reported with
`--empty-state`, by default CRITICAL: the data is older than the range.

## States

- **OK**, **WARNING**, **CRITICAL**: the age of the newest entry is inside
  the ranges, outside `-w`, or outside `-c`
- **CRITICAL**: InfluxDB cannot be reached, does not answer within `-T`, or
  answers with a server error
- **WARNING**: the newest entry has a timestamp in the future, see above
- **UNKNOWN**: InfluxDB rejects the token or the query, the answer has no
  `_time` column or cannot be read, or the command line is wrong

The plugin prints exactly one line, with the age as performance data.

## Upgrading from the monitoring-plugins collection

This plugin was `check_influxdb_data_age.py` in the monitoring-plugins
collection. The options are the same, apart from:

- `-w`/`-c` are ranges now. A plain number still alerts above that many
  seconds, but the old defaults `-w 1800 -c 600` were reversed and made
  WARNING unreachable; the new defaults are `-w 10m -c 30m`.
- An empty query result is CRITICAL instead of UNKNOWN, see `--empty-state`.
- `-ssl` still works, `-S`/`--ssl` is the new spelling.
- The output starts with `INFLUXDB DATA AGE` and uses the full state names.

## Bash completion

`completions/check_influxdb_data_age` completes the options and their values,
also when the plugin is called by its full path. The packages install it;
otherwise copy it to bash-completion's directory:

```sh
install -D -m 0644 completions/check_influxdb_data_age /usr/share/bash-completion/completions/check_influxdb_data_age
```

## Tests

```sh
python3 -m unittest -v
```

The tests run the plugin against a fake InfluxDB query API.

## License

GPL-2.0-or-later, see [LICENSE](LICENSE).
