"""Official iFinDPy boundary. No vendor names or credentials enter the engine."""
from __future__ import annotations

import contextlib
from datetime import datetime, timedelta
import importlib
import io
import json
import os
import time

from .models import D


class IFinDError(RuntimeError):
    """Safe error text: never include the vendor response or credentials."""


class IFinDConfigurationError(IFinDError):
    pass


class IFinDQueryError(IFinDError):
    def __init__(self, function, code):
        self.code = code if type(code) is int else None
        message = f"iFinD {function} failed (code={self.code})"
        if function == "login" and self.code == -1:
            message += (
                "; SDK reports generic operation failure. Check the same data-interface "
                "account in official SuperCommand, then verify this terminal's credentials "
                "and SDK network settings; see docs/IFIND.md."
            )
        super().__init__(message)


def credentials_present():
    return {name: bool(os.environ.get(name)) for name in ("IFIND_USERNAME", "IFIND_PASSWORD")}


def load_sdk():
    try:
        # Some official SDK versions print the .pth filename on import.
        with contextlib.redirect_stdout(io.StringIO()):
            return importlib.import_module("iFinDPy")
    except Exception:
        raise IFinDConfigurationError(
            "Cannot load iFinDPy. Install the official SDK into this Python environment; "
            "see docs/IFIND.md (matching Python/DLL architecture and iFinDPy.pth required)."
        ) from None


def _json(value):
    if isinstance(value, bytes):
        for encoding in ("utf-8-sig", "gb18030"):
            try:
                return json.loads(value.decode(encoding), parse_float=D)
            except (UnicodeError, ValueError):
                pass
        raise IFinDError("Malformed iFinD JSON response")
    if isinstance(value, str):
        try:
            return json.loads(value, parse_float=D)
        except ValueError:
            raise IFinDError("Malformed iFinD JSON response") from None
    return value


def decode_response(response, function):
    """Accept official THSData JSON or native dictionary responses (DateQuery)."""
    if hasattr(response, "errorcode"):
        if response.errorcode != 0:
            raise IFinDQueryError(function, response.errorcode)
        response = response.data
    payload = _json(response)
    if not isinstance(payload, dict) or "errorcode" not in payload:
        raise IFinDError("Expected iFinD response envelope with errorcode")
    if type(payload["errorcode"]) is not int or payload["errorcode"] != 0:
        raise IFinDQueryError(function, payload["errorcode"])
    if "tables" not in payload:
        raise IFinDError("iFinD response is missing tables")
    return payload


def records_from_payload(payload):
    """Expand per-security column arrays without truncating or cross-producting time."""
    tables = payload["tables"]
    if isinstance(tables, dict):
        times = tables.get("time")
        if not isinstance(times, list):
            raise IFinDError("Unsupported iFinD calendar response")
        return [{"time": stamp} for stamp in times]
    if not isinstance(tables, list):
        raise IFinDError("Expected iFinD tables list")
    records = []
    for block in tables:
        if not isinstance(block, dict) or not isinstance(block.get("table"), dict):
            raise IFinDError("Invalid iFinD table structure")
        columns = block["table"]
        if not columns:
            continue
        if any(not isinstance(values, list) for values in columns.values()):
            raise IFinDError("Expected iFinD column arrays")
        lengths = {len(values) for values in columns.values()}
        if len(lengths) != 1:
            raise IFinDError("iFinD table columns have inconsistent lengths")
        size = lengths.pop()
        stamps = block.get("time")
        if stamps is not None and (not isinstance(stamps, list) or len(stamps) != size):
            raise IFinDError("iFinD time and value arrays have inconsistent lengths")
        for index in range(size):
            row = {key: values[index] for key, values in columns.items()}
            if "thscode" in block:
                if "thscode" in row and row["thscode"] != block["thscode"]:
                    raise IFinDError("Conflicting iFinD security identifiers")
                row["thscode"] = block["thscode"]
            if stamps is not None:
                if "time" in row and row["time"] != stamps[index]:
                    raise IFinDError("Conflicting iFinD timestamps")
                row["time"] = stamps[index]
            records.append(row)
    return records


FUNCTIONS = {
    "THS_HQ": ("codes", "indicators", "parameters", "start", "end"),
    "THS_HF": ("codes", "indicators", "parameters", "start", "end"),
    "THS_BD": ("codes", "indicators", "parameters"),
    "THS_DS": ("codes", "indicators", "parameters", "global_parameters", "start", "end"),
    "THS_DP": ("name", "parameters", "options"),
    "THS_DateQuery": ("exchange", "parameters", "start", "end"),
}


def request_function(request):
    function = request.get("function")
    if function is None:
        function = {"1d": "THS_HQ", "1m": "THS_HF"}.get(request.get("frequency"))
    if function not in FUNCTIONS:
        raise IFinDConfigurationError("Unsupported iFinD function/frequency")
    for key in FUNCTIONS[function]:
        if key not in request or not isinstance(request[key], str):
            raise IFinDConfigurationError(f"iFinD request requires string field: {key}")
        if not request[key] and key not in {"parameters", "global_parameters", "options"}:
            raise IFinDConfigurationError(f"iFinD request field cannot be empty: {key}")
        if "REPLACE_" in request[key]:
            raise IFinDConfigurationError("Replace iFinD template placeholders before requesting data")
    return function


class IFinDSource:
    def __init__(self, sdk=None, *, retries=3, retry_codes=(), sleep=time.sleep):
        if type(retries) is not int or retries < 1:
            raise IFinDConfigurationError("retries must be a positive integer")
        self.sdk = sdk if sdk is not None else load_sdk()
        self.retries, self.retry_codes, self.sleep = retries, frozenset(retry_codes), sleep
        self.logged_in = False
        self.owns_session = False

    def login(self):
        if self.logged_in:
            return
        if not all(credentials_present().values()):
            raise IFinDConfigurationError("Set IFIND_USERNAME and IFIND_PASSWORD locally; never put credentials in task JSON")
        try:
            code = self.sdk.THS_iFinDLogin(os.environ["IFIND_USERNAME"], os.environ["IFIND_PASSWORD"])
        except Exception:
            raise IFinDError("iFinD SDK login call failed") from None
        # Official manual: -201 means this process is already logged in.
        if code not in (0, -201):
            raise IFinDQueryError("login", code)
        self.logged_in, self.owns_session = True, code == 0

    def close(self):
        owned = self.owns_session
        self.logged_in = self.owns_session = False
        if owned:
            try:
                code = self.sdk.THS_iFinDLogout()
            except Exception:
                raise IFinDError("iFinD SDK logout call failed") from None
            if code != 0:
                raise IFinDQueryError("logout", code)

    def __enter__(self):
        self.login()
        return self

    def __exit__(self, exc_type, *_):
        if exc_type:
            with contextlib.suppress(IFinDError):
                self.close()
        else:
            self.close()

    def query(self, request):
        function = request_function(request)
        if not self.logged_in:
            raise IFinDConfigurationError("Login before querying iFinD")
        args = [request[key] for key in FUNCTIONS[function]]
        if function != "THS_DateQuery":
            args.append("format:json")  # avoid SDK pandas dependency and float conversion
        for attempt in range(self.retries):
            try:
                response = getattr(self.sdk, function)(*args)
                payload = decode_response(response, function)
                return records_from_payload(payload)
            except IFinDQueryError as error:
                if error.code not in self.retry_codes or attempt + 1 == self.retries:
                    raise
            except (TimeoutError, ConnectionError):
                if attempt + 1 == self.retries:
                    raise IFinDError("iFinD transport failed after retries") from None
            except IFinDError:
                raise
            except Exception:
                raise IFinDError(f"iFinD SDK {function} call failed") from None
            self.sleep(min(2 ** attempt, 8))

    def fetch(self, request):
        return self.query(request)


def market_requests(request, batch_size=20, chunk_days=5):
    """Inclusive, non-overlapping vendor date/time ranges; no silent partial publication."""
    function = request_function(request)
    frequency = request.get("frequency")
    if function != {"1d": "THS_HQ", "1m": "THS_HF"}.get(frequency):
        raise IFinDConfigurationError("Market update requires matching 1d/THS_HQ or 1m/THS_HF")
    if type(batch_size) is not int or batch_size < 1 or type(chunk_days) is not int or chunk_days < 1:
        raise IFinDConfigurationError("batch_size and chunk_days must be positive integers")
    codes = [code.strip() for code in request["codes"].split(",")]
    if any(not c for c in codes) or len(set(codes)) != len(codes):
        raise IFinDConfigurationError("Security codes must be nonempty and unique")
    fmt = "%Y-%m-%d" if frequency == "1d" else "%Y-%m-%d %H:%M:%S"
    try:
        start, end = (datetime.strptime(request[k], fmt) for k in ("start", "end"))
    except ValueError:
        raise IFinDConfigurationError(f"Request dates must use {fmt}") from None
    if start > end:
        raise IFinDConfigurationError("Request start follows end")
    params = {}
    for item in request["parameters"].split(","):
        if not item.strip():
            continue
        key, sep, value = item.partition(":")
        if not sep or key.strip().lower() in params:
            raise IFinDConfigurationError("Invalid or duplicated market parameter")
        params[key.strip().lower()] = value.strip().lower()
    if params.get("cps") != ("1" if frequency == "1d" else "no"):
        raise IFinDConfigurationError("Market storage requires explicit unadjusted CPS")
    if params.get("fill") != ("omit" if frequency == "1d" else "original"):
        raise IFinDConfigurationError("Market storage forbids forward-filled synthetic bars")
    if params.get("interval") != ("d" if frequency == "1d" else "1") or "sampleinterval" in params:
        raise IFinDConfigurationError("Market storage requires explicit daily or 1-minute interval")
    tick = timedelta(days=1) if frequency == "1d" else timedelta(seconds=1)
    while start <= end:
        finish = min(start + timedelta(days=chunk_days) - tick, end)
        for index in range(0, len(codes), batch_size):
            yield dict(request, codes=",".join(codes[index:index + batch_size]), start=start.strftime(fmt), end=finish.strftime(fmt))
        start = finish + tick
