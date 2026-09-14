#!/usr/bin/env python3
"""
Samenvattingen per blok (beurt, termijn of agendapunt) — los van de rest van de pipeline.

Leest data/apb.json, laat een taalmodel een samenvatting schrijven en bewaart die in
data/samenvattingen.json, gekoppeld op blok-id én de fragment-ID's die erin zitten.
De viewer toont ze altijd boven de ruwe tekst met het label "samenvatting — geen bron".
Daarna wordt apb-offline.html opnieuw gebundeld.

Kiezen welke blokken:
  summarize.py --lijst [--dag 2026-09-16]          overzicht met fragment-ID's per blok
  summarize.py --beurt 2026-09-16-0012              beurt die dit fragment bevat (herhaalbaar)
  summarize.py --termijn 2026-09-16-0012            termijn die dit fragment bevat
  summarize.py --agendapunt 2026-09-16-0012         agendapunt dat dit fragment bevat
  summarize.py --alle-beurten --dag 2026-09-16      alle sprekersbeurten van die dag (niet de voorzitter)
  summarize.py --kies --dag 2026-09-16              interactief: nummers uit de lijst intypen

Opties:
  --opnieuw        ook blokken die al een actuele samenvatting hebben
  --droog          toon wat er zou gebeuren, roep geen model aan
  --backend api    Anthropic API via de Python-SDK (standaard; vereist `pip install anthropic`,
                   Python >= 3.10 en ANTHROPIC_API_KEY of `ant auth login`)
  --backend claude-code
                   via de lokale `claude -p` (Claude Code-abonnement, geen API-sleutel nodig)
  --model          standaard claude-opus-5

Een samenvatting heet verouderd als de tekst van een van haar fragmenten sindsdien is
gewijzigd (bijv. na correctie); zonder --opnieuw worden alleen ontbrekende en verouderde
samenvattingen gemaakt.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

HERE = Path(__file__).resolve().parent
DATA = HERE / "data"
APB = DATA / "apb.json"
SAMENVATTINGEN = DATA / "samenvattingen.json"
TZ = ZoneInfo("Europe/Amsterdam")
PROMPT_VERSIE = 1
STANDAARD_MODEL = "claude-opus-5"

SYSTEEM = """Je vat verslagen van debatten in de Tweede Kamer samen voor iemand die deze debatten beroepsmatig bestudeert. De invoer is een stenogram, meestal nog ongecorrigeerd. Je samenvatting wordt altijd naast de brontekst getoond met het label "samenvatting — geen bron"; ze dient om snel te oriënteren en de juiste passage terug te vinden.

Regels:
- Geef de inhoud neutraal en feitelijk weer: standpunten en argumenten, concrete voorstellen, vragen aan en toezeggingen van het kabinet, ingediende of aangehouden moties, en de kern van interrupties (wie vroeg wat, wat was het antwoord).
- Parafraseer. Citeer niet letterlijk, ook niet tussen aanhalingstekens.
- Zet achter elke bewering het fragment-ID waar die op steunt tussen vierkante haken, bijvoorbeeld [2026-09-16-0143]. Gebruik alleen ID's die in de invoer voorkomen.
- Geen eigen oordeel, geen politieke duiding, geen kennis van buiten de tekst.
- Is de brontekst onvolledig of onduidelijk (het verslag stopt bijvoorbeeld halverwege), zeg dat dan.
- Vorm: platte tekst. Eén openingszin over wie er spreekt en waarover, daarna regels die beginnen met "- ". Geen kopjes en geen andere opmaak."""

LENGTE = {
    "beurt": "circa 80 tot 300 woorden, naar verhouding van de lengte van de beurt",
    "termijn": "circa 250 tot 700 woorden; per spreker een eigen regel of korte reeks regels",
    "agendapunt": "circa 300 tot 900 woorden; per spreker een eigen regel of korte reeks regels",
}


def laad_json(pad: Path, default):
    try:
        return json.loads(pad.read_text())
    except FileNotFoundError:
        return default


def nu_iso() -> str:
    return datetime.now(TZ).isoformat(timespec="seconds")


def duur(s: int | None) -> str:
    if s is None:
        return ""
    if s < 60:
        return f"{s}s"
    u, m, sec = s // 3600, s % 3600 // 60, s % 60
    return f"{u}u{m:02d}m" if u else f"{m}m{sec:02d}s"


def wie(f: dict) -> str:
    if f["type"] == "procedure":
        return f"Procedure — {f.get('procedure_soort') or 'tekst'}"
    naam = f.get("naam") or "Onbekende spreker"
    if f["categorie"] == "fractie":
        return f"{naam} ({f['fractie']})"
    if f["categorie"] == "voorzitter":
        return f"{naam} (voorzitter)"
    return f"{naam} ({f.get('functie') or f['groep']})"


def fragment_als_tekst(f: dict) -> str:
    tijden = f"{f['begin'][11:19]} – {f['eind'][11:19]}" if f.get("eind") else (f.get("begin") or "")[11:19]
    kop = f"[{f['id']}] {wie(f)}\n{tijden} · {duur(f.get('duur_s'))} · {f['type']} · {f['bron']['status'].upper()}"
    return kop + "\n\n" + "\n\n".join(a["tekst"] for a in f["alineas"])


class Dataset:
    def __init__(self, apb: dict):
        self.apb = apb
        self.fragmenten = {f["id"]: f for f in apb["fragmenten"]}
        self.blokken: dict[str, dict] = {}
        for dag in apb["dagen"]:
            for soort, naam in (("agendapunten", "agendapunt"), ("termijnen", "termijn"), ("beurten", "beurt")):
                for b in dag[soort]:
                    self.blokken[b["id"]] = {**b, "_soort": naam}

    def blok_van(self, fragment_id: str, soort: str) -> dict:
        f = self.fragmenten.get(fragment_id)
        if f is None:
            sys.exit(f"Fragment {fragment_id} bestaat niet in data/apb.json.")
        blok_id = f[{"beurt": "beurt_id", "termijn": "termijn_id", "agendapunt": "agendapunt_id"}[soort]]
        if not blok_id or blok_id not in self.blokken:
            sys.exit(f"Fragment {fragment_id} valt niet binnen een {soort}.")
        return self.blokken[blok_id]

    def titel(self, b: dict) -> str:
        if b["_soort"] == "beurt":
            spreker = b.get("naam") or b.get("titel")
            wat = b.get("fractie") or (b.get("spreker") or {}).get("functie") or ""
            tm = self.blokken.get(b.get("termijn") or "", {}).get("titel", "")
            ap = self.blokken.get(b.get("agendapunt") or "", {}).get("titel", "")
            return f"{spreker}{f' ({wat})' if wat else ''} — {ap}{f', {tm}' if tm else ''}, {b['begin'][11:16]}–{(b.get('eind') or '')[11:16]}"
        if b["_soort"] == "termijn":
            return f"{self.blokken.get(b['agendapunt'], {}).get('titel', '')} — {b['titel']}"
        return b["titel"]


def sam_id(blok: dict) -> str:
    return f"{blok['_soort']}:{blok['id']}"


def is_actueel(sam: dict | None, ds: Dataset) -> bool:
    if not sam:
        return False
    blok = ds.blokken.get(sam.get("blok_id") or "")
    # Een beurt die later is aangevuld (extra interrupties) maakt de samenvatting ook onvolledig.
    if blok is not None and blok["fragmenten"] != sam.get("fragment_ids"):
        return False
    return all(ds.fragmenten.get(fid, {}).get("tekst_hash") == h for fid, h in sam.get("bron_hashes", {}).items())


def toon_lijst(ds: Dataset, dag: str | None, bestaande: dict) -> list[dict]:
    rijen = []
    for d in ds.apb["dagen"]:
        if dag and d["datum"] != dag:
            continue
        print(f"\n{d['label']} — {d['lang']}")
        for ap in d["agendapunten"]:
            if not ap["fragmenten"]:
                continue
            rijen.append(ds.blokken[ap["id"]])
            print(f"  [{len(rijen):>3}] agendapunt {ap['fragmenten'][0]}  {ap['titel']}  ({len(ap['fragmenten'])} fr.){markering(ds.blokken[ap['id']], bestaande, ds)}")
            for tm in (t for t in d["termijnen"] if t["agendapunt"] == ap["id"] and t["fragmenten"]):
                rijen.append(ds.blokken[tm["id"]])
                print(f"  [{len(rijen):>3}]   termijn  {tm['fragmenten'][0]}  {tm['titel']}  ({len(tm['fragmenten'])} fr.){markering(ds.blokken[tm['id']], bestaande, ds)}")
                for b in (b for b in d["beurten"] if b["termijn"] == tm["id"] and sprekersbeurt(b)):
                    rijen.append(ds.blokken[b["id"]])
                    wat = b.get("fractie") or ("kabinet" if b["categorie"] == "kabinet" else "")
                    print(f"  [{len(rijen):>3}]     beurt  {b['fragmenten'][0]}  {b['begin'][11:16]}  {b.get('naam')} ({wat})  {duur(b.get('duur_s'))}, {len(b['fragmenten'])} fr.{markering(ds.blokken[b['id']], bestaande, ds)}")
    return rijen


def sprekersbeurt(b: dict) -> bool:
    return b["soort"] == "Spreekbeurt" and not b["is_voorzitter"] and bool(b["fragmenten"])


def markering(blok: dict, bestaande: dict, ds: Dataset) -> str:
    sam = bestaande.get(sam_id(blok))
    if not sam:
        return ""
    return "  ✓ samenvatting" if is_actueel(sam, ds) else "  ! samenvatting verouderd"


def parse_keuze(invoer: str, maximum: int) -> list[int]:
    gekozen: list[int] = []
    for deel in invoer.replace(" ", "").split(","):
        if not deel:
            continue
        if "-" in deel:
            a, b = (int(x) for x in deel.split("-", 1))
            gekozen.extend(range(a, b + 1))
        else:
            gekozen.append(int(deel))
    return [i for i in gekozen if 1 <= i <= maximum]


# ---------------------------------------------------------------- modellen


def via_api(systeem: str, prompt: str, model: str) -> tuple[str, str]:
    try:
        import anthropic
    except ImportError:
        sys.exit("De Python-SDK ontbreekt: `python3.12 -m pip install anthropic` (vereist Python >= 3.10), of gebruik --backend claude-code.")
    client = anthropic.Anthropic()
    with client.beta.messages.stream(
        model=model,
        max_tokens=16000,
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
        thinking={"type": "adaptive"},
        system=systeem,
        messages=[{"role": "user", "content": prompt}],
    ) as stream:
        bericht = stream.get_final_message()
    if bericht.stop_reason == "refusal":
        raise RuntimeError(f"model weigerde: {getattr(bericht.stop_details, 'category', None)}")
    if bericht.stop_reason == "max_tokens":
        raise RuntimeError("uitvoer afgekapt (max_tokens)")
    tekst = "".join(b.text for b in bericht.content if b.type == "text").strip()
    return tekst, bericht.model


def via_claude_code(systeem: str, prompt: str, model: str) -> tuple[str, str]:
    cmd = ["claude", "-p", "--output-format", "json", "--tools", "", "--strict-mcp-config", "--no-session-persistence",
           "--system-prompt", systeem, "--model", model]
    # Buiten de projectmap draaien, zodat geen project-CLAUDE.md in de context belandt.
    with tempfile.TemporaryDirectory() as leeg:
        r = subprocess.run(cmd, input=prompt, capture_output=True, text=True, cwd=leeg, timeout=900)
    if r.returncode != 0:
        raise RuntimeError(f"claude -p faalde (exit {r.returncode}): {r.stderr.strip()[-400:]}")
    uit = json.loads(r.stdout)
    # Nieuwere versies geven een lijst events; oudere één resultaatobject.
    events = uit if isinstance(uit, list) else [uit]
    resultaat = next((e for e in reversed(events) if e.get("type") == "result" or "result" in e), {})
    if resultaat.get("is_error") or not resultaat:
        raise RuntimeError(f"claude -p meldt fout: {str(resultaat.get('result', r.stdout))[:400]}")
    gebruikt = next((e["message"]["model"] for e in events if e.get("type") == "assistant" and isinstance(e.get("message"), dict)), model)
    return str(resultaat.get("result", "")).strip(), f"{gebruikt} via Claude Code"


# ---------------------------------------------------------------- hoofdprogramma


def samenvat(blok: dict, ds: Dataset, backend: str, model: str) -> dict:
    fragmenten = [ds.fragmenten[fid] for fid in blok["fragmenten"] if fid in ds.fragmenten]
    titel = ds.titel(blok)
    bron = "\n\n———\n\n".join(fragment_als_tekst(f) for f in fragmenten)
    prompt = (f"Vat samen ({blok['_soort']}): {titel}\n"
              f"Gewenste lengte: {LENGTE[blok['_soort']]}.\n\n<verslag>\n{bron}\n</verslag>")
    tekst, gebruikt = (via_api if backend == "api" else via_claude_code)(SYSTEEM, prompt, model)
    if not tekst:
        raise RuntimeError("lege samenvatting ontvangen")
    statussen: dict[str, int] = {}
    for f in fragmenten:
        sleutel = f"{f['bron']['status']} / {f['bron']['soort']}"
        statussen[sleutel] = statussen.get(sleutel, 0) + 1
    return {
        "id": sam_id(blok),
        "scope": blok["_soort"],
        "blok_id": blok["id"],
        "dag": blok["dag"],
        "titel": titel,
        "fragment_ids": [f["id"] for f in fragmenten],
        "bron_hashes": {f["id"]: f["tekst_hash"] for f in fragmenten},
        "bron_statussen": statussen,
        "tekst": tekst,
        "model": gebruikt,
        "backend": backend,
        "prompt_versie": PROMPT_VERSIE,
        "gegenereerd_op": nu_iso(),
        "label": "Automatisch gegenereerde samenvatting — geen bron. Controleer altijd tegen de ruwe tekst.",
    }


def bewaar(alle: dict) -> None:
    DATA.mkdir(exist_ok=True)
    uit = {
        "meta": {
            "schema_versie": 1,
            "bijgewerkt_op": nu_iso(),
            "waarschuwing": "Automatisch gegenereerde samenvattingen. Geen bron; controleer altijd tegen de ruwe tekst in data/apb.json.",
        },
        "samenvattingen": sorted(alle.values(), key=lambda s: (s["dag"], s["fragment_ids"][0] if s["fragment_ids"] else "")),
    }
    tmp = SAMENVATTINGEN.with_name(SAMENVATTINGEN.name + ".tmp")
    tmp.write_text(json.dumps(uit, ensure_ascii=False, indent=1))
    os.replace(tmp, SAMENVATTINGEN)


def bundel_offline() -> None:
    sys.path.insert(0, str(HERE))
    import parse  # noqa: E402 — pas hier nodig, en parse.py staat naast dit script
    pad = parse.bundel()
    if pad:
        print(f"{pad.name} bijgewerkt")


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--lijst", action="store_true")
    ap.add_argument("--kies", action="store_true")
    ap.add_argument("--dag")
    ap.add_argument("--beurt", action="append", default=[], metavar="FRAGMENT_ID")
    ap.add_argument("--termijn", action="append", default=[], metavar="FRAGMENT_ID")
    ap.add_argument("--agendapunt", action="append", default=[], metavar="FRAGMENT_ID")
    ap.add_argument("--alle-beurten", action="store_true")
    ap.add_argument("--opnieuw", action="store_true")
    ap.add_argument("--droog", action="store_true")
    ap.add_argument("--backend", choices=["api", "claude-code"], default="api")
    ap.add_argument("--model", default=STANDAARD_MODEL)
    args = ap.parse_args(argv)

    apb = laad_json(APB, None)
    if apb is None:
        sys.exit("data/apb.json ontbreekt — draai eerst parse.py.")
    ds = Dataset(apb)
    bestaande = {s["id"]: s for s in laad_json(SAMENVATTINGEN, {"samenvattingen": []})["samenvattingen"]}

    if args.lijst:
        toon_lijst(ds, args.dag, bestaande)
        return 0

    blokken: list[dict] = []
    if args.kies:
        rijen = toon_lijst(ds, args.dag, bestaande)
        keuze = input("\nWelke nummers? (bijv. 3,5-9): ")
        blokken += [rijen[i - 1] for i in parse_keuze(keuze, len(rijen))]
    for soort in ("beurt", "termijn", "agendapunt"):
        for fid in getattr(args, soort):
            blokken.append(ds.blok_van(fid, soort))
    if args.alle_beurten:
        if not args.dag:
            sys.exit("--alle-beurten vraagt om --dag.")
        dag = next((d for d in apb["dagen"] if d["datum"] == args.dag), None)
        if dag is None:
            sys.exit(f"Dag {args.dag} staat niet in data/apb.json.")
        blokken += [ds.blokken[b["id"]] for b in dag["beurten"] if sprekersbeurt(b)]
    if not blokken:
        ap.print_help()
        return 1

    uniek = list({b["id"]: b for b in blokken}.values())
    te_doen = [b for b in uniek if args.opnieuw or not is_actueel(bestaande.get(sam_id(b)), ds)]
    overgeslagen = len(uniek) - len(te_doen)
    woorden = sum(len(a["tekst"].split()) for b in te_doen for fid in b["fragmenten"] for a in ds.fragmenten.get(fid, {}).get("alineas", []))
    print(f"{len(te_doen)} blok(ken) samen te vatten, {overgeslagen} al actueel; ca. {woorden:,} woorden brontekst "
          f"(~{int(woorden * 1.6):,} tokens invoer) via {args.backend} / {args.model}")
    if args.droog:
        for b in te_doen:
            print(f"  - {b['_soort']}: {ds.titel(b)} ({len(b['fragmenten'])} fr.)")
        return 0

    fouten = 0
    for i, b in enumerate(te_doen, 1):
        print(f"[{i}/{len(te_doen)}] {b['_soort']}: {ds.titel(b)} …", flush=True)
        try:
            sam = samenvat(b, ds, args.backend, args.model)
        except Exception as e:  # noqa: BLE001 — één mislukt blok mag de rest niet tegenhouden
            fouten += 1
            print(f"    FOUT: {e}", file=sys.stderr)
            continue
        bestaande[sam["id"]] = sam
        bewaar(bestaande)
        print(f"    ok ({len(sam['tekst'].split())} woorden, {sam['model']})")
    if len(te_doen) > fouten:
        bundel_offline()
    return 1 if fouten else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
