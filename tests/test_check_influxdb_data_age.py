"""Tests for check_influxdb_data_age against a fake InfluxDB 2 query API."""
import datetime
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN = os.path.join(os.path.dirname(HERE), "check_influxdb_data_age")
TOKEN = "secret-token"


def stamp(seconds_ago, digits=9, zone="Z"):
    """An RFC 3339 timestamp as InfluxDB writes it, with nanoseconds."""
    moment = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(seconds=seconds_ago)
    if zone != "Z":
        hours = int(zone[:3])
        moment = moment.astimezone(datetime.timezone(datetime.timedelta(hours=hours)))
    text = moment.strftime("%Y-%m-%dT%H:%M:%S")
    if digits:
        text += "." + (moment.strftime("%f") + "123")[:digits]
    return text + zone


def table(*ages, extra="_value"):
    lines = [",result,table,_start,_stop,_time,%s,_field,_measurement" % extra]
    for index, age in enumerate(ages):
        lines.append(",_result,0,2026-01-01T00:00:00Z,2026-12-31T00:00:00Z,%s,%d,temp,room" % (stamp(age), index))
    return "\r\n".join(lines) + "\r\n"


class FakeInflux(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def reply(self, code, body, content_type="text/csv; charset=utf-8"):
        data = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        query = self.rfile.read(length).decode()
        url = urllib.parse.urlparse(self.path)
        self.server.requests.append({"path": url.path, "params": urllib.parse.parse_qs(url.query),
                                     "query": query, "headers": dict(self.headers)})
        scenario = self.server.scenario
        if scenario.get("delay"):
            time.sleep(scenario["delay"])
        if self.headers.get("Authorization") != "Token %s" % TOKEN:
            return self.reply(401, json.dumps({"code": "unauthorized", "message": "unauthorized access"}),
                              "application/json")
        if "status" in scenario:
            return self.reply(scenario["status"], json.dumps({"code": "invalid", "message": scenario["message"]}),
                              "application/json")
        return self.reply(200, scenario["body"]() if callable(scenario["body"]) else scenario["body"])


class TestPlugin(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), FakeInflux)
        cls.server.daemon_threads = True
        cls.server.requests = []
        cls.server.scenario = {}
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.port = cls.server.server_address[1]
        handle, cls.token_file = tempfile.mkstemp()
        os.write(handle, (TOKEN + "\n").encode())
        os.close(handle)

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        os.remove(cls.token_file)

    def setUp(self):
        self.server.requests.clear()
        self.server.scenario = {"body": lambda: table(100)}

    def run_plugin(self, *argv, defaults=True, env=None, port=None):
        command = [sys.executable, PLUGIN]
        if defaults:
            command += ["-H", "127.0.0.1", "-p", str(port or self.port), "-f", self.token_file, "-o", "0123abcd",
                        "--query", 'from(bucket: "sensors") |> range(start: -1d) |> last()']
        environ = {key: value for key, value in os.environ.items() if not key.startswith("INFLUX_")}
        environ.update(env or {})
        return subprocess.run(command + list(argv), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              universal_newlines=True, env=environ, timeout=60)

    def expect(self, state, pattern, *argv, **kwargs):
        result = self.run_plugin(*argv, **kwargs)
        self.assertEqual(state, result.returncode, result)
        self.assertRegex(result.stdout, pattern)
        self.assertEqual(1, len(result.stdout.splitlines()), result)
        return result

    # the age against the thresholds

    def test_ok(self):
        self.expect(0, r"^INFLUXDB DATA AGE OK - newest entry is 1m 40s old \(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d UTC\) "
                       r"\| age=(99\.9|100|100\.1)s;600;1800$")
        request = self.server.requests[0]
        self.assertEqual("/api/v2/query", request["path"])
        self.assertEqual({"orgID": ["0123abcd"]}, request["params"])
        self.assertEqual('from(bucket: "sensors") |> range(start: -1d) |> last()', request["query"])
        self.assertEqual("application/vnd.flux", request["headers"]["Content-Type"])

    def test_warning_is_reachable(self):
        # 700s with the defaults -w 10m -c 30m used to be CRITICAL
        self.server.scenario = {"body": lambda: table(700)}
        self.expect(1, r"^INFLUXDB DATA AGE WARNING - newest entry is 11m 40s old")

    def test_critical(self):
        self.server.scenario = {"body": lambda: table(2000)}
        self.expect(2, r"^INFLUXDB DATA AGE CRITICAL - newest entry is 33m 20s old")

    def test_thresholds_with_units_and_ranges(self):
        self.server.scenario = {"body": lambda: table(700)}
        self.expect(0, r"\| age=[0-9.]+s;3600;7200$", "-w", "1h", "-c", "2h")
        self.expect(2, r"^INFLUXDB DATA AGE CRITICAL - .* \| age=[0-9.]+s;60;300$", "-w", "60", "-c", "300")
        self.expect(1, r"^INFLUXDB DATA AGE WARNING - .* \| age=[0-9.]+s;@600:800;3600$", "-w", "@10m:800", "-c", "1h")

    def test_newest_over_rows(self):
        self.server.scenario = {"body": lambda: table(5000, 30, 900)}
        self.expect(0, r"newest entry is 30s old")

    def test_several_tables(self):
        # every table has its own header, with _time in a different column, separated by blank lines
        body = lambda: (table(5000) + "\r\n" + ",result,table,_time,_value\r\n,_result,1,%s,7\r\n" % stamp(42)
                        + "\r\n" + table(900, extra="other"))
        self.server.scenario = {"body": body}
        self.expect(0, r"newest entry is 42s old")

    def test_annotations_are_skipped(self):
        body = lambda: ("#datatype,string,long,dateTime:RFC3339,double\r\n#group,false,false,false,false\r\n"
                        "#default,_result,,,\r\n,result,table,_time,_value\r\n,,0,%s,1\r\n" % stamp(50))
        self.server.scenario = {"body": body}
        self.expect(0, r"newest entry is 50s old")

    def test_timestamp_formats(self):
        for digits, zone in ((0, "Z"), (3, "Z"), (9, "+02:00"), (6, "-05:00")):
            self.server.scenario = {"body": lambda d=digits, z=zone: ",result,table,_time\r\n,,0,%s\r\n" % stamp(80, d, z)}
            # whole seconds lose the fraction, so the age may be just above 80s
            self.expect(0, r"newest entry is 1m 2[01]s old")

    def test_future_timestamp_is_reported(self):
        # the clock of the writer is off: WARNING by default
        self.server.scenario = {"body": lambda: table(-120)}
        self.expect(1, r"^INFLUXDB DATA AGE WARNING - newest entry is 2m in the future \(.* UTC\), see "
                       r"--allow-timestamps-in-future \| age=-1[12][0-9](\.[0-9])?s;600;1800$")

    def test_future_timestamp_allowed(self):
        self.server.scenario = {"body": lambda: table(-120)}
        self.expect(0, r"^INFLUXDB DATA AGE OK - newest entry is 2m in the future \(.* UTC\) \| age=-1",
                    "--allow-timestamps-in-future")
        self.expect(0, r"^INFLUXDB DATA AGE OK - newest entry is 2m in the future", "--allow-timestamps-in-future",
                    "5m")
        # the option takes no value when another option follows it
        self.expect(0, r"^INFLUXDB DATA AGE OK - ", "--allow-timestamps-in-future", "-w", "10m")

    def test_future_timestamp_beyond_the_allowed_offset(self):
        self.server.scenario = {"body": lambda: table(-120)}
        self.expect(1, r"^INFLUXDB DATA AGE WARNING - newest entry is 2m in the future \(.* UTC\), more than the "
                       r"allowed 30s \|", "--allow-timestamps-in-future", "30s")

    def test_allowed_future_counts_as_current(self):
        # thresholds apply to old data only, so a strict -w does not trigger on an allowed future entry
        self.server.scenario = {"body": lambda: table(-5)}
        self.expect(0, r"^INFLUXDB DATA AGE OK - newest entry is 5s in the future", "--allow-timestamps-in-future",
                    "10s", "-w", "1", "-c", "2")

    def test_no_time_column(self):
        self.server.scenario = {"body": ",result,table,_value\r\n,_result,0,1\r\n"}
        self.expect(3, r"^INFLUXDB DATA AGE UNKNOWN - the query result has no _time column$")

    def test_unparsable_time(self):
        self.server.scenario = {"body": ",result,table,_time\r\n,_result,0,yesterday\r\n"}
        self.expect(3, r"^INFLUXDB DATA AGE UNKNOWN - cannot parse the _time value 'yesterday'$")

    def test_empty_answer(self):
        # no data within the range() of the query: the data is stale
        self.server.scenario = {"body": "\r\n"}
        self.expect(2, r"^INFLUXDB DATA AGE CRITICAL - the query returned no data$")
        self.expect(1, r"^INFLUXDB DATA AGE WARNING - the query returned no data$", "--empty-state", "warning")

    # errors

    def test_token_rejected(self):
        with tempfile.NamedTemporaryFile("w") as handle:
            handle.write("wrong")
            handle.flush()
            self.expect(3, r"^INFLUXDB DATA AGE UNKNOWN - InfluxDB rejected the token \(HTTP 401\): unauthorized access$",
                        "-f", handle.name)

    def test_bad_query(self):
        self.server.scenario = {"status": 400, "message": "error @1:1-1:5: undefined identifier frm"}
        self.expect(3, r"^INFLUXDB DATA AGE UNKNOWN - InfluxDB answered with HTTP 400: error @1:1-1:5: "
                       r"undefined identifier frm$")

    def test_server_error_is_critical(self):
        self.server.scenario = {"status": 503, "message": "service unavailable"}
        self.expect(2, r"^INFLUXDB DATA AGE CRITICAL - InfluxDB answered with HTTP 503: service unavailable$")

    def test_unreachable_is_critical(self):
        self.expect(2, r"^INFLUXDB DATA AGE CRITICAL - cannot query 127.0.0.1:1: ", port=1)

    def test_timeout_is_critical(self):
        self.server.scenario = {"body": lambda: table(10), "delay": 3}
        self.expect(2, r"^INFLUXDB DATA AGE CRITICAL - no answer from 127.0.0.1:\d+ within 1s$", "-T", "1")

    def test_ssl_alias(self):
        # the old option -ssl still selects https; the fake speaks plain http, so the TLS handshake fails
        self.expect(2, r"^INFLUXDB DATA AGE CRITICAL - cannot query 127.0.0.1:\d+: ", "-ssl", "-T", "3")

    # token, organization and verbose output

    def test_token_sources(self):
        base = ["-H", "127.0.0.1", "-p", str(self.port), "-o", "x", "--query", "q"]
        self.assertEqual(0, self.run_plugin(*base, "-t", TOKEN, defaults=False).returncode)
        self.assertEqual(0, self.run_plugin(*base, defaults=False, env={"INFLUX_TOKEN": TOKEN}).returncode)
        self.assertEqual(0, self.run_plugin(*base, defaults=False, env={"INFLUX_TOKEN_FILE": self.token_file}).returncode)
        result = self.run_plugin(*base, defaults=False)
        self.assertEqual(3, result.returncode, result)
        self.assertEqual("INFLUXDB DATA AGE UNKNOWN - no API token given, use -f/--token-file\n", result.stdout)
        result = self.run_plugin(*base, "-f", "/nonexistent/token", defaults=False)
        self.assertEqual(3, result.returncode, result)
        self.assertIn("cannot read token file /nonexistent/token: No such file or directory", result.stdout)

    def test_org_name(self):
        base = ["-H", "127.0.0.1", "-p", str(self.port), "-f", self.token_file, "--query", "q"]
        self.assertEqual(0, self.run_plugin(*base, "--org", "my org", defaults=False).returncode)
        self.assertEqual({"org": ["my org"]}, self.server.requests[-1]["params"])

    def test_verbose_hides_the_token(self):
        result = self.expect(0, r"^INFLUXDB DATA AGE OK - ", "-v")
        self.assertIn("Authorization: Token ***", result.stderr)
        self.assertNotIn(TOKEN, result.stderr + result.stdout)

    # command line

    def test_usage_errors_are_unknown(self):
        base = ["-H", "127.0.0.1", "-p", str(self.port)]
        for argv in ([], base + ["-o", "x"], base + ["--query", "q"], base + ["-o", "x", "--query", "q", "-t", "a", "-f", "b"],
                     base + ["-o", "x", "--org", "y", "--query", "q"], base + ["-o", "x", "--query", "q", "-p", "abc"]):
            result = self.run_plugin(*argv, defaults=False)
            self.assertEqual(3, result.returncode, (argv, result))
            self.assertIn("check_influxdb_data_age: error: ", result.stderr)

    def test_invalid_values_are_unknown(self):
        self.expect(3, r"^INFLUXDB DATA AGE UNKNOWN - invalid threshold 'soon'$", "-w", "soon")
        self.expect(3, r"^INFLUXDB DATA AGE UNKNOWN - invalid threshold '2h:1h': start is above end$", "-c", "2h:1h")
        self.expect(3, r"^INFLUXDB DATA AGE UNKNOWN - -T must be above 0$", "-T", "0")
        for value in ("soon", "-5s"):
            result = self.run_plugin("--allow-timestamps-in-future=" + value)
            self.assertEqual(3, result.returncode, (value, result))
            self.assertIn("argument --allow-timestamps-in-future: ", result.stderr)

    def test_expanded_tilde_gets_a_hint(self):
        # what bash passes for an unquoted -w ~:10m
        self.expect(3, r"^INFLUXDB DATA AGE UNKNOWN - invalid threshold '/home/nagios:10m' \(did the shell expand an "
                       r"unquoted ~\? Quote the range: '~:\.\.\.'\)$", "-w", "/home/nagios:10m")

    def test_version(self):
        result = self.run_plugin("--version", defaults=False)
        self.assertEqual(0, result.returncode, result)
        self.assertRegex(result.stdout, r"^check_influxdb_data_age \d+\.\d+$")


if __name__ == "__main__":
    unittest.main()
