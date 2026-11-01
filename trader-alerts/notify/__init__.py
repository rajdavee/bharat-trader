"""Local notifications for trader-alerts: a desktop pop-up and an optional webhook of your own. Standard library only.

OFF by default. Turn it on in trader/config.json:

    "notify": {"enabled": true, "desktop": true, "webhook": false,
               "quiet_hours": ["22:00", "07:00"], "min_gap_min": 5, "max_per_hour": 12,
               "events": {"cotrader_candidate": true, "autopilot_fill": true, ...}}

Channels
  desktop   macOS osascript, Linux notify-send, Windows a toast through PowerShell. Best effort: when the tool is
            missing nothing happens and the log says so.
  webhook   a generic JSON POST to the URL in the environment variable BT_NOTIFY_WEBHOOK (your own phone-push
            service, for example). The URL is never written to a file or a log. The body is only
            {"source", "event", "title", "text", "at"}: a short summary, at most 200 characters, with any value of a
            secret-looking environment variable and any long token-like string replaced by [redacted]. Never
            positions, quantities of the whole account, keys or phrases.

Rules, in order: the event's own toggle, quiet hours (nothing is sent; it is logged as suppressed), the per-event
gap (one notification of an event per min_gap_min minutes; the ones held back are counted into the next), the same
key once a day (one lock or one candidate is told once), and max_per_hour across all events.

Files: trader/notify/state.json (what was sent, for the limits) and trader/notify/log.jsonl (each decision; no URL).
Nothing here can place, change or approve an order. Education and tooling, not financial advice.
"""

import datetime
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request

IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))
SCHEMA = "bharat-trader/notify/1"
ENV_URL = "BT_NOTIFY_WEBHOOK"
MAX_TEXT = 200
EVENTS = {
    "cotrader_candidate": "the co-trader has new PROMOTED or WATCH candidates",
    "autopilot_fill": "an auto-pilot paper entry filled",
    "autopilot_exit": "an auto-pilot paper position closed",
    "copilot_proposal": "a LIVE co-pilot proposal is waiting for your approval",
    "risk_lock": "a circuit breaker locked new entries",
    "kill": "a kill switch went on",
    "decay_alert": "a strategy's forward record fell below its backtest's range",
    "capture_failure": "the intraday option-chain capture stopped or went stale",
    "test": "a test notification you asked for",
}
DEFAULTS = {
    "enabled": False,          # off by default: nothing is sent until you turn it on
    "desktop": True,           # once enabled: a desktop pop-up on this machine
    "webhook": False,          # once enabled, and BT_NOTIFY_WEBHOOK is set: a JSON POST to your own URL
    "quiet_hours": None,       # ["22:00", "07:00"] (IST, may wrap midnight): nothing is sent inside
    "min_gap_min": 5,          # per event: at most one notification each N minutes
    "max_per_hour": 12,        # all events together
    "events": {k: True for k in EVENTS},
    "timeout_s": 5,
}
SECRET_ENV = re.compile(r"(KEY|TOKEN|SECRET|PASSWORD|PASSWD|PIN|TOTP|AUTH|SESSION|COOKIE|WEBHOOK)", re.I)
TOKENISH = re.compile(r"(?=[A-Za-z0-9_+/=.]*\d)(?=[A-Za-z0-9_+/=.]*[A-Za-z])[A-Za-z0-9_+/=.]{20,}")  # keys, JWTs
HHMM = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")

RUN = None          # tests replace the desktop command runner: RUN(argv) -> anything
POST = None         # tests may replace the webhook call: POST(url, body_bytes, timeout) -> HTTP status
PLATFORM = sys.platform


class NotifyError(Exception):
    pass


# ---------------------------------------------------------------- files

def _read(path, default=None):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return default


def _write(path, doc):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=1, ensure_ascii=False)
        fh.write("\n")
    os.replace(tmp, path)


def paths(workdir):
    base = os.path.join(os.path.abspath(workdir), "trader")
    return {"config": os.path.join(base, "config.json"), "state": os.path.join(base, "notify", "state.json"),
            "log": os.path.join(base, "notify", "log.jsonl")}


def now_ist(now=None):
    if now is None:
        return datetime.datetime.now(IST).replace(tzinfo=None)
    return now.astimezone(IST).replace(tzinfo=None) if now.tzinfo else now


# ---------------------------------------------------------------- settings

def settings_of(cfg):
    """The "notify" block of trader/config.json with its defaults, validated (NotifyError on a bad value)."""
    raw = (cfg or {}).get("notify")
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise NotifyError("\"notify\" in trader/config.json must be an object")
    bad = sorted(set(raw) - set(DEFAULTS))
    if bad:
        raise NotifyError("unknown \"notify\" keys: %s (known: %s)" % (", ".join(bad), ", ".join(sorted(DEFAULTS))))
    s = json.loads(json.dumps(DEFAULTS))
    for k, v in raw.items():
        if k == "events":
            if not isinstance(v, dict) or set(v) - set(EVENTS) or not all(isinstance(x, bool) for x in v.values()):
                raise NotifyError("\"notify.events\" is {event: true|false} for: %s" % ", ".join(sorted(EVENTS)))
            s["events"].update(v)
        else:
            s[k] = v
    for k in ("enabled", "desktop", "webhook"):
        if not isinstance(s[k], bool):
            raise NotifyError("\"notify.%s\" is true or false" % k)
    q = s["quiet_hours"]
    if q is not None and not (isinstance(q, list) and len(q) == 2 and all(isinstance(x, str) and HHMM.match(x)
                                                                         for x in q)):
        raise NotifyError("\"notify.quiet_hours\" is [\"HH:MM\", \"HH:MM\"] (IST), e.g. [\"22:00\", \"07:00\"], or null")
    for k, lo, hi in (("min_gap_min", 0, 1440), ("max_per_hour", 1, 120), ("timeout_s", 1, 30)):
        v = s[k]
        if not isinstance(v, int) or isinstance(v, bool) or not lo <= v <= hi:
            raise NotifyError("\"notify.%s\" is a whole number from %d to %d" % (k, lo, hi))
    return s


def in_quiet_hours(q, now):
    if not q:
        return False
    t = now.strftime("%H:%M")
    a, b = q
    if a == b:
        return False
    return a <= t < b if a < b else (t >= a or t < b)


# ---------------------------------------------------------------- the words that may leave the machine

def redact(text, env=None):
    """Short, plain and safe: the value of every secret-looking environment variable (6+ characters) and any long
    token-like run of characters become [redacted]; control characters go; at most MAX_TEXT characters."""
    env = os.environ if env is None else env
    s = " ".join(str(text or "").split())
    for k, v in env.items():
        if v and len(v) >= 6 and SECRET_ENV.search(k) and v in s:
            s = s.replace(v, "[redacted]")
    s = TOKENISH.sub("[redacted]", s)
    s = "".join(ch for ch in s if ch.isprintable())
    return s if len(s) <= MAX_TEXT else s[:MAX_TEXT - 1].rstrip() + "…"


def payload(event, title, text, at, env=None):
    """The only body a webhook ever gets."""
    return {"source": "bharat-trader", "event": event, "title": redact(title, env)[:60], "text": redact(text, env),
            "at": at.strftime("%Y-%m-%dT%H:%M:%S+05:30")}


# ---------------------------------------------------------------- channels

def _ps_quote(s):
    return "'" + s.replace("'", "''") + "'"


def desktop_argv(title, text, platform=None):
    """The command that shows a desktop notification on this platform, or None when there is none."""
    platform = platform or PLATFORM
    clean = re.sub(r"[\"\\`$]", " ", text)
    ctitle = re.sub(r"[\"\\`$]", " ", title)
    if platform == "darwin":
        return ["osascript", "-e", "display notification \"%s\" with title \"%s\"" % (clean, ctitle)]
    if platform.startswith("linux"):
        return ["notify-send", "--app-name=bharat-trader", ctitle, clean]
    if platform.startswith("win"):
        script = ("[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = "
                  "WindowsRuntime] > $null; $t = [Windows.UI.Notifications.ToastNotificationManager]::"
                  "GetTemplateContent([Windows.UI.Notifications.ToastTemplateType]::ToastText02); "
                  "$x = $t.GetElementsByTagName('text'); $x.Item(0).AppendChild($t.CreateTextNode(%s)) > $null; "
                  "$x.Item(1).AppendChild($t.CreateTextNode(%s)) > $null; "
                  "[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('bharat-trader')"
                  ".Show([Windows.UI.Notifications.ToastNotification]::new($t))" % (_ps_quote(ctitle), _ps_quote(clean)))
        return ["powershell", "-NoProfile", "-NonInteractive", "-Command", script]
    return None


def send_desktop(title, text, timeout=5):
    argv = desktop_argv(title, text)
    if argv is None:
        return "no desktop notifier on %s" % PLATFORM
    run = RUN or (lambda a: subprocess.run(a, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                           stderr=subprocess.DEVNULL, timeout=timeout, check=False))
    try:
        run(argv)
    except (OSError, subprocess.SubprocessError, ValueError) as e:
        return "desktop notifier failed: %s" % type(e).__name__
    return "sent"


def webhook_url(env=None):
    env = os.environ if env is None else env
    url = (env.get(ENV_URL) or "").strip()
    if not url:
        return None, "%s is not set" % ENV_URL
    u = urllib.parse.urlparse(url)
    if u.scheme not in ("https", "http") or not u.netloc:
        return None, "%s is not an http(s) URL" % ENV_URL
    return url, None


def send_webhook(body, timeout=5, env=None):
    url, why = webhook_url(env)
    if not url:
        return why
    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    try:
        if POST is not None:
            status = POST(url, data, timeout)
        else:
            req = urllib.request.Request(url, data=data, method="POST", headers={
                "Content-Type": "application/json", "User-Agent": "bharat-trader-notify"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                status = r.status
    except urllib.error.HTTPError as e:
        e.close()
        return "webhook answered HTTP %s" % e.code
    except (urllib.error.URLError, OSError, ValueError) as e:
        return "webhook failed: %s" % type(e).__name__
    return "sent" if 200 <= int(status) < 300 else "webhook answered HTTP %s" % status


# ---------------------------------------------------------------- the decision

def decide(s, state, event, key, now):
    """(send?, why not). state is trader/notify/state.json."""
    if not s["enabled"]:
        return False, "notifications are off (\"notify\": {\"enabled\": true} turns them on)"
    if event != "test" and not s["events"].get(event, False):
        return False, "%s is switched off in notify.events" % event
    if event != "test" and in_quiet_hours(s["quiet_hours"], now):
        return False, "quiet hours %s to %s" % tuple(s["quiet_hours"])
    if not s["desktop"] and not s["webhook"]:
        return False, "no channel: desktop and webhook are both off"
    stamp = now.strftime("%Y-%m-%d %H:%M:%S")
    if key:
        seen = (state.get("keys") or {}).get(key)
        if seen and seen[:10] == stamp[:10]:
            return False, "already told today (%s)" % seen[11:16]
    if event != "test":
        last = (state.get("last") or {}).get(event)
        if last and s["min_gap_min"]:
            gap = (now - datetime.datetime.strptime(last, "%Y-%m-%d %H:%M:%S")).total_seconds() / 60.0
            if gap < s["min_gap_min"]:
                return False, "rate limit: one %s each %d minutes" % (event, s["min_gap_min"])
    hour_ago = (now - datetime.timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S")
    recent = [x for x in state.get("sent") or [] if x >= hour_ago]
    if len(recent) >= s["max_per_hour"]:
        return False, "rate limit: %d notifications in the last hour" % s["max_per_hour"]
    return True, ""


def _log(p, row):
    try:
        os.makedirs(os.path.dirname(p["log"]), exist_ok=True)
        with open(p["log"], "a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
    except OSError:
        pass


def send(workdir, event, title, text, now=None, key=None, cfg=None, env=None):
    """Notify one event, if the config allows it now. Never raises for a channel problem; returns
    {"sent": bool, "why", "channels": {...}, "payload"}. A bad "notify" block is NotifyError."""
    if event not in EVENTS:
        raise NotifyError("unknown event %r (known: %s)" % (event, ", ".join(sorted(EVENTS))))
    p = paths(workdir)
    cfg = _read(p["config"], {}) if cfg is None else cfg
    s = settings_of(cfg)
    now = now_ist(now)
    state = _read(p["state"], {}) or {}
    ok, why = decide(s, state, event, key, now)
    body = payload(event, title, text, now, env)
    out = {"sent": False, "event": event, "why": why, "channels": {}, "payload": body}
    if not ok and not s["enabled"]:
        return out                      # off: nothing is written either
    if not ok:
        held = state.setdefault("held", {})
        if s["enabled"] and why.startswith("rate limit"):
            held[event] = held.get(event, 0) + 1
            _write(p["state"], state)
        _log(p, {"at": body["at"], "event": event, "sent": False, "why": why})
        return out
    more = (state.get("held") or {}).pop(event, 0)
    if more:
        body["text"] = redact("%s (+%d more held back by the rate limit)" % (body["text"], more), env)
    if s["desktop"]:
        out["channels"]["desktop"] = send_desktop(body["title"], body["text"], s["timeout_s"])
    if s["webhook"]:
        out["channels"]["webhook"] = send_webhook(body, s["timeout_s"], env)
    out["sent"] = any(v == "sent" for v in out["channels"].values())
    out["why"] = "" if out["sent"] else "; ".join(out["channels"].values())
    stamp = now.strftime("%Y-%m-%d %H:%M:%S")
    if out["sent"]:
        state.setdefault("last", {})[event] = stamp
        hour_ago = (now - datetime.timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S")
        state["sent"] = [x for x in state.get("sent") or [] if x >= hour_ago] + [stamp]
        if key:
            keys = state.setdefault("keys", {})
            keys[key] = stamp
            if len(keys) > 500:
                for k in sorted(keys, key=keys.get)[:len(keys) - 500]:
                    del keys[k]
    _write(p["state"], state)
    _log(p, {"at": body["at"], "event": event, "sent": out["sent"], "why": out["why"], "channels": out["channels"],
             "title": body["title"], "text": body["text"]})
    return out


def test(workdir, now=None, channel=None, cfg=None, env=None):
    """A test notification through the configured channels (or one --channel, even while notifications are off:
    you asked for it). Returns the send() result."""
    p = paths(workdir)
    cfg = dict(_read(p["config"], {}) if cfg is None else cfg)
    if channel:
        if channel not in ("desktop", "webhook"):
            raise NotifyError("channel is desktop or webhook")
        n = dict(cfg.get("notify") or {})
        n.update({"enabled": True, "desktop": channel == "desktop", "webhook": channel == "webhook"})
        cfg["notify"] = n
    return send(workdir, "test", "bharat-trader test", "Test notification: if you can read this, %s works. "
                "Nothing was traded." % (channel or "this channel"), now=now, cfg=cfg, env=env)


def status(workdir, now=None, env=None):
    """The settings in force, whether the webhook URL is set (never the URL), and the last decisions."""
    p = paths(workdir)
    cfg = _read(p["config"], {}) or {}
    try:
        s, problem = settings_of(cfg), None
    except NotifyError as e:
        s, problem = None, str(e)
    url, why = webhook_url(env)
    rows = []
    try:
        with open(p["log"], encoding="utf-8") as fh:
            for line in fh.readlines()[-20:]:
                try:
                    rows.append(json.loads(line))
                except ValueError:
                    continue
    except OSError:
        pass
    host = urllib.parse.urlparse(url).hostname if url else None
    return {"settings": s, "problem": problem, "events": EVENTS, "webhook_set": bool(url),
            "webhook_host": host, "webhook_note": why, "platform": PLATFORM,
            "desktop_tool": (desktop_argv("t", "t") or [None])[0], "recent": rows}
