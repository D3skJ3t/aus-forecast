"""Scrape each forecast site for the locations in locations.json and write data/<id>.json.

Shape per file: {id, generated, sources: [{source, name, url, fetched, point?, days: [{date, tmax, tmin,
rain_min, rain_max, rain_text, chance, text}]}], observations: {...}, errors: {source: message}}.
A source that fails keeps its last good entry (its "fetched" time shows how old it is).

Runs are staggered: each run updates only the locations that are due (last updated DUE_MINUTES ago
or more), oldest first, at most PER_RUN of them. With a run every 30 minutes, every location refreshes
about every 3 hours without all the sites being hit at once. --all updates every location.
"""
import argparse, json, os, re, sys, time, traceback
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
import requests
from bs4 import BeautifulSoup

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOCATIONS = os.path.join(ROOT, "locations.json")
DATA = os.path.join(ROOT, "data")
PER_RUN = 6  # 31 locations over six half-hourly runs: about every 3 hours each
DUE_MINUTES = 165  # a little under 3 hours, so a late run doesn't push a location past its slot
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36")
HEADERS = {"User-Agent": UA, "Accept-Language": "en-AU,en;q=0.9",
           "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8"}


def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def num(v):
    """Number or None; whole floats become ints so the JSON reads cleanly."""
    if v is None or v == "":
        return None
    try:
        f = float(str(v).replace("%", "").strip())
    except ValueError:
        return None
    return int(f) if f.is_integer() else f


def rain_range(text):
    """'1-5mm', '1–5 mm', '5 - 10 mm' -> (1, 5); '< 1mm' -> (0, 1)."""
    if not text:
        return None, None
    t = text.replace("–", "-").replace("\xa0", " ")
    vals = [num(n) for n in re.findall(r"\d+(?:\.\d+)?", t)]
    if "<" in t and vals:
        return 0, vals[0]
    if len(vals) >= 2:
        return vals[0], vals[1]
    if len(vals) == 1:
        return vals[0], vals[0]
    return None, None


def clean(s):
    return re.sub(r"\s+", " ", s or "").strip()


def day(date, tmax=None, tmin=None, rain_text=None, rain_min=None, rain_max=None, chance=None, text=None):
    if rain_text is not None and rain_min is None and rain_max is None:
        rain_min, rain_max = rain_range(rain_text)
    return {"date": date, "tmax": num(tmax), "tmin": num(tmin), "rain_min": num(rain_min),
            "rain_max": num(rain_max), "rain_text": clean(rain_text) or None,
            "chance": num(chance), "text": clean(text) or None}


def get(url, headers=None, **kw):
    r = requests.get(url, headers={**HEADERS, **(headers or {})}, timeout=30, **kw)
    r.raise_for_status()
    return r


def compass(deg):
    if deg is None:
        return None
    return ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE", "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"][round(deg / 22.5) % 16]


def with_point(out, loc, place):
    """Name the site's own place when it isn't the location itself (e.g. Eurong for K'gari)."""
    p, n = clean(place).lower(), loc["name"].lower()
    if p and p not in n and n not in p:
        out["point"] = clean(place)
    return out


# ---- Bureau of Meteorology: JSON feed used by its own app ----
def scrape_bom(loc):
    url = f"https://api.weather.bom.gov.au/v1/locations/{loc['bom']}/forecasts/daily"
    tz = ZoneInfo(loc["tz"])
    days = []
    for d in get(url).json()["data"]:
        # Dates are local midnight expressed in UTC (e.g. 2026-10-26T14:00:00Z is 27 Oct in Brisbane)
        local = datetime.fromisoformat(d["date"].replace("Z", "+00:00")).astimezone(tz).date()
        amt = (d.get("rain") or {}).get("amount") or {}
        lo, hi = amt.get("min"), amt.get("max")
        txt = None
        if lo is not None or hi is not None:
            txt = f"{lo or 0}-{hi} mm" if hi is not None else f"{lo} mm"
        days.append(day(local.isoformat(), d.get("temp_max"), d.get("temp_min"), txt, lo, hi,
                        (d.get("rain") or {}).get("chance"), d.get("short_text")))
    return {"source": "bom", "name": "BOM official", "url": "https://www.bom.gov.au/", "days": days}


# ---- WillyWeather: plain HTML, temperatures on the main page, rain on the rainfall page ----
def scrape_willyweather(loc):
    url = f"https://www.willyweather.com.au/{loc['willyweather']}.html"
    main = BeautifulSoup(get(url).text, "html.parser")
    rain = BeautifulSoup(get(f"https://rainfall.willyweather.com.au/{loc['willyweather']}.html").text, "html.parser")
    chance = {}
    for li in rain.select("section.forecast li.day"):
        t = li.find("time")
        if not t:
            continue
        cv, ca = li.select_one(".chance-value"), li.select_one(".chance-amount")
        chance[t["datetime"]] = (cv.get_text() if cv else None, ca.get_text() if ca else None)
    days = []
    for li in main.select("section.forecast li.day"):
        t = li.find("time")
        if not t:
            continue
        mn, mx, pr = li.select_one("li.min"), li.select_one("li.max"), li.select_one(".short-precis")
        c, amt = chance.get(t["datetime"], (None, None))
        days.append(day(t["datetime"], mx.get("data-max") if mx else None, mn.get("data-min") if mn else None,
                        amt, chance=c, text=pr.get_text() if pr else None))
    if not days:
        raise ValueError("no forecast days found in page")
    h1 = main.select_one("article h1")
    place = h1.find(string=True, recursive=False) if h1 else ""
    return with_point({"source": "willyweather", "name": "WillyWeather", "url": url, "days": days}, loc, place)


# ---- Elders Weather: 7-day table in the server-rendered HTML ----
def elders_date(label, idx, today):
    label = clean(label)
    if idx == 0 or label.lower() == "today":
        return today
    try:
        d = datetime.strptime(f"{label} {today.year}", "%a %b %d %Y").date()
    except ValueError:
        return today + timedelta(days=idx)
    if d < today - timedelta(days=180):  # header crosses into the new year
        d = d.replace(year=today.year + 1)
    return d


def scrape_elders(loc):
    url = f"https://eldersweather.com.au/forecast/{loc['elders']}"
    soup = BeautifulSoup(get(url).text, "html.parser")
    title = soup.title.get_text() if soup.title else ""
    if "Australia" in title:  # unknown places fall back to a national page
        raise ValueError(f"wrong page served: {clean(title)[:80]}")
    table = None
    for t in soup.select("table"):
        heads = [clean(th.get_text()) for th in t.select("thead th")]
        if "Today" in heads:
            table = t
            break
    if table is None:
        raise ValueError("7-day table not found")
    heads = [clean(th.get_text()) for th in table.select("thead th")][1:]
    rows = {}
    for tr in table.select("tbody tr"):
        cells = tr.find_all("td")
        if not cells:
            continue
        label = clean(cells[0].get_text())
        vals = []
        for td in cells[1:]:
            img = td.find("img")
            vals.append(img.get("alt") if img and label == "Summary" else clean(td.get_text()))
        rows[label] = vals
    today = datetime.now(ZoneInfo(loc["tz"])).date()
    days = []
    for i, h in enumerate(heads):
        def col(k):
            v = rows.get(k) or []
            return v[i] if i < len(v) else None
        tmax = (col("Maximum") or "").replace("°C", "")
        tmin = (col("Minimum") or "").replace("°C", "")
        days.append(day(elders_date(h, i, today).isoformat(), tmax, tmin, col("Rain Amount"),
                        chance=col("Chance of Rain"), text=col("Summary")))
    return with_point({"source": "elders", "name": "Elders Weather", "url": url, "days": days},
                      loc, title.replace("Forecast - ", ""))


def capture_json(url, wanted):
    """Load url in headless Chromium and return the JSON bodies of responses whose URL satisfies wanted().
    Used for sites whose forecast only arrives as background requests the page itself signs."""
    from playwright.sync_api import sync_playwright

    found = []

    def on_resp(r):
        if wanted(r.url):
            try:
                found.append(r.json())
            except Exception:
                pass

    with sync_playwright() as p:
        browser = p.chromium.launch()
        try:
            ctx = browser.new_context(user_agent=UA, locale="en-AU", timezone_id="Australia/Brisbane",
                                      viewport={"width": 1366, "height": 900})
            page = ctx.new_page()
            page.on("response", on_resp)
            page.goto(url, wait_until="domcontentloaded", timeout=45000)
            for _ in range(30):  # up to 30 s for the first matching response
                if found:
                    break
                page.wait_for_timeout(1000)
            page.wait_for_timeout(5000)  # let any later matching responses arrive
        finally:
            browser.close()
    if not found:
        raise ValueError("expected background response never arrived")
    return found


# ---- Weatherzone: forecast arrives as background JSON, so load the page in a browser ----
def scrape_weatherzone(loc):
    url = f"https://www.weatherzone.com/au/{loc['weatherzone']}"
    found = capture_json(url, lambda u: "/weather/forecast/daily/TWCID/" in u and "start=" in u)
    # The page requests several date ranges from the same endpoint; keep the longest
    payload = max(found, key=lambda b: len(b.get("body") or []))
    place = next(iter(((payload.get("metadata") or {}).get("locations") or {}).values()), {}).get("name", "")
    days = [day(d["date"], d.get("temperatureMax"), d.get("temperatureMin"), d.get("precipitationAmountText"),
                chance=d.get("precipitationProbability"), text=d.get("precis"))
            for d in payload.get("body") or []]
    if not days:
        raise ValueError("forecast response had no days")
    # e.g. Archerfield for Oxley: the forecast point Weatherzone uses
    return with_point({"source": "weatherzone", "name": "Weatherzone", "url": url, "days": days}, loc, place)


def wu_unit(el):
    """Weather Underground's unit class on a value element: "m" metric, "e" imperial (US)."""
    return next((c for c in (el.get("class") or []) if c in ("m", "e", "h", "s")), None) if el else None


# ---- Weather Underground: 10-day cards in the plain HTML, found by coordinates ----
def scrape_wunderground(loc):
    slug, expect = loc["wunderground"]
    url = f"https://www.wunderground.com/forecast/au/{slug}/{loc['lat']},{loc['lon']}"
    # Accepting JSON gets a different page (Brisbane, other layout), so ask for HTML only
    # It occasionally serves its Brisbane page, or the right place in a layout without the day cards,
    # so retry a few times before giving up
    for attempt in range(4):
        html = get(url, headers={"Accept": "text/html,application/xhtml+xml,*/*;q=0.8"}).text
        soup = BeautifulSoup(html, "html.parser")
        title = soup.title.get_text() if soup.title else ""
        if title.startswith(expect + ",") and soup.select_one("a.day-link"):
            break
        time.sleep(3)
    else:
        raise ValueError(f"wrong page served: {clean(title)[:80]}")
    days = []
    for a in soup.select("a.day-link"):
        m = re.search(r"/date/(\d{4}-\d{2}-\d{2})", a.get("href", ""))
        sunrise = a.select_one(".sunrise[data-time]")
        date = m.group(1) if m else (sunrise["data-time"][:10] if sunrise else None)
        if not date:
            continue
        hi, lo = a.select_one(".high"), a.select_one(".low")
        cond, qpf = a.select_one(".condition"), a.select_one(".precip")
        # Units follow the visitor's location: GitHub's US servers get Fahrenheit and inches. The low
        # and rain elements carry a unit class ("m" metric, "e" imperial); the high has none.
        t_unit, r_unit = wu_unit(lo), wu_unit(qpf)
        if t_unit not in ("m", "e") or (qpf and r_unit not in ("m", "e")):
            raise ValueError(f"unknown units: low {lo.get('class') if lo else None}, rain {qpf.get('class') if qpf else None}")
        to_c = (lambda v: None if v is None else round((v - 32) * 5 / 9, 1)) if t_unit == "e" else (lambda v: v)
        # "--" for today's high once the day has passed its peak; num() turns that into None
        tmax = to_c(num(hi.get_text().replace("°", ""))) if hi else None
        tmin = to_c(num(lo.get_text().replace("°", ""))) if lo else None
        mm = num(qpf.get_text()) if qpf else None
        if mm is not None and r_unit == "e":
            mm = round(mm * 25.4, 1)
        for v in (tmax, tmin):
            if v is not None and not -10 <= v <= 50:
                raise ValueError(f"implausible temperature {v} for {date} after unit handling")
        days.append(day(date, tmax, tmin, None if mm is None else f"{mm:g} mm", mm, mm,
                        text=cond.get_text() if cond else None))
    if not days:
        raise ValueError("no forecast cards found in page")
    return with_point({"source": "wunderground", "name": "Weather Underground", "url": url, "days": days}, loc, expect)


# ---- Windy: point forecast arrives as background JSON (signed by the page), temperatures in Kelvin ----
def scrape_windy(loc):
    url = f"https://www.windy.com/{loc['lat']}/{loc['lon']}"
    found = capture_json(url, lambda u: "/forecast/point/" in u)
    payload = max(found, key=lambda b: len(b.get("summary") or []))
    header = payload.get("header") or {}
    offset = timedelta(hours=header.get("utcOffset", 10))
    k2c = lambda k: None if k is None else round(k - 273.15, 1)
    data = payload.get("data") or {}
    rain, steps, wind = {}, {}, {}
    n = len(data.get("ts") or [])
    col = lambda k: (data.get(k) or [None] * n)
    for ts, mm, w, g, wd in zip(data.get("ts") or [], col("precipAmount"), col("wind"), col("windGust"), col("windDir")):
        d = (datetime.fromtimestamp(ts / 1000, timezone.utc) + offset).date().isoformat()
        rain[d] = rain.get(d, 0) + (mm or 0)
        steps[d] = steps.get(d, 0) + 1
        # Wind and gusts come in metres per second; keep each day's strongest, in km/h
        if w is not None:
            cur = wind.setdefault(d, {"wind_max": 0, "gust_max": 0, "wind_dir": None})
            if w * 3.6 > cur["wind_max"]:
                cur["wind_max"], cur["wind_dir"] = round(w * 3.6), compass(wd)
            if g is not None:
                cur["gust_max"] = max(cur["gust_max"], round(g * 3.6))
    days = []
    for s in payload.get("summary") or []:
        d = (datetime.fromtimestamp(s["timestamp"] / 1000, timezone.utc) + offset).date().isoformat()
        # Only total rain for days the 3-hourly series fully covers; the last day is often cut short
        full = steps.get(d, 0) >= s.get("segments", 8)
        mm = round(rain[d], 1) if full else None
        days.append({**day(d, k2c(s.get("tempMax")), k2c(s.get("tempMin")),
                           None if mm is None else f"{mm:g} mm", mm, mm), **(wind.get(d, {}) if full else {})})
    if not days:
        raise ValueError("forecast response had no daily summary")
    out = {"source": "windy", "name": "Windy", "url": url, "days": days}
    if header.get("model"):
        out["point"] = f"{header['model']} model"
    return out


# ---- Bureau of Meteorology latest observations from the station nearest the location ----
def scrape_observations(loc):
    j = get(f"https://api.weather.bom.gov.au/v1/locations/{loc['bom']}/observations").json()
    d, st = j.get("data") or {}, (j.get("data") or {}).get("station") or {}
    if d.get("temp") is None:
        raise ValueError("no current reading")
    t = (j.get("metadata") or {}).get("observation_time")
    when = datetime.fromisoformat(t.replace("Z", "+00:00")).astimezone(ZoneInfo(loc["tz"])).isoformat() if t else None
    return {"source": "bom_obs", "station": st.get("name"), "distance_km": num(round(st["distance"] / 1000, 1)) if st.get("distance") else None,
            "time": when, "temp": num(d.get("temp")), "feels_like": num(d.get("temp_feels_like")),
            "humidity": num(d.get("humidity")), "wind_dir": (d.get("wind") or {}).get("direction"),
            "wind_kmh": num((d.get("wind") or {}).get("speed_kilometre")), "gust_kmh": num((d.get("gust") or {}).get("speed_kilometre")),
            "rain_since_9am": num(d.get("rain_since_9am"))}


# ---- Boating: tides and swell from WillyWeather, for coastal locations only ----
def ww_graph(html, key):
    """Hourly points of one WillyWeather forecast graph (e.g. swell-height), or [] if the page has none."""
    i = html.find('{"forecastGraphs"')
    if i < 0:
        return []
    graphs, _ = json.JSONDecoder().raw_decode(html[i:])
    series = ((graphs.get("forecastGraphs") or {}).get(key) or {}).get("dataConfig", {}).get("series", {})
    return [p for g in series.get("groups") or [] for p in g.get("points") or []]


def scrape_marine(loc):
    if loc.get("water") != "coast":
        return None  # lakes and inland places: wind comes from the Bureau and Windy, no tides or swell
    tz = ZoneInfo(loc["tz"])
    path = loc["willyweather"]
    tides = {}
    soup = BeautifulSoup(get(f"https://tides.willyweather.com.au/{path}.html").text, "html.parser")
    for li in soup.select("section.forecast li.day"):
        t = li.find("time")
        if not t:
            continue
        tides[t["datetime"]] = [{"type": "high" if "point-high" in (p.get("class") or []) else "low",
                                 "time": clean(p.find("h3").get_text()), "height_m": num(clean(p.find("span").get_text()).replace(" m", ""))}
                                for p in li.select("li.point-high, li.point-low") if p.find("h3") and p.find("span")]
    swell = {}
    html = get(f"https://swell.willyweather.com.au/{path}.html").text
    heights, periods = ww_graph(html, "swell-height"), ww_graph(html, "swell-period")
    period_at = {p["x"]: p.get("y") for p in periods}
    for p in heights:
        if p.get("y") is None:
            continue
        d = datetime.fromtimestamp(p["x"], timezone.utc).astimezone(tz).date().isoformat()
        cur = swell.get(d)
        if cur is None or p["y"] > cur["max_m"]:
            swell[d] = {"max_m": p["y"], "dir": p.get("directionText"), "period_s": num(round(period_at.get(p["x"]) or 0)) or None}
    if not tides and not swell:
        raise ValueError("no tide or swell forecast on WillyWeather for this place")
    return {"source": "willyweather", "url": f"https://tides.willyweather.com.au/{path}.html", "tides": tides, "swell": swell}


SCRAPERS = [scrape_bom, scrape_willyweather, scrape_elders, scrape_weatherzone, scrape_wunderground,
            scrape_windy]
EXTRAS = [scrape_observations, scrape_marine]  # written as top-level "observations" and "marine"


def masked(e):
    return f"{type(e).__name__}: {e}"[:300]


def scrape_location(loc):
    out = os.path.join(DATA, f"{loc['id']}.json")
    try:
        prev = json.load(open(out, encoding="utf-8"))
    except (OSError, ValueError):
        prev = {}
    old = {s["source"]: s for s in prev.get("sources", [])}
    sources, errors, ran = [], {}, 0
    result = {}
    for fn in EXTRAS:
        key = fn.__name__.replace("scrape_", "")
        try:
            r = fn(loc)
            if r is None:
                continue  # doesn't apply to this location
            ran += 1
            result[key] = {**r, "fetched": now_iso()}
            print(f"  {key}: ok")
        except Exception as e:
            ran += 1
            errors[key] = masked(e)
            traceback.print_exc()
            if prev.get(key):
                result[key] = prev[key]
            print(f"  {key}: FAILED ({errors[key]})", file=sys.stderr)
    for fn in SCRAPERS:
        key = fn.__name__.replace("scrape_", "")
        ran += 1
        try:
            s = fn(loc)
            s["fetched"] = now_iso()
            sources.append(s)
            print(f"  {key}: {len(s['days'])} days")
        except Exception as e:
            errors[key] = masked(e)
            traceback.print_exc()
            # Keep the last good entry, unless it is itself bad (e.g. saved before a unit fix)
            if key in old and all(-10 <= d.get("tmax", 0) <= 50 for d in old[key].get("days", [])
                                  if isinstance(d.get("tmax"), (int, float))):
                sources.append(old[key])
            print(f"  {key}: FAILED ({errors[key]})", file=sys.stderr)
    os.makedirs(DATA, exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        json.dump({"id": loc["id"], "generated": now_iso(), "sources": sources, **result, "errors": errors},
                  f, indent=1, ensure_ascii=False)
        f.write("\n")
    return len(errors) < ran


def last_updated(loc):
    try:
        g = json.load(open(os.path.join(DATA, f"{loc['id']}.json"), encoding="utf-8"))["generated"]
        return datetime.fromisoformat(g.replace("Z", "+00:00"))
    except (OSError, ValueError, KeyError):
        return datetime.min.replace(tzinfo=timezone.utc)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--all", action="store_true", help="update every location now")
    ap.add_argument("--only", help="update just this location id")
    args = ap.parse_args()
    locs = json.load(open(LOCATIONS, encoding="utf-8"))
    if args.only:
        todo = [l for l in locs if l["id"] == args.only]
    elif args.all:
        todo = locs
    else:
        cutoff = datetime.now(timezone.utc) - timedelta(minutes=DUE_MINUTES)
        due = sorted((l for l in locs if last_updated(l) <= cutoff), key=last_updated)
        todo = due[:PER_RUN]
    if not todo:
        print("Nothing due")
        return 0
    ok = 0
    for loc in todo:
        print(f"{loc['id']}:")
        ok += scrape_location(loc)
    # Fail the step only if every location came back empty, so partial results still get committed
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
