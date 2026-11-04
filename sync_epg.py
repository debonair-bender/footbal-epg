"""Publish a football EPG catalogue. Never fetch or publish playlist/stream URLs."""
import argparse
import base64
import gzip
import hashlib
import io
import json
import os
import re
import sys
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent
UTC = timezone.utc
MAX_BYTES = 80 * 1024 * 1024


def read_json(path, default=None):
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default


def write_text(path, value):
    if path.exists() and path.read_text(encoding="utf-8") == value:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(value, encoding="utf-8")
    temporary.replace(path)


def write_json(path, value):
    write_text(path, json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def stamp(value):
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def key(value):
    plain = unicodedata.normalize("NFKD", value.casefold().replace("ø", "o"))
    plain = "".join(c for c in plain if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]+", " ", plain).strip()


def contains(text, phrase):
    return has_phrase(key(text), key(phrase))


def has_phrase(normalized_text, normalized_phrase):
    return bool(normalized_phrase) and f" {normalized_phrase} " in f" {normalized_text} "


def xml_time(value):
    # XMLTV offsets are authoritative; never assume the machine's timezone.
    if not re.fullmatch(r"\d{14}\s+[+-]\d{4}", value or ""):
        raise ValueError("XMLTV timestamp without a valid explicit UTC offset")
    return datetime.strptime(value, "%Y%m%d%H%M%S %z").astimezone(UTC)


def fetch(url, token=None):
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "https" or parsed.username or parsed.password:
        raise ValueError("Only HTTPS sources without embedded credentials are supported")
    headers = {"User-Agent": "FootballEPGCatalog/1.0", "Accept": "*/*"}
    if token:
        if parsed.hostname != "api.github.com":
            raise ValueError("Calendar credentials are restricted to api.github.com")
        headers["Authorization"] = f"Bearer {token}"
        headers["Accept"] = "application/vnd.github+json"

    class SafeRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, hdrs, newurl):
            # The public EPG redirects to a versioned path on its own host.
            # Authenticated calendar requests must never forward credentials.
            if token:
                return None
            target = urllib.parse.urlsplit(newurl)
            if target.scheme != "https" or target.hostname != parsed.hostname or target.username or target.password:
                raise ValueError("EPG redirect must stay on the same HTTPS host")
            return super().redirect_request(req, fp, code, msg, hdrs, newurl)

    opener = urllib.request.build_opener(SafeRedirect())
    with opener.open(urllib.request.Request(url, headers=headers), timeout=30) as response:
        payload = response.read(MAX_BYTES + 1)
        modified = response.headers.get("Last-Modified")
    if len(payload) > MAX_BYTES:
        raise ValueError("Source exceeds the download size limit")
    if payload.startswith(b"\x1f\x8b"):
        with gzip.GzipFile(fileobj=io.BytesIO(payload)) as compressed:
            payload = compressed.read(MAX_BYTES + 1)
        if len(payload) > MAX_BYTES:
            raise ValueError("Uncompressed source exceeds the size limit")
    return payload, modified


def channel_family(cid, names, config):
    if cid in config.get("channel_ids_exclude", []):
        return None
    if cid in config.get("channel_ids_include", []):
        return "custom"
    values = [cid.rsplit(".", 1)[0], *names]
    compact = [key(re.sub(r"^IT\s*-\s*", "", v, flags=re.I)).replace(" ", "") for v in values]
    for family in config["channel_families"]:
        for prefix in family["prefixes"]:
            if any(v.startswith(key(prefix).replace(" ", "")) for v in compact):
                return family["id"]
    return None


def programme_type(title, description, live=False, previously_shown=False):
    text = key(title + " " + description)
    if previously_shown or any(has_phrase(text, p) for p in ("replica", "differita", "replay", "rerun")):
        return "replay"
    if live or any(has_phrase(text, p) for p in ("diretta", "live")):
        return "live"
    return "unknown"


def window(now, config):
    zone = ZoneInfo(config["timezone"])
    first = now.astimezone(zone).date()
    start = datetime.combine(first, datetime.min.time(), zone).astimezone(UTC)
    end = datetime.combine(first + timedelta(days=config["days_ahead"] + 1), datetime.min.time(), zone).astimezone(UTC)
    return start, end


def parse_epg(payload, config, source, now, channel_aliases):
    if b"<!DOCTYPE" in payload.upper() or b"<!ENTITY" in payload.upper():
        raise ValueError("XML DTD/entities are not supported")
    lower, upper = window(now, config)
    category_keys = {key(v) for v in config["football_categories"]}
    football_keys = [key(v) for v in config["football_keywords"]]
    excluded_title_keys = [key(v) for v in config["non_match_title_keywords"]]
    competition_keys = [(c["id"], [key(v) for v in c["keywords"]]) for c in config["competitions"]]
    channels, programmes = {}, {}
    root = ET.fromstring(payload)
    if root.tag != "tv":
        raise ValueError("Expected an XMLTV <tv> document")
    for element in root.findall("channel"):
        cid = element.get("id", "")
        names = [el.text for el in element.findall("display-name") if el.text]
        family = channel_family(cid, names, config)
        if cid and family:
            channels[cid] = {"id": cid, "names": names, "family": family, "source": source,
                             "aliases": channel_aliases.get(cid, [])}
    invalid = 0
    for element in root.findall("programme"):
        cid = element.get("channel")
        if cid not in channels:
            continue
        try:
            start, end = xml_time(element.get("start")), xml_time(element.get("stop"))
            if end <= start:
                raise ValueError("Invalid programme interval")
        except (TypeError, ValueError):
            invalid += 1
            continue
        if start >= upper or end <= lower:
            continue
        title = " / ".join(el.text for el in element.findall("title") if el.text)
        subtitle = " / ".join(el.text for el in element.findall("sub-title") if el.text)
        description = " / ".join(el.text for el in element.findall("desc") if el.text)
        categories = [el.text for el in element.findall("category") if el.text]
        text = " ".join([title, subtitle, description])
        normalized_text = key(text)
        if not any(key(c) in category_keys for c in categories) and not any(has_phrase(normalized_text, v) for v in football_keys):
            continue
        competitions = [cid for cid, terms in competition_keys if any(has_phrase(normalized_text, term) for term in terms)]
        pid = hashlib.sha256(f"{source}|{cid}|{stamp(start)}|{stamp(end)}|{title}".encode()).hexdigest()[:24]
        programmes[pid] = {
            "id": pid, "channelId": cid, "source": source, "startUtc": stamp(start), "endUtc": stamp(end),
            "title": title, "subtitle": subtitle, "description": description, "categories": categories,
            "competitions": competitions,
            "broadcastType": programme_type(title, description, element.find("live") is not None, element.find("previously-shown") is not None),
            "matchEligible": not any(has_phrase(key(title), v) for v in excluded_title_keys),
        }
    return sorted(channels.values(), key=lambda c: c["id"]), sorted(programmes.values(), key=lambda p: (p["startUtc"], p["channelId"], p["id"])), invalid


def team_names(name, aliases):
    normalized = key(name)
    result = {normalized}
    for canonical, values in aliases.items():
        group = {key(canonical), *(key(v) for v in values)}
        if normalized in group:
            result.update(group)
    for value in list(result):
        short = re.sub(r"\b(?:fc|ac|as|ssc|cf|fk|club|\d{4})\b", " ", value)
        short = " ".join(short.split())
        if short:
            result.add(short)
    return result


def parse_calendar(document, slug, now, config):
    if not isinstance(document, dict) or not isinstance(document.get("matches"), list):
        raise ValueError("Calendar must contain a matches list")
    lower, upper = window(now, config)
    events = {}
    for row in document["matches"]:
        if not isinstance(row, dict) or not all(row.get(k) for k in ("id", "homeTeam", "awayTeam")):
            continue
        try:
            if isinstance(row.get("timestamp"), (int, float)):
                start = datetime.fromtimestamp(row["timestamp"], UTC)
            else:
                start = datetime.strptime(f"{row['date']} {row['time']}", "%Y-%m-%d %H:%M").replace(tzinfo=UTC)
        except (KeyError, TypeError, ValueError, OverflowError, OSError):
            continue
        if not lower <= start < upper:
            continue
        fid = f"{slug}:{row['id']}"
        events[fid] = {"fixtureId": fid, "calendarId": str(row["id"]), "competition": slug,
                       "homeTeam": row["homeTeam"], "awayTeam": row["awayTeam"], "kickoffUtc": stamp(start)}
    return list(events.values())


def associate(programmes, fixtures, aliases, config):
    result = {f["fixtureId"]: dict(f, broadcasts=[]) for f in fixtures}
    names = {f["fixtureId"]: (team_names(f["homeTeam"], aliases), team_names(f["awayTeam"], aliases)) for f in fixtures}
    competition_config = {c["id"]: c for c in config["competitions"]}
    competition_excludes = {cid: [key(v) for v in comp.get("excluded_keywords", [])] for cid, comp in competition_config.items()}
    before = timedelta(minutes=config["matching"]["minutes_before_kickoff"])
    after = timedelta(minutes=config["matching"]["minutes_after_kickoff"])
    for programme in programmes:
        if programme["broadcastType"] == "replay" or not programme["matchEligible"]:
            continue
        start = datetime.fromisoformat(programme["startUtc"].replace("Z", "+00:00"))
        end = datetime.fromisoformat(programme["endUtc"].replace("Z", "+00:00"))
        title = key(programme["title"] + " " + programme["subtitle"])
        text = key(title + " " + programme["description"])
        candidates = []
        for fixture in fixtures:
            kickoff = datetime.fromisoformat(fixture["kickoffUtc"].replace("Z", "+00:00"))
            if not kickoff - before <= start <= kickoff + after or end <= kickoff:
                continue
            if programme["competitions"] and fixture["competition"] not in programme["competitions"]:
                continue
            if any(has_phrase(text, v) for v in competition_excludes[fixture["competition"]]):
                continue
            home, away = names[fixture["fixtureId"]]
            if not any(has_phrase(text, h) for h in home) or not any(has_phrase(text, a) for a in away):
                continue
            strong = any(has_phrase(title, h) for h in home) and any(has_phrase(title, a) for a in away)
            candidates.append((fixture["fixtureId"], "high" if strong else "medium"))
        # A programme mentioning several simultaneous matches is ambiguous.
        if len(candidates) == 1:
            fid, confidence = candidates[0]
            result[fid]["broadcasts"].append({
                "programmeId": programme["id"], "channelId": programme["channelId"],
                "startUtc": programme["startUtc"], "endUtc": programme["endUtc"],
                "broadcastType": programme["broadcastType"], "confidence": confidence,
                "evidence": "teams_in_title_and_time" if confidence == "high" else "teams_in_description_and_time",
            })
    return sorted(result.values(), key=lambda f: (f["kickoffUtc"], f["fixtureId"]))


def load_calendars(config, now, calendar_dir=None):
    settings = config["calendars"]
    if not settings["enabled"] and calendar_dir is None:
        return [], {"state": "disabled", "message": "EPG catalogue active; calendar associations not configured"}
    token = os.environ.get(settings["token_env"])
    local = now.astimezone(ZoneInfo(config["timezone"])).date()
    last = local + timedelta(days=config["days_ahead"])
    seasons = {f"{d.year if d.month >= 7 else d.year - 1}-{d.year + 1 if d.month >= 7 else d.year}" for d in (local, last)}
    fixtures = {}
    for comp in config["competitions"]:
        for season in sorted(seasons):
            if calendar_dir is not None:
                document = read_json(calendar_dir / f"{comp['id']}.json")
            else:
                repository = settings["repository"]
                if not re.fullmatch(r"[\w.-]+/[\w.-]+", repository):
                    raise ValueError("Invalid calendar repository")
                path = f"{settings['directory']}/{comp['id']}/{season}.json"
                url = f"https://api.github.com/repos/{repository}/contents/{urllib.parse.quote(path, safe='/')}?ref={urllib.parse.quote(settings['ref'], safe='')}"
                raw, _ = fetch(url, token)
                document = json.loads(raw)
                if document.get("encoding") == "base64":
                    document = json.loads(base64.b64decode(document["content"]))
            for fixture in parse_calendar(document, comp["id"], now, config):
                fixtures[fixture["fixtureId"]] = fixture
    return list(fixtures.values()), {"state": "ok", "fixtures": len(fixtures)}


def markdown(programmes, channels, matches, now, config, calendar_status):
    zone = ZoneInfo(config["timezone"])
    def local(value):
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(zone).strftime("%d/%m %H:%M")
    def cell(value):
        return str(value).replace("|", "\\|").replace("\n", " ").replace("\r", " ").replace("<", "&lt;").replace(">", "&gt;")
    names = {c["id"]: (c["names"] or [c["id"]])[0] for c in channels}
    lines = ["# Programmazione calcio", "", f"Ultima acquisizione riuscita: {stamp(now)}. Orari: {config['timezone']}.", "",
             "Solo Sky Calcio, Sky Sport, DAZN/Zona DAZN, TV8 e RAI secondo config.json.", "",
             "Consultare data/status.json per l'esito dell'ultimo tentativo e l'eventuale stato obsoleto.", "",
             f"Calendari: {calendar_status['state']}. Il catalogo EPG funziona anche senza collegare i calendari.", "",
             "La voce 'unknown' indica che la guida non specifica diretta o replica.", "",
             "## Associazioni alle partite", "", "| Calcio d'inizio | Partita | Competizione | Canali associati |", "| --- | --- | --- | --- |"]
    for fixture in matches:
        broadcast = "; ".join(f"{names.get(b['channelId'], b['channelId'])} ({b['broadcastType']}, {b['confidence']})" for b in fixture["broadcasts"])
        lines.append("| " + " | ".join(map(cell, (local(fixture["kickoffUtc"]), f"{fixture['homeTeam']} - {fixture['awayTeam']}", fixture["competition"], broadcast or "Nessuna associazione"))) + " |")
    if not matches:
        lines += ["", "Nessuna associazione disponibile. La programmazione EPG è elencata sotto."]
    lines += ["", "## Catalogo EPG filtrato", "", "| Inizio | Fine | Canale | Programma | Tipo | Competizioni rilevate |", "| --- | --- | --- | --- | --- | --- |"]
    for p in programmes:
        lines.append("| " + " | ".join(map(cell, (local(p["startUtc"]), local(p["endUtc"]), names.get(p["channelId"], p["channelId"]), p["title"], p["broadcastType"], ", ".join(p["competitions"])))) + " |")
    return "\n".join(lines) + "\n"


def run(config_path, output, now, calendar_dir=None, downloader=fetch):
    config = read_json(config_path)
    channel_aliases = read_json(config_path.parent / "aliases/channels.json", {})
    team_aliases = read_json(config_path.parent / "aliases/teams.json", {})
    data = output / "data"
    status = {"schemaVersion": 1, "checkedAt": stamp(now), "state": "ok", "sources": [], "errors": []}
    channels, programmes = {}, {}
    try:
        for source in config["sources"]:
            payload, modified = downloader(source["url"])
            selected, rows, invalid = parse_epg(payload, config, source["id"], now, channel_aliases)
            if not selected or not rows:
                raise ValueError(f"{source['id']}: no followed channels or football programmes; retaining last good catalogue")
            channels.update({c["id"]: c for c in selected})
            programmes.update({p["id"]: p for p in rows})
            status["sources"].append({"id": source["id"], "url": source["url"], "lastModified": modified,
                                       "sha256": hashlib.sha256(payload).hexdigest(), "channels": len(selected),
                                       "programmes": len(rows), "invalidProgrammes": invalid})
    except (OSError, ValueError, ET.ParseError) as exc:
        status.update(state="error", errors=[str(exc)])
        write_json(data / "status.json", status)
        print(f"EPG acquisition failed: {exc}", file=sys.stderr)
        return 1
    channels = sorted(channels.values(), key=lambda c: c["id"])
    programmes = sorted(programmes.values(), key=lambda p: (p["startUtc"], p["channelId"], p["id"]))
    calendar_status = {"state": "error"}
    matches = []
    try:
        fixtures, calendar_status = load_calendars(config, now, calendar_dir)
        matches = associate(programmes, fixtures, team_aliases, config)
    except (OSError, ValueError, KeyError) as exc:
        status["state"] = "partial"
        status["errors"].append(f"Calendar fetch failed: {exc}")
        # Keep the prior association file with its original timestamp. Consumers
        # must use status.json/calendarState to detect that it was not refreshed.
    status["calendarState"] = calendar_status["state"]
    common = {"schemaVersion": 1, "updatedAt": stamp(now), "timezone": config["timezone"]}
    write_json(data / "channels.json", dict(common, channels=channels))
    write_json(data / "programmes.json", dict(common, programmes=programmes))
    if calendar_status["state"] != "error":
        write_json(data / "matches.json", dict(common, calendarState=calendar_status["state"], matches=matches))
    write_text(output / "PROGRAMMAZIONE.md", markdown(programmes, channels, matches, now, config, calendar_status))
    write_json(data / "status.json", status)
    print(f"Published {len(channels)} followed channels, {len(programmes)} football programmes, {sum(bool(f['broadcasts']) for f in matches)} matched fixtures; {status['state']}")
    return 1 if status["state"] == "partial" else 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=ROOT / "config.json")
    parser.add_argument("--output", type=Path, default=ROOT)
    parser.add_argument("--calendar-dir", type=Path, help="Optional local folder with <competition-id>.json calendars")
    args = parser.parse_args()
    return run(args.config, args.output, datetime.now(UTC), args.calendar_dir)


if __name__ == "__main__":
    sys.exit(main())
