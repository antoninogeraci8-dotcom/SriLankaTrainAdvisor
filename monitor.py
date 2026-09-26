"""
Controlla se una data è selezionabile nel calendario di
seatreservation.railway.gov.lk (default: Nanu Oya -> Ella).

Uso locale (stampa SÌ/NO + log dei passaggi):
    python monitor.py 2026-12-28
    python monitor.py 25/10/2026 --headed        # vedi il browser mentre lavora
    python monitor.py 2026-12-28 --from "Kandy" --to "Ella"

Uso cloud (manda WhatsApp via CallMeBot se è SÌ):
    python monitor.py 2026-12-28 --notify

Debug: in debug/ trovi uno screenshot per ogni passaggio e l'HTML del calendario.
"""
from __future__ import annotations

import argparse
import datetime as dt
import os
import pathlib
import sys
import time
import urllib.parse
import urllib.request

from playwright.sync_api import sync_playwright

URL = "https://seatreservation.railway.gov.lk/mtktwebslr/"
MAX_NOTIFICHE = 3

STATE_DIR = pathlib.Path("state")
HEARTBEAT = STATE_DIR / "last_check.txt"
DEBUG_DIR = pathlib.Path("debug")

MONTHS = ["January", "February", "March", "April", "May", "June", "July",
          "August", "September", "October", "November", "December"]

T0 = time.time()


def log(msg: str) -> None:
    print(f"[{time.time() - T0:6.1f}s] {msg}", flush=True)


# ---------- helper JS condivisi: trovano intestazione e riquadro del calendario ----------
JS_LIB = r"""
const MONTH_RE = /^(January|February|March|April|May|June|July|August|September|October|November|December)\s+(20\d\d)$/;
const vis = el => { const r = el.getBoundingClientRect(); return r.width > 0 && r.height > 0 && getComputedStyle(el).visibility !== 'hidden'; };
const cls = e => (e && e.className && e.className.toString()) || '';
const norm = s => (s || '').replace(/\s+/g, ' ').trim();
function findHeaders() {
  return [...document.querySelectorAll('body *')].filter(el => MONTH_RE.test(norm(el.textContent)) && vis(el));
}
function findBox(header) {
  let box = header;
  while (box.parentElement) {
    const nums = [...box.querySelectorAll('*')].filter(e => e.children.length === 0 && /^\d{1,2}$/.test(e.textContent.trim()));
    if (nums.length >= 28) return box;
    box = box.parentElement;
  }
  return box;
}
"""

HEADERS_JS = "() => {" + JS_LIB + r"""
  return findHeaders().map(h => ({text: norm(h.textContent), tag: h.tagName, cls: cls(h)}));
}"""

BOX_INFO_JS = "() => {" + JS_LIB + r"""
  const h = findHeaders()[0];
  if (!h) return null;
  const box = findBox(h);
  return {tag: box.tagName, cls: cls(box), id: box.id, html: box.outerHTML};
}"""

# Marca con data-mon-next i possibili pulsanti "mese successivo" dentro il calendario
NEXT_CANDIDATES_JS = "() => {" + JS_LIB + r"""
  document.querySelectorAll('[data-mon-next]').forEach(e => e.removeAttribute('data-mon-next'));
  const h = findHeaders()[0];
  if (!h) return [];
  const box = findBox(h);
  const nextRe = /(next|right|forward|avanti)/i;
  const arrowRe = /^[›»>→❯▶]$/;
  const out = [];
  let i = 0;
  for (const e of box.querySelectorAll('*')) {
    const meta = [cls(e), e.getAttribute('title'), e.getAttribute('aria-label'), e.getAttribute('data-action')].join(' ');
    const t = norm(e.textContent);
    if (!vis(e) || !(nextRe.test(meta) || arrowRe.test(t))) continue;
    e.setAttribute('data-mon-next', String(i));
    out.push({
      idx: i++, tag: e.tagName, cls: cls(e), text: t.slice(0, 15),
      disabled: /disabled/i.test(cls(e)) || e.getAttribute('aria-disabled') === 'true' || e.hasAttribute('disabled'),
      html: e.outerHTML.slice(0, 160),
    });
  }
  return out;
}"""

DAY_JS = "(day) => {" + JS_LIB + r"""
  const h = findHeaders()[0];
  if (!h) return {found: false, reason: 'intestazione calendario non trovata'};
  const box = findBox(h);
  const outside = /(^|\s)(old|new|other-month|outside|prev-month|next-month|ui-datepicker-other-month|prevMonthDay|nextMonthDay)(\s|$)/i;
  const disabled = /(disabled|unselectable)/i;
  const cands = [...box.querySelectorAll('*')].filter(e => e.children.length === 0 && e.textContent.trim() === String(day) && vis(e));
  const seen = [];
  for (const c of cands) {
    const chain = [c, c.parentElement, c.closest('td'), c.closest('button')].filter(Boolean);
    const classes = chain.map(cls).join(' | ');
    if (chain.some(e => outside.test(cls(e)))) { seen.push('SCARTATA (altro mese): ' + classes); continue; }
    const isDis = chain.some(e => disabled.test(cls(e)) || e.getAttribute('aria-disabled') === 'true' || e.hasAttribute('disabled'));
    return {found: true, enabled: !isDis, classes, html: c.outerHTML.slice(0, 200), seen};
  }
  return {found: false, reason: 'giorno non trovato', seen};
}"""


# ---------- utilità ----------
def parse_date(s: str) -> dt.date:
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y"):
        try:
            return dt.datetime.strptime(s.strip(), fmt).date()
        except ValueError:
            pass
    raise argparse.ArgumentTypeError(f"Data non valida: {s} (usa 2026-12-28 o 28/12/2026)")


def header_to_ym(h: str) -> tuple[int, int]:
    name, year = h.split()
    return int(year), MONTHS.index(name) + 1


def notify(text: str) -> None:
    q = urllib.parse.urlencode({
        "phone": os.environ["CALLMEBOT_PHONE"],
        "apikey": os.environ["CALLMEBOT_APIKEY"],
        "text": text,
    })
    with urllib.request.urlopen(f"https://api.callmebot.com/whatsapp.php?{q}", timeout=30) as r:
        log(f"CallMeBot: HTTP {r.status}")


class Stepper:
    """Salva uno screenshot numerato per ogni passaggio."""
    def __init__(self, page):
        self.page, self.n = page, 0

    def shot(self, name: str) -> None:
        self.n += 1
        path = DEBUG_DIR / f"{self.n:02d}_{name}.png"
        try:
            self.page.screenshot(path=str(path))
            log(f"  screenshot → {path}")
        except Exception as e:
            log(f"  screenshot fallito: {e}")


def current_header(page) -> str | None:
    hs = page.evaluate(HEADERS_JS)
    if len(hs) > 1:
        log(f"  ⚠ più intestazioni visibili: {[h['text'] for h in hs]} (uso la prima)")
    return hs[0]["text"] if hs else None


def select_station(page, idx: int, name: str) -> None:
    selects = page.locator("select:visible")
    log(f"Stazione #{idx} '{name}': trovati {selects.count()} <select> visibili")
    try:
        selects.nth(idx).select_option(label=name, timeout=5000)
        log(f"  ✓ selezionata '{name}'")
    except Exception as e:
        log(f"  ✗ non riesco a selezionarla ({e.__class__.__name__}), proseguo")


def open_calendar(page, st: Stepper) -> None:
    for sel in ["input[placeholder='Date']", ".fa-calendar", ".input-group-append", ".input-group-text"]:
        loc = page.locator(sel).first
        n = loc.count()
        log(f"Apro calendario con '{sel}': {'trovato' if n else 'non trovato'}")
        if n and loc.is_visible():
            loc.click()
            page.wait_for_timeout(700)
            h = current_header(page)
            log(f"  intestazione dopo il click: {h}")
            if h:
                st.shot("calendario_aperto")
                return
    raise RuntimeError("Non riesco ad aprire il calendario")


def wait_header_change(page, old: str, timeout_ms: int = 2500) -> str | None:
    end = time.time() + timeout_ms / 1000
    while time.time() < end:
        h = current_header(page)
        if h != old:
            return h
        page.wait_for_timeout(150)
    return current_header(page)


def click_next(page, st: Stepper, header: str) -> bool:
    cands = page.evaluate(NEXT_CANDIDATES_JS)
    log(f"  candidati 'mese successivo' nel calendario: {len(cands)}")
    for c in cands:
        log(f"    [{c['idx']}] <{c['tag']}> class='{c['cls']}' text='{c['text']}' disabled={c['disabled']}")
        log(f"        {c['html']}")
    for c in cands:
        if c["disabled"]:
            continue
        log(f"  → click sul candidato [{c['idx']}]")
        try:
            page.locator(f"[data-mon-next='{c['idx']}']").click(timeout=3000)
        except Exception as e:
            log(f"    click fallito: {e.__class__.__name__}: {str(e)[:150]}")
            continue
        new = wait_header_change(page, header)
        log(f"    intestazione: {header} → {new}")
        if new and new != header:
            st.shot(f"mese_{new.replace(' ', '_')}")
            return True
        log("    nessun cambio di mese, provo il prossimo candidato")
    return False


def is_available(target: dt.date, from_st: str, to_st: str, headed: bool = False) -> bool:
    if target < dt.date.today():
        log("Data nel passato")
        return False
    target_ym = (target.year, target.month)
    DEBUG_DIR.mkdir(exist_ok=True)
    for f in DEBUG_DIR.glob("*.png"):
        f.unlink()

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=not headed, slow_mo=150 if headed else 0)
        page = browser.new_page(viewport={"width": 1400, "height": 1000}, locale="en-US")
        page.on("console", lambda m: m.type == "error" and log(f"  [console error] {m.text[:150]}"))
        st = Stepper(page)
        try:
            log(f"Apro {URL}")
            page.goto(URL, wait_until="networkidle", timeout=90000)
            log(f"  pagina caricata: '{page.title()}'")
            st.shot("pagina")

            select_station(page, 0, from_st)
            select_station(page, 1, to_st)
            st.shot("stazioni")

            open_calendar(page, st)
            box = page.evaluate(BOX_INFO_JS)
            if box:
                (DEBUG_DIR / "calendar.html").write_text(box["html"], encoding="utf-8")
                log(f"Riquadro calendario: <{box['tag']}> id='{box['id']}' class='{box['cls']}' → debug/calendar.html")

            for step in range(24):
                header = current_header(page)
                ym = header_to_ym(header)
                log(f"Passo {step}: calendario su {header}, cerco {MONTHS[target.month - 1]} {target.year}")
                if ym == target_ym:
                    log("  ✓ mese giusto raggiunto")
                    break
                if ym > target_ym:
                    raise RuntimeError(f"Il calendario è su {header}, oltre la data cercata")
                if not click_next(page, st, header):
                    log(f"  ✗ non riesco ad andare oltre {header}")
                    st.shot("bloccato")
                    return False
            else:
                raise RuntimeError("Non riesco ad arrivare al mese cercato")

            res = page.evaluate(DAY_JS, target.day)
            for s in res.get("seen", []):
                log(f"  {s}")
            if not res["found"]:
                raise RuntimeError(res["reason"])
            log(f"Giorno {target.day}: classi = {res['classes']}")
            log(f"  {res['html']}")
            log(f"  → {'ATTIVO' if res['enabled'] else 'DISABILITATO'}")
            st.shot("risultato")
            return res["enabled"]
        finally:
            try:
                (DEBUG_DIR / "page.html").write_text(page.content(), encoding="utf-8")
            except Exception:
                pass
            browser.close()


def main() -> None:
    ap = argparse.ArgumentParser(description="Controlla se una data è prenotabile su Sri Lanka Railways")
    ap.add_argument("date", type=parse_date, help="es. 2026-12-28 oppure 28/12/2026")
    ap.add_argument("--from", dest="from_st", default="Nanu Oya")
    ap.add_argument("--to", dest="to_st", default="Ella")
    ap.add_argument("--headed", action="store_true", help="mostra il browser (al rallentatore)")
    ap.add_argument("--notify", action="store_true", help="manda WhatsApp se SÌ (modalità cloud)")
    ap.add_argument("--test-notify", action="store_true", help="manda un WhatsApp di prova")
    a = ap.parse_args()

    label = f"{a.date:%d/%m/%Y} {a.from_st} → {a.to_st}"
    log(f"Controllo {label}")

    if a.test_notify:
        notify(f"✅ Test: monitor attivo per {label}")

    if not a.notify:
        ok = is_available(a.date, a.from_st, a.to_st, a.headed)
        print(f"\n{label}: {'SÌ' if ok else 'NO'}")
        return

    STATE_DIR.mkdir(exist_ok=True)
    HEARTBEAT.write_text(dt.date.today().isoformat() + "\n")
    counter = STATE_DIR / f"notified_{a.date.isoformat()}.txt"
    count = int(counter.read_text()) if counter.exists() else 0
    if count >= MAX_NOTIFICHE:
        log("Già notificato, niente da fare")
        return

    ok = is_available(a.date, a.from_st, a.to_st)
    print(f"\n{label}: {'SÌ' if ok else 'NO'}")
    if ok:
        notify(f"🚂 {label}: data PRENOTABILE! Vai subito: {URL}")
        counter.write_text(str(count + 1))


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        log(f"ERRORE: {e} (guarda la cartella debug/)")
        sys.exit(2)
