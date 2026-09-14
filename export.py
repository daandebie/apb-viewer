#!/usr/bin/env python3
"""
Laag 4 — leesbare bestanden voor taalmodellen, op vaste adressen.

Schrijft uit data/apb.json dezelfde Markdown als de exportknop in de viewer, maar dan
automatisch en per vast bestand, zodat een taalmodel (Gemini) ze zelf kan openen:

  index.md                         overzicht met adressen, omvang en actualiteit (hier beginnen)
  {datum}.md                       hele dag
  {datum}/termijn-{eerste-id}.md   één termijn (bijv. eerste termijn Kamer)
  {datum}/beurt-{eerste-id}.md     één sprekersbeurt, inclusief interrupties

Bestandsnamen hangen aan het eerste fragment-ID en blijven dus stabiel. Elk bestand staat er
ook als .txt, voor tools die Markdown als download behandelen.

Gebruik:
  export.py --uitvoer _site/gemini --basis-url https://daandebie.github.io/apb-viewer/
  export.py --data _site/demo/data/apb.json --geen-status --uitvoer _site/demo/gemini --basis-url …/demo/
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

HERE = Path(__file__).resolve().parent
TZ = ZoneInfo("Europe/Amsterdam")
HANDELINGEN = "https://www.officielebekendmakingen.nl/"
MILD = {"Gecorrigeerd", "Gerectificeerd"}
TOKENS_PER_WOORD = 1.6


def laad_json(pad: Path, default):
    try:
        return json.loads(pad.read_text())
    except FileNotFoundError:
        return default


def getal(n: float) -> str:
    return f"{int(n):,}".replace(",", ".")


def tijd(iso: str | None) -> str:
    return iso[11:19] if iso else ""


def duur(s: int | None) -> str:
    if s is None:
        return "duur onbekend"
    if s < 60:
        return f"{s}s"
    u, m, sec = s // 3600, s % 3600 // 60, s % 60
    return f"{u}u{m:02d}m" if u else f"{m}m{sec:02d}s"


def streng(fragmenten: list[dict]) -> bool:
    # Alles wat niet expliciet gecorrigeerd is, telt als ongecorrigeerd.
    return not fragmenten or any(f["bron"].get("status") not in MILD for f in fragmenten)


def disclaimer_regels(fragmenten: list[dict]) -> list[str]:
    if streng(fragmenten):
        return ["⚠ ONGECORRIGEERD STENOGRAM — Tweede Kamer, Algemene Politieke Beschouwingen.",
                "Aan ongecorrigeerde verslagen kunnen geen rechten worden ontleend en er mag niet letterlijk uit worden geciteerd.",
                f"De definitieve, citeerbare tekst staat in de Handelingen: {HANDELINGEN}"]
    return ["⚠ GEEN OFFICIËLE PUBLICATIE — verslag Tweede Kamer uit het Gegevensmagazijn (status per fragment vermeld).",
            "Hieraan kunnen geen rechten worden ontleend; citeer niet letterlijk uit deze tekst.",
            f"De definitieve, citeerbare tekst staat in de Handelingen: {HANDELINGEN}"]


def wie(f: dict) -> str:
    if f["type"] == "procedure":
        return f"Procedure — {f.get('procedure_soort') or 'tekst'}"
    naam = f.get("naam") or "Onbekende spreker"
    if f["categorie"] == "fractie":
        return f"{naam} ({f['fractie']})"
    if f["categorie"] == "voorzitter":
        return f"{naam} (voorzitter)"
    return f"{naam} ({f.get('functie') or f['groep']})"


def status_regel(f: dict) -> str:
    delen = [f"{tijd(f['begin']) or '??:??:??'} – {tijd(f['eind']) if f.get('eind') else 'onbekend'}",
             duur(f.get("duur_s")), f["type"], (f["bron"].get("status") or "onbekend").upper(), f["bron"].get("soort") or ""]
    if "tekst" in f.get("wijzigingen", []):
        delen.append("TEKST GEWIJZIGD t.o.v. eerdere versie")
    return " · ".join(d for d in delen if d)


def alinea_tekst(f: dict) -> str:
    uit = []
    for a in f["alineas"]:
        if a.get("soort") == "lijstitem":
            uit.append("- " + a["tekst"])
        elif a.get("soort") in ("Motietekst", "Motieinfo"):
            uit.append("> " + a["tekst"])
        else:
            uit.append(a["tekst"])
    return "\n\n".join(uit)


def woorden(fragmenten: list[dict]) -> int:
    return sum(len(a["tekst"].split()) for f in fragmenten for a in f["alineas"])


class Exporteur:
    def __init__(self, apb: dict, status: dict, basis_url: str):
        self.apb = apb
        self.status = status
        self.basis_url = basis_url if basis_url.endswith("/") else basis_url + "/"
        self.byid = {f["id"]: f for f in apb["fragmenten"]}
        self.blok = {b["id"]: b for d in apb["dagen"] for soort in ("agendapunten", "termijnen", "beurten") for b in d[soort]}
        self.gemaakt = datetime.now(TZ)

    def viewer_link(self, fid: str) -> str:
        return f"{self.basis_url}#{fid}"

    def bestand(self, fragmenten: list[dict], omschrijving: str) -> str:
        dagen = [d for d in self.apb["dagen"] if any(f["dag"] == d["datum"] for f in fragmenten)]
        tel: dict[str, int] = {}
        for f in fragmenten:
            s = (f["bron"].get("status") or "onbekend").upper()
            tel[s] = tel.get(s, 0) + 1
        bron_ids = {f["bron"]["verslag_id"] for f in fragmenten}
        bronnen = [(d["datum"], b) for d in dagen for b in d["bronverslagen"] if b["verslag_id"] in bron_ids]
        eerder = sum(1 for d in dagen for b in d["bronverslagen"] if b["verslag_id"] not in bron_ids)
        eerste = fragmenten[0]["id"]
        disc = disclaimer_regels(fragmenten)
        L = [f"# Algemene Politieke Beschouwingen — {omschrijving}", "", f"> **{disc[0]}**", "",
             "# Over dit bestand", "",
             "- **Wat:** stenografische verslagen (VLOS) van de Tweede Kamer, opgehaald uit het Gegevensmagazijn van de Tweede Kamer en opgedeeld in fragmenten (één bijdrage van één spreker per fragment).",
             f"- **Dag(en):** {'; '.join(d['label'] + ' — ' + d['lang'] for d in dagen)}",
             f"- **Selectie:** {omschrijving} — {getal(len(fragmenten))} fragmenten",
             f"- **Actualiteit:** laatst gecontroleerd bij de Tweede Kamer {self.status.get('gecontroleerd_op') or 'onbekend'}; "
             f"nieuwste verslagversie binnengehaald {self.apb['meta'].get('laatst_opgehaald_op') or 'onbekend'}; het verslag loopt op dat moment tot "
             f"{' / '.join(tijd(d.get('laatste_markeertijd')) or '—' for d in dagen)} uur (tijdstip van het laatste fragment; tijdens een vergadering loopt de publicatie achter). "
             "Dit bestand wordt automatisch bijgewerkt; open het opnieuw voor de nieuwste stand.",
             f"- **Bestand gemaakt:** {self.gemaakt.isoformat(timespec='minutes')}",
             f"- **Status van de fragmenten:** {', '.join(f'{getal(v)} {k}' for k, v in tel.items())}",
             f"- **Terugzoeken:** elk fragment heeft een ID tussen vierkante haken, bijvoorbeeld [{eerste}]. Het origineel staat in de APB-viewer op {self.viewer_link(eerste)}.",
             "", "# Juridische status — lees dit eerst", ""]
        if streng(fragmenten):
            L += ["- Dit zijn ONGECORRIGEERDE stenogrammen. De Dienst Verslag en Redactie werkt ze nog bij; tekst, sprekertoewijzing en tijden kunnen nog veranderen.",
                  "- Aan ongecorrigeerde verslagen kunnen geen rechten worden ontleend.",
                  "- Er mag niet letterlijk uit worden geciteerd."]
        else:
            L += ["- Deze fragmenten hebben in het Gegevensmagazijn de status Gecorrigeerd, maar dit bestand is geen officiële publicatie.",
                  "- Hieraan kunnen geen rechten worden ontleend; citeer niet letterlijk uit dit bestand."]
        L += [f"- De definitieve, citeerbare tekst staat in de Handelingen op {HANDELINGEN}",
              "- De status staat bij elk afzonderlijk fragment, zodat die ook bij losse passages bekend blijft.",
              "", "# Instructie voor het taalmodel", "",
              "Je krijgt hieronder verslagtekst van de Tweede Kamer. Houd je bij elk antwoord over deze tekst aan deze regels:", "",
              "1. Gebruik deze tekst om te zoeken, samen te vatten en te oriënteren — niet als citeerbare bron.",
              "2. Citeer niet letterlijk. Geef standpunten en uitspraken in eigen woorden weer en zeg erbij dat het om een ongecorrigeerde weergave gaat.",
              f"3. Verwijs in je antwoord altijd naar het fragment-ID tussen vierkante haken, bijvoorbeeld [{eerste}], bij elke bewering die je op de tekst baseert, bij voorkeur als link: {self.viewer_link(eerste)}. Noem alleen ID's die in dit bestand voorkomen.",
              "4. Vermeld de status (bijv. ONGECORRIGEERD) van de fragmenten waarop je je baseert.",
              f"5. Wijs de gebruiker erop dat de definitieve, citeerbare tekst in de Handelingen op {HANDELINGEN} staat.",
              "6. Staat iets niet in deze fragmenten, zeg dat dan. Vul niet aan uit eigen kennis zonder dat expliciet te melden.",
              "", "# Bronverslagen", "",
              "| Dag | Verslag-ID | Soort | Status | Gepubliceerd (API, UTC) | Opgehaald |", "|---|---|---|---|---|---|"]
        L += [f"| {dag} | {b['verslag_id']} | {b['soort']} | {b['status']} | {b.get('api_gewijzigd_op') or ''} | {b.get('opgehaald_op') or ''} |" for dag, b in bronnen]
        if eerder:
            L += ["", f"Daarnaast {eerder} eerdere versie(s) in het archief; per fragment geldt de nieuwste versie."]
        L += ["", "# Opbouw van elk fragment", "",
              '    ## [fragment-ID] Spreker (fractie, functie of "voorzitter")',
              "    begintijd – eindtijd · duur · soort (hoofdtermijn / interruptie / voorzitter / procedure) · STATUS · publicatiesoort",
              "", "---"]
        dag = ap = tm = None
        for f in fragmenten:
            if f["dag"] != dag and len(dagen) > 1:
                d = next(x for x in dagen if x["datum"] == f["dag"])
                L += ["", f"# {d['label']} — {d['lang']}"]
                ap = tm = None
            dag = f["dag"]
            if f["agendapunt_id"] != ap or f["termijn_id"] != tm:
                ap, tm = f["agendapunt_id"], f["termijn_id"]
                a, t = self.blok.get(ap), self.blok.get(tm) if tm else None
                L += ["", f"# Agendapunt: {a['titel'] if a else 'Agendapunt'}{' — ' + t['titel'] if t else ''}"]
            L += ["", f"## [{f['id']}] {wie(f)}", status_regel(f), "", alinea_tekst(f)]
        L += ["", "---", "", f"Einde bestand. {' '.join(disc[1:])}"]
        return "\n".join(L) + "\n"


def schrijf(pad: Path, tekst: str) -> None:
    pad.parent.mkdir(parents=True, exist_ok=True)
    pad.write_text(tekst)
    pad.with_suffix(".txt").write_text(tekst)


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data", default=str(HERE / "data" / "apb.json"))
    ap.add_argument("--status", default=str(HERE / "data" / "ophaalstatus.json"))
    ap.add_argument("--geen-status", action="store_true", help="geen ophaalstatus gebruiken (demo)")
    ap.add_argument("--uitvoer", required=True)
    ap.add_argument("--basis-url", required=True, help="adres van de viewer, bijv. https://daandebie.github.io/apb-viewer/")
    args = ap.parse_args(argv)

    apb = laad_json(Path(args.data), None)
    if apb is None:
        sys.exit(f"{args.data} ontbreekt — draai eerst parse.py.")
    status = {} if args.geen_status else laad_json(Path(args.status), {})
    ex = Exporteur(apb, status, args.basis_url)
    uit = Path(args.uitvoer)
    if uit.exists():
        shutil.rmtree(uit)
    map_url = ex.basis_url + uit.name + "/"

    alle = apb["fragmenten"]
    disc = disclaimer_regels(alle)
    idx = ["# APB 2026 — leesbare verslagbestanden (index)", "", f"> **{disc[0]}** {' '.join(disc[1:])}", "",
           "Deze map bevat de verslagen van de Algemene Politieke Beschouwingen als leesbare tekst, automatisch bijgewerkt uit de APB-viewer.",
           f"Viewer: {ex.basis_url} — direct naar een fragment: {ex.basis_url}#<fragment-ID>", "",
           "## Actualiteit", ""]
    if status.get("gecontroleerd_op"):
        idx.append(f"- Laatst gecontroleerd bij de Tweede Kamer: {status['gecontroleerd_op']} ({'gelukt' if status.get('resultaat') == 'ok' else 'mislukt: ' + str(status.get('fout'))})")
    if apb["meta"].get("peilmoment"):
        idx.append(f"- Momentopname: stand van {apb['meta']['peilmoment']} (testdata).")
    idx.append(f"- Nieuwste verslagversie binnengehaald: {apb['meta'].get('laatst_opgehaald_op') or 'nog niets'}")
    idx.append(f"- Index gemaakt: {ex.gemaakt.isoformat(timespec='minutes')}")
    idx += ["", "## Hoe te gebruiken (voor taalmodellen)", "",
            "1. Kies het kleinste bestand dat de vraag dekt: een beurt voor één spreker, een termijn voor een deel van het debat, de hele dag alleen als het nodig is.",
            "2. Een hele dag kan meer dan 300.000 tokens zijn. Lukt het openen niet volledig, neem dan de termijn- of beurtbestanden.",
            "3. Elk bestand begint met de juridische status en een instructie; houd je daaraan. Verwijs naar fragment-ID's.",
            "4. Controleer bij 'Actualiteit' tot welk tijdstip het verslag loopt; latere delen staan er nog niet in.",
            "5. Elk bestand staat er ook als .txt (zelfde inhoud).", ""]
    aantal = 0
    for d in apb["dagen"]:
        fr = [f for f in alle if f["dag"] == d["datum"]]
        idx += [f"## {d['label']} — {d['lang']}", ""]
        if not fr:
            idx += ["Nog geen verslag beschikbaar. Zodra de Kamer een versie publiceert, verschijnen hier de bestanden.", ""]
            continue
        dag_pad = uit / f"{d['datum']}.md"
        schrijf(dag_pad, ex.bestand(fr, f"{d['label']} ({d['lang']}), hele dag"))
        aantal += 1
        w = woorden(fr)
        idx += [f"- **Hele dag:** {map_url}{dag_pad.name} — {getal(len(fr))} fragmenten, ~{getal(w)} woorden (~{getal(w * TOKENS_PER_WOORD)} tokens), "
                f"tekst t/m {tijd(d.get('laatste_markeertijd'))}", ""]
        for agp in d["agendapunten"]:
            termijnen = [t for t in d["termijnen"] if t["agendapunt"] == agp["id"] and t["fragmenten"]]
            beurten = [b for b in d["beurten"] if b["agendapunt"] == agp["id"] and b["soort"] == "Spreekbeurt" and not b["is_voorzitter"] and b["fragmenten"]]
            if not beurten:
                continue
            idx += [f"### {agp['titel']}", ""]
            for t in termijnen:
                tfr = [ex.byid[i] for i in t["fragmenten"] if i in ex.byid]
                t_pad = uit / d["datum"] / f"termijn-{tfr[0]['id']}.md"
                schrijf(t_pad, ex.bestand(tfr, f"{agp['titel']} — {t['titel']}"))
                aantal += 1
                tw = woorden(tfr)
                idx.append(f"- **{t['titel']}** ({tijd(t.get('begin'))[:5]}–{tijd(t.get('eind'))[:5]}): {map_url}{d['datum']}/{t_pad.name} — ~{getal(tw)} woorden (~{getal(tw * TOKENS_PER_WOORD)} tokens)")
                for b in (b for b in beurten if b["termijn"] == t["id"]):
                    bfr = [ex.byid[i] for i in b["fragmenten"] if i in ex.byid]
                    wat = b.get("fractie") or (b.get("spreker") or {}).get("functie") or ""
                    b_pad = uit / d["datum"] / f"beurt-{bfr[0]['id']}.md"
                    schrijf(b_pad, ex.bestand(bfr, f"beurt van {b.get('naam')}{' (' + wat + ')' if wat else ''}, {tijd(b.get('begin'))}–{tijd(b.get('eind'))}, incl. interrupties"))
                    aantal += 1
                    bw = woorden(bfr)
                    n_intr = sum(1 for f in bfr if f["type"] == "interruptie")
                    idx.append(f"  - {b.get('naam')} ({wat}) {tijd(b.get('begin'))[:5]}–{tijd(b.get('eind'))[:5]}, fragmenten {bfr[0]['id']} t/m {bfr[-1]['id']}, "
                               f"{n_intr} interrupties: {map_url}{d['datum']}/{b_pad.name} — ~{getal(bw)} woorden")
            idx.append("")
    schrijf(uit / "index.md", "\n".join(idx) + "\n")
    print(f"{uit}: index + {aantal} bestanden")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
