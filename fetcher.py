#!/usr/bin/env python3
"""
Laag 1 — fetcher: haalt de VLOS-verslagen van plenaire vergaderingen op uit het
Gegevensmagazijn van de Tweede Kamer en archiveert ze ONGEWIJZIGD in raw/.

Elke fetch krijgt een eigen bestand raw/{datum}_{verslag_id}_{ApiGewijzigdOp}.xml
en een regel in raw/manifest.json. Bestaande XML-bestanden worden nooit
overschreven: raw/ is het archief, alles daarboven wordt ervan afgeleid.

Gebruik:
  fetcher.py                      eenmalig ophalen voor de dagen uit config.json
  fetcher.py --dag 2026-06-03     andere dag(en), herhaalbaar
  fetcher.py --watch              elke 60 s pollen; draait parse.py na nieuwe data
  fetcher.py --watch --tot 23:59  stop automatisch op dat tijdstip (vandaag, of ISO-datumtijd)
  fetcher.py --status             toon wat er in het archief staat

Na elke ronde (ook zonder nieuwe versies, ook na een fout) staat de stand in
data/ophaalstatus.json; de viewer toont daaruit wanneer er voor het laatst is gecontroleerd.
Omgevingsvariabelen APB_BRON en APB_INTERVAL_MIN beschrijven wie er pollt (bijv. GitHub Actions).

Vriendelijk voor de API: één request tegelijk, minimaal 1 s tussen requests,
backoff bij fouten (Retry-After wordt gerespecteerd).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

HERE = Path(__file__).resolve().parent
RAW = HERE / "raw"
MANIFEST = RAW / "manifest.json"
CONFIG = HERE / "config.json"
LOG_PATH = HERE / "fetcher.log"
OPHAALSTATUS = HERE / "data" / "ophaalstatus.json"
TZ = ZoneInfo("Europe/Amsterdam")

VERSLAG_VELDEN = "Id,Soort,Status,ContentType,ContentLength,GewijzigdOp,ApiGewijzigdOp,Verwijderd,Vergadering_Id"
VERGADERING_VELDEN = "Id,Soort,Titel,Zaal,VergaderingNummer,Datum,Aanvangstijd,Sluiting,GewijzigdOp,ApiGewijzigdOp,Verwijderd"


def log(msg: str, alleen_scherm: bool = False) -> None:
    line = f"{datetime.now(TZ).strftime('%Y-%m-%d %H:%M:%S')} {msg}"
    print(line, flush=True)
    if not alleen_scherm:
        with LOG_PATH.open("a") as f:
            f.write(line + "\n")


def nu_iso() -> str:
    return datetime.now(TZ).isoformat(timespec="seconds")


class TijdelijkeFout(Exception):
    pass


class Api:
    MIN_GAP_S = 1.0
    RETRY_DELAYS_S = (5, 15, 45, 120)

    def __init__(self, basis: str, user_agent: str):
        self.basis = basis if basis.endswith("/") else basis + "/"
        self.ua = user_agent
        self._laatste = 0.0

    def _wacht_beurt(self) -> None:
        gap = time.monotonic() - self._laatste
        if gap < self.MIN_GAP_S:
            time.sleep(self.MIN_GAP_S - gap)
        self._laatste = time.monotonic()

    def get(self, url: str, accept: str) -> tuple[bytes, dict]:
        pogingen = len(self.RETRY_DELAYS_S) + 1
        for poging in range(pogingen):
            self._wacht_beurt()
            try:
                req = urllib.request.Request(url, headers={"User-Agent": self.ua, "Accept": accept})
                with urllib.request.urlopen(req, timeout=60) as r:
                    body = r.read()
                    headers = {k.lower(): v for k, v in r.headers.items()}
                verwacht = headers.get("content-length")
                if verwacht is not None and int(verwacht) != len(body):
                    raise TijdelijkeFout(f"onvolledige download: {len(body)} van {verwacht} bytes")
                return body, headers
            except urllib.error.HTTPError as e:
                # 4xx behalve 408/429 gaat niet vanzelf over; niet blijven hameren.
                if e.code < 500 and e.code not in (408, 429):
                    raise
                wacht = self._retry_after(e) or self._delay(poging)
                fout = f"HTTP {e.code}"
            except (urllib.error.URLError, TimeoutError, ConnectionError, TijdelijkeFout) as e:
                wacht = self._delay(poging)
                fout = str(e)
            if poging == pogingen - 1:
                raise TijdelijkeFout(f"{fout} na {pogingen} pogingen: {url}")
            log(f"FOUT {fout} — nieuwe poging over {wacht:.0f} s")
            time.sleep(wacht)
        raise AssertionError("onbereikbaar")

    def _delay(self, poging: int) -> float:
        return self.RETRY_DELAYS_S[poging] * random.uniform(0.8, 1.2)

    @staticmethod
    def _retry_after(e: urllib.error.HTTPError) -> float | None:
        waarde = e.headers.get("Retry-After") if e.headers else None
        try:
            return min(float(waarde), 900.0) if waarde else None
        except ValueError:
            return None

    def odata(self, pad: str, params: dict) -> list[dict]:
        # OData-systeemparameters ($filter, $expand) moeten letterlijk met $ in de URL.
        query = "&".join(f"{k}={urllib.parse.quote(v, safe=chr(39) + '(),=;$')}" for k, v in params.items())
        url = f"{self.basis}{pad}?{query}"
        items: list[dict] = []
        while url:
            body, _ = self.get(url, "application/json")
            data = json.loads(body)
            items.extend(data.get("value", []))
            url = data.get("@odata.nextLink")
        return items

    def resource(self, verslag_id: str) -> tuple[bytes, dict]:
        return self.get(f"{self.basis}verslag/{verslag_id}/resource", "text/xml")


def laad_json(pad: Path, default):
    try:
        return json.loads(pad.read_text())
    except FileNotFoundError:
        return default


def schrijf_atomisch(pad: Path, tekst: str) -> None:
    tmp = pad.with_name(pad.name + ".tmp")
    tmp.write_text(tekst)
    os.replace(tmp, pad)


def dag_grenzen(dag: str) -> tuple[str, str]:
    d = date.fromisoformat(dag)
    begin = datetime(d.year, d.month, d.day, tzinfo=TZ)
    eind = begin + timedelta(days=1)
    return begin.isoformat(), eind.isoformat()


def vergaderingen_voor(api: Api, dag: str) -> list[dict]:
    begin, eind = dag_grenzen(dag)
    return api.odata("Vergadering", {
        "$filter": f"Soort eq 'Plenair' and Datum ge {begin} and Datum lt {eind}",
        "$select": VERGADERING_VELDEN,
        "$expand": f"Verslag($select={VERSLAG_VELDEN})",
        "$orderby": "Aanvangstijd",
    })


def bestandsnaam(dag: str, verslag: dict) -> str:
    stempel = verslag["ApiGewijzigdOp"].replace(":", "-")
    return f"{dag}_{verslag['Id']}_{stempel}.xml"


def xml_ok(body: bytes) -> bool:
    try:
        ET.fromstring(body)
        return True
    except ET.ParseError:
        return False


class Archief:
    def __init__(self):
        RAW.mkdir(exist_ok=True)
        self.manifest = laad_json(MANIFEST, None) or {
            "beschrijving": "Fetch-log van ongewijzigd gearchiveerde VLOS-verslagen. Alleen aanvullen, nooit regels wijzigen.",
            "fetches": [],
        }
        self.bekend = {(f["verslag"]["Id"], f["verslag"]["ApiGewijzigdOp"]) for f in self.manifest["fetches"]}

    def heeft(self, verslag: dict) -> bool:
        return (verslag["Id"], verslag["ApiGewijzigdOp"]) in self.bekend

    def registreer(self, entry: dict) -> None:
        self.manifest["fetches"].append(entry)
        self.bekend.add((entry["verslag"]["Id"], entry["verslag"]["ApiGewijzigdOp"]))
        schrijf_atomisch(MANIFEST, json.dumps(self.manifest, ensure_ascii=False, indent=1))

    def bewaar(self, naam: str, body: bytes) -> None:
        # os.link faalt als het doel al bestaat (nooit overschrijven) en is atomair (nooit half bestand).
        tmp = RAW / (naam + ".part")
        tmp.write_bytes(body)
        try:
            os.link(tmp, RAW / naam)
        finally:
            tmp.unlink()


def entry_voor(dag: str, vergadering: dict, verslag: dict, naam: str, body: bytes, headers: dict | None, opmerking: str | None = None) -> dict:
    entry = {
        "bestand": naam,
        "opgehaald_op": nu_iso(),
        "datum": dag,
        "vergadering": {k: vergadering.get(k) for k in VERGADERING_VELDEN.split(",")},
        "verslag": {k: verslag.get(k) for k in VERSLAG_VELDEN.split(",")},
        "http": {
            "content_type": (headers or {}).get("content-type"),
            "content_length": (headers or {}).get("content-length"),
        },
        "bytes": len(body),
        "sha256": hashlib.sha256(body).hexdigest(),
        "xml_ok": xml_ok(body),
    }
    if opmerking:
        entry["opmerking"] = opmerking
    return entry


def seconden_iso(stempel: str | None) -> str | None:
    # Sub-seconden (7 decimalen in de API) weglaten: browsers lezen die niet betrouwbaar.
    return re.sub(r"\.\d+", "", stempel) if stempel else None


def cyclus(api: Api, archief: Archief, dagen: list[str], stil: bool = False, info: dict | None = None) -> int:
    """Eén ronde: vergaderingen + verslagen opvragen, nieuwe versies downloaden. Geeft aantal nieuwe bestanden."""
    nieuw = 0
    for dag in dagen:
        vergaderingen = vergaderingen_voor(api, dag)
        if info is not None:
            alle = [v for verg in vergaderingen for v in (verg.get("Verslag") or []) if not v.get("Verwijderd")]
            nieuwste = max(alle, key=lambda v: v["ApiGewijzigdOp"], default=None)
            info[dag] = {
                "vergadering_gevonden": bool(vergaderingen),
                "verslagversies": len(alle),
                "nieuwste_versie": None if nieuwste is None else {
                    "soort": nieuwste["Soort"], "status": nieuwste["Status"],
                    "gewijzigd_op": seconden_iso(nieuwste["GewijzigdOp"]),
                    "api_gewijzigd_op": seconden_iso(nieuwste["ApiGewijzigdOp"]),
                },
            }
        if not vergaderingen:
            log(f"{dag}: nog geen plenaire vergadering in het Gegevensmagazijn", alleen_scherm=stil)
            continue
        for verg in vergaderingen:
            verslagen = sorted(verg.get("Verslag") or [], key=lambda v: v["ApiGewijzigdOp"])
            if not verslagen:
                log(f"{dag}: '{verg['Titel']}' heeft nog geen verslag", alleen_scherm=stil)
            for vs in verslagen:
                if archief.heeft(vs):
                    continue
                naam = bestandsnaam(dag, vs)
                pad = RAW / naam
                if vs.get("Verwijderd"):
                    log(f"{dag}: verslag {vs['Id']} staat als verwijderd gemarkeerd — niet opgehaald")
                    archief.registreer({
                        "bestand": None, "opgehaald_op": nu_iso(), "datum": dag,
                        "vergadering": {k: verg.get(k) for k in VERGADERING_VELDEN.split(",")},
                        "verslag": {k: vs.get(k) for k in VERSLAG_VELDEN.split(",")},
                        "opmerking": "Verwijderd=true in API; geen bestand",
                    })
                    continue
                if pad.exists():
                    # Crash tussen wegschrijven en manifest-update: bestand staat er al, alleen registreren.
                    body = pad.read_bytes()
                    archief.registreer(entry_voor(dag, verg, vs, naam, body, None, "manifest-regel hersteld voor bestaand bestand"))
                    continue
                body, headers = api.resource(vs["Id"])
                if not xml_ok(body):
                    log(f"{dag}: verslag {vs['Id']} is geen geldige XML — tweede poging")
                    time.sleep(10)
                    body, headers = api.resource(vs["Id"])
                archief.bewaar(naam, body)
                entry = entry_voor(dag, verg, vs, naam, body, headers)
                archief.registreer(entry)
                nieuw += 1
                log(f"NIEUW {dag} {vs['Soort']}/{vs['Status']} {len(body):,} bytes -> raw/{naam}"
                    + ("" if entry["xml_ok"] else "  [LET OP: ongeldige XML]"))
    return nieuw


def schrijf_ophaalstatus(archief: Archief, cfg: dict, info: dict, nieuw: int, fout: str | None) -> None:
    vorige = laad_json(OPHAALSTATUS, {})
    opgehaald = [f["opgehaald_op"] for f in archief.manifest["fetches"] if f.get("bestand")]
    status = {
        "gecontroleerd_op": nu_iso(),
        "resultaat": "fout" if fout else "ok",
        "fout": fout,
        "nieuw_deze_ronde": nieuw,
        "laatste_nieuwe_versie_opgehaald_op": max(opgehaald) if opgehaald else None,
        # Bij een fout halverwege de ronde blijft de laatst bekende stand per dag staan.
        "dagen": {**vorige.get("dagen", {}), **info},
        "bron": os.environ.get("APB_BRON", "lokaal"),
        "interval_min": float(os.environ.get("APB_INTERVAL_MIN") or int(cfg.get("poll_interval_s", 60)) / 60),
    }
    OPHAALSTATUS.parent.mkdir(exist_ok=True)
    schrijf_atomisch(OPHAALSTATUS, json.dumps(status, ensure_ascii=False, indent=1))


def draai_parse(dagen: list[str]) -> None:
    cmd = [sys.executable, str(HERE / "parse.py")]
    for d in dagen:
        cmd += ["--dag", d]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode == 0:
        log("parse.py: " + (r.stdout.strip().splitlines() or ["ok"])[-1])
    else:
        log(f"FOUT parse.py (exit {r.returncode}): {r.stderr.strip()[-500:]}")


def stoptijd(waarde: str | None) -> datetime | None:
    if not waarde:
        return None
    if len(waarde) <= 5:
        uur, minuut = (int(x) for x in waarde.split(":"))
        return datetime.now(TZ).replace(hour=uur, minute=minuut, second=0, microsecond=0)
    t = datetime.fromisoformat(waarde)
    return t if t.tzinfo else t.replace(tzinfo=TZ)


def status() -> None:
    manifest = laad_json(MANIFEST, {"fetches": []})
    per_dag: dict[str, list] = {}
    for f in manifest["fetches"]:
        per_dag.setdefault(f["datum"], []).append(f)
    if not per_dag:
        print("Archief is leeg.")
    for dag in sorted(per_dag):
        print(f"\n{dag}")
        for f in per_dag[dag]:
            vs = f["verslag"]
            print(f"  {vs['ApiGewijzigdOp'][:19]}  {vs['Soort']:<16} {vs['Status']:<14} "
                  f"{f.get('bytes', 0):>10,} B  opgehaald {f['opgehaald_op'][:19]}  {f.get('bestand') or '-'}")


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dag", action="append", help="datum JJJJ-MM-DD (herhaalbaar); standaard de dagen uit config.json")
    ap.add_argument("--watch", action="store_true", help="blijf pollen")
    ap.add_argument("--tot", help="stop de watch-lus op HH:MM (vandaag) of op een ISO-datumtijd")
    ap.add_argument("--parse", action="store_true", help="draai parse.py na nieuwe data (standaard aan bij --watch)")
    ap.add_argument("--no-parse", action="store_true", help="geen parse.py na nieuwe data")
    ap.add_argument("--status", action="store_true", help="toon inhoud van het archief")
    args = ap.parse_args(argv)

    if args.status:
        status()
        return 0

    cfg = laad_json(CONFIG, {})
    dagen = args.dag or cfg["dagen"]
    api = Api(cfg["api_basis"], cfg["user_agent"])
    archief = Archief()
    parse = (args.parse or args.watch) and not args.no_parse

    if not args.watch:
        info: dict = {}
        nieuw, fout = 0, None
        try:
            nieuw = cyclus(api, archief, dagen, info=info)
        except (TijdelijkeFout, urllib.error.URLError, json.JSONDecodeError) as e:
            fout = str(e)
            log(f"FOUT: {fout}")
        schrijf_ophaalstatus(archief, cfg, info, nieuw, fout)
        log(f"klaar: {nieuw} nieuwe verslagversie(s) voor {', '.join(dagen)}")
        if parse and (nieuw or args.parse):
            draai_parse(dagen)
        return 1 if fout else 0

    interval = int(cfg.get("poll_interval_s", 60))
    einde = stoptijd(args.tot)
    log(f"watch gestart voor {', '.join(dagen)} (elke {interval} s{', tot ' + einde.strftime('%d-%m %H:%M') if einde else ''})")
    fouten_op_rij = 0
    eerste = True
    while True:
        info = {}
        try:
            nieuw = cyclus(api, archief, dagen, stil=not eerste, info=info)
            schrijf_ophaalstatus(archief, cfg, info, nieuw, None)
            fouten_op_rij = 0
            if nieuw and parse:
                draai_parse(dagen)
            elif not nieuw:
                log("geen nieuwe verslagversies", alleen_scherm=True)
            wacht = interval
        except (TijdelijkeFout, urllib.error.URLError, json.JSONDecodeError) as e:
            fouten_op_rij += 1
            schrijf_ophaalstatus(archief, cfg, info, 0, str(e))
            wacht = min(interval * 2 ** fouten_op_rij, 900)
            log(f"FOUT in pollronde ({fouten_op_rij}x op rij): {e} — volgende ronde over {wacht} s")
        except KeyboardInterrupt:
            log("watch gestopt (Ctrl+C)")
            return 0
        eerste = False
        if einde and datetime.now(TZ) + timedelta(seconds=wacht) > einde:
            log("watch gestopt: stoptijd bereikt")
            return 0
        try:
            time.sleep(wacht)
        except KeyboardInterrupt:
            log("watch gestopt (Ctrl+C)")
            return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
