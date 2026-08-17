"""
geoip.py — country lookup for the Visitor Log, with no license key and no build-time dependency.

WHY DB-IP AND NOT MAXMIND: GeoLite2 needs a MaxMind account + license key = another secret + another
"one-time human step". DB-IP's IP-to-Country Lite is the same MaxMind-DB (.mmdb) format, free, and
downloadable with no account. Licence: CC-BY-4.0 — attribution is required, so it is credited on the
/privacy page. Do not remove that credit.

The DB is fetched LAZILY into /data (the persistent colt_webdata volume) on first use and refreshed
monthly. So: docker build never depends on a download, and the file survives redeploys.

Country only — deliberately. City-level geolocation of visitors is a bigger privacy intrusion than
security monitoring needs, and under DSGVO you collect the minimum that does the job (Art. 5(1)(c)).
"""
import datetime, gzip, os, threading, urllib.request

DB_DIR  = os.environ.get("GEOIP_DIR", "/data")
ENABLED = os.environ.get("GEOIP_ENABLED", "1") != "0"
_lock   = threading.Lock()
_reader = None
_tried  = None          # YYYY-MM we last attempted, so a failure retries next month, not every request


def _path(ym):
    return os.path.join(DB_DIR, "dbip-country-lite-%s.mmdb" % ym)


def _download(ym):
    url = "https://download.db-ip.com/free/dbip-country-lite-%s.mmdb.gz" % ym
    dst = _path(ym)
    tmp = dst + ".tmp"
    req = urllib.request.Request(url, headers={"User-Agent": "colt-cybergod/1.0"})
    with urllib.request.urlopen(req, timeout=60) as r, open(tmp, "wb") as fh:
        fh.write(gzip.decompress(r.read()))
    os.replace(tmp, dst)
    for f in os.listdir(DB_DIR):                       # keep only the current month
        if f.startswith("dbip-country-lite-") and f != os.path.basename(dst):
            try: os.unlink(os.path.join(DB_DIR, f))
            except OSError: pass
    return dst


def _get_reader():
    global _reader, _tried
    if not ENABLED:
        return None
    ym = datetime.datetime.utcnow().strftime("%Y-%m")
    if _reader is not None and _tried == ym:
        return _reader
    with _lock:
        if _reader is not None and _tried == ym:
            return _reader
        _tried = ym
        try:
            import maxminddb
        except ImportError:
            return None
        p = _path(ym)
        try:
            if not os.path.exists(p):
                os.makedirs(DB_DIR, exist_ok=True)
                try:
                    _download(ym)
                except Exception:
                    # DB-IP publishes on the 1st; early in the month fall back to last month's file
                    prev = (datetime.date.today().replace(day=1) - datetime.timedelta(days=1)).strftime("%Y-%m")
                    p = _path(prev)
                    if not os.path.exists(p):
                        _download(prev)
            if _reader is not None:
                try: _reader.close()
                except Exception: pass
            _reader = maxminddb.open_database(p)
        except Exception as e:
            print('{"evt":"geoip","result":"error","err":"%s"}' % repr(e)[:140], flush=True)
            _reader = None
    return _reader


def country(ip):
    """ISO-3166 alpha-2, or '-' if unknown/private/unavailable. Never raises."""
    if not ip or ip.startswith(("10.", "192.168.", "127.", "172.16.", "h:")) or ip == "-":
        return "-"
    try:
        r = _get_reader()
        if not r:
            return "-"
        rec = r.get(ip) or {}
        return (rec.get("country", {}) or {}).get("iso_code") or "-"
    except Exception:
        return "-"
