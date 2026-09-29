"""
Controlla se una data è selezionabile nel calendario di
seatreservation.railway.gov.lk (default: Nanu Oya -> Ella) e, con --seats,
esegue la ricerca vera per quella data e i 3 giorni precedenti leggendo i posti per classe.

Uso locale:
    python monitor.py 2026-12-28                 # SÌ/NO
    python monitor.py 2026-10-30 --seats         # posti per classe: 30/10, 29/10, 28/10, 27/10
    python monitor.py 2026-10-30 --seats --headed
    python monitor.py auto --seats --seats-days 1

Uso cloud (WhatsApp via CallMeBot se è SÌ, con i posti se c'è --seats):
    python monitor.py auto --notify --seats

Debug: in debug/ trovi screenshot per ogni passaggio, l'HTML del calendario e dei risultati.
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
from zoneinfo import ZoneInfo

URL = "https://seatreservation.railway.gov.lk/mtktwebslr/"
MAX_NOTIFICHE = 3
MAX_MSG_CHARS = 1800  # CallMeBot passa il testo in URL: teniamolo compatto

# ---- Pianificazione (ora dello Sri Lanka, UTC+5:30, niente ora legale) ----
SL_TZ = dt.timezone(dt.timedelta(hours=5, minutes=30))
IT_TZ = ZoneInfo("Europe/Rome")
FAILS_KEEP = 10
OPEN_DAYS_BEFORE = 30  # la data D si sblocca il giorno D-30
OPENING_WINDOWS = [((6, 40), (8, 0)), ((9, 40), (11, 30))]  # solo pianificazione automatica
DAILY_AT = (10, 15)

STATE_DIR = pathlib.Path("state")
HEARTBEAT = STATE_DIR / "last_check.txt"
LAST_DAILY = STATE_DIR / "last_daily.txt"
LAST_HOURLY = STATE_DIR / "last_hourly.txt"
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

# Trova la cella del giorno, la marca con data-mon-day (per poterla cliccare) e dice se è attiva
DAY_JS = "(day) => {" + JS_LIB + r"""
  document.querySelectorAll('[data-mon-day]').forEach(e => e.removeAttribute('data-mon-day'));
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
    c.setAttribute('data-mon-day', '1');
    return {found: true, enabled: !isDis, classes, html: c.outerHTML.slice(0, 200), seen};
  }
  return {found: false, reason: 'giorno non trovato', seen};
}"""

# Estrae in modo generico il contenuto della pagina: tabelle visibili + righe di testo "rilevanti"
PAGE_TEXT_JS = r"""
() => {
  const norm = s => (s || '').replace(/[ \t\u00a0]+/g, ' ').trim();
  const vis = el => { const r = el.getBoundingClientRect(); return r.width > 0 && r.height > 0; };
  const tables = [...document.querySelectorAll('table')].filter(vis).map(t =>
    [...t.rows].map(r => [...r.cells].map(c => norm(c.innerText)).join(' | ')).filter(x => x.replace(/[| ]/g, ''))
  ).filter(t => t.length);
  const lines = (document.body.innerText || '').split('\n').map(norm).filter(l => l && l.length <= 200);
  return {url: location.href, title: document.title, tables, lines};
}
"""
# Righe utili nei risultati: classi, posti, orari, treni, messaggi di errore
RELEVANT = r"(class|saloon|observation|seat|available|berth|sleeper|reserved|train|\b\d{1,2}[:.]\d{2}\b|no\s+(result|train|seat)|sold|full|not\s+available|error|login)"


# ---------- utilità ----------
def auto_target(now: dt.datetime) -> dt.date:
    """Prossima data che si sblocca (o appena sbloccata) alla mezzanotte SL.
    Cambia a mezzogiorno SL (8:30 IT legale / 7:30 IT solare): resta la stessa per tutta la sera.
    Es. 26/09 pomeriggio o sera -> 27/10."""
    base = (now.astimezone(SL_TZ) - dt.timedelta(hours=12)).date()
    return base + dt.timedelta(days=1 + OPEN_DAYS_BEFORE)


def parse_date(s: str) -> dt.date:
    if s.strip().lower() in ("auto", "domani+30"):
        return auto_target(dt.datetime.now(SL_TZ))
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y"):
        try:
            return dt.datetime.strptime(s.strip(), fmt).date()
        except ValueError:
            pass
    raise argparse.ArgumentTypeError(f"Data non valida: {s} (usa 2026-12-28, 28/12/2026 o auto)")


def header_to_ym(h: str) -> tuple[int, int]:
    name, year = h.split()
    return int(year), MONTHS.index(name) + 1


def notify(text: str) -> None:
    if len(text) > MAX_MSG_CHARS:
        text = text[:MAX_MSG_CHARS - 20] + "\n…(troncato)"
    q = urllib.parse.urlencode({
        "phone": os.environ["CALLMEBOT_PHONE"],
        "apikey": os.environ["CALLMEBOT_APIKEY"],
        "text": text,
    })
    with urllib.request.urlopen(f"https://api.callmebot.com/whatsapp.php?{q}", timeout=30) as r:
        log(f"CallMeBot: HTTP {r.status}")


def reset_debug() -> None:
    DEBUG_DIR.mkdir(exist_ok=True)
    for f in list(DEBUG_DIR.glob("*.png")) + list(DEBUG_DIR.glob("results_*.html")):
        f.unlink()


class Stepper:
    """Salva uno screenshot numerato per ogni passaggio (numerazione unica per tutto il run)."""
    n = 0

    def __init__(self, page, prefix: str = ""):
        self.page, self.prefix = page, prefix

    def shot(self, name: str) -> None:
        Stepper.n += 1
        path = DEBUG_DIR / f"{Stepper.n:02d}_{self.prefix}{name}.png"
        try:
            self.page.screenshot(path=str(path), full_page=True)
            log(f"  screenshot → {path}")
        except Exception as e:
            log(f"  screenshot fallito: {e}")


# ---------- navigazione ----------
def new_page(browser):
    page = browser.new_page(viewport={"width": 1400, "height": 1000}, locale="en-US",
                            timezone_id="Asia/Colombo")  # il browser "vive" in Sri Lanka, ovunque giri
    page.on("console", lambda m: m.type == "error" and log(f"  [console error] {m.text[:150]}"))

    def on_dialog(d):
        log(f"  [popup del sito] {d.message}")
        d.accept()
    page.on("dialog", on_dialog)
    return page


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


def fill_passengers(page, n: int = 1) -> None:
    loc = page.locator("input[placeholder*='Passenger' i], select[name*='passenger' i], input[name*='passenger' i]").first
    if not loc.count():
        log("Passeggeri: campo non trovato")
        return
    try:
        if (loc.evaluate("e => e.tagName")).lower() == "select":
            loc.select_option(str(n), timeout=3000)
        else:
            loc.fill(str(n), timeout=3000)
        log(f"Passeggeri: impostato {n}")
    except Exception as e:
        log(f"Passeggeri: non riesco a impostarlo ({e.__class__.__name__})")


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


def prepare(page, st: Stepper, from_st: str, to_st: str) -> None:
    log(f"Apro {URL}")
    page.goto(URL, wait_until="networkidle", timeout=90000)
    log(f"  pagina caricata: '{page.title()}'")
    st.shot("pagina")
    select_station(page, 0, from_st)
    select_station(page, 1, to_st)
    fill_passengers(page, 1)
    st.shot("stazioni")
    open_calendar(page, st)
    box = page.evaluate(BOX_INFO_JS)
    if box:
        (DEBUG_DIR / "calendar.html").write_text(box["html"], encoding="utf-8")
        log(f"Riquadro calendario: <{box['tag']}> id='{box['id']}' class='{box['cls']}'")


def goto_month(page, st: Stepper, target: dt.date) -> bool:
    target_ym = (target.year, target.month)
    for step in range(24):
        header = current_header(page)
        ym = header_to_ym(header)
        log(f"Passo {step}: calendario su {header}, cerco {MONTHS[target.month - 1]} {target.year}")
        if ym == target_ym:
            log("  ✓ mese giusto raggiunto")
            return True
        if ym > target_ym:
            raise RuntimeError(f"Il calendario è su {header}, oltre la data cercata")
        if not click_next(page, st, header):
            log(f"  ✗ non riesco ad andare oltre {header}")
            st.shot("bloccato")
            return False
    raise RuntimeError("Non riesco ad arrivare al mese cercato")


def check_day(page, target: dt.date) -> dict:
    res = page.evaluate(DAY_JS, target.day)
    for s in res.get("seen", []):
        log(f"  {s}")
    if not res["found"]:
        raise RuntimeError(res["reason"])
    log(f"Giorno {target.day}: classi = {res['classes']} → {'ATTIVO' if res['enabled'] else 'DISABILITATO'}")
    return res


# ---------- 1) la data è selezionabile? ----------
def is_available(target: dt.date, from_st: str, to_st: str, headed: bool = False) -> bool:
    if target < dt.datetime.now(SL_TZ).date():
        log("Data nel passato")
        return False
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=not headed, slow_mo=150 if headed else 0)
        page = new_page(browser)
        st = Stepper(page, "disp_")
        try:
            prepare(page, st, from_st, to_st)
            if not goto_month(page, st, target):
                return False
            ok = check_day(page, target)["enabled"]
            st.shot("risultato")
            return ok
        finally:
            try:
                (DEBUG_DIR / "page.html").write_text(page.content(), encoding="utf-8")
            except Exception:
                pass
            browser.close()


# ---------- 2) ricerca vera e posti per classe ----------
def search_one(browser, target: dt.date, from_st: str, to_st: str) -> dict:
    tag = f"{target:%m%d}_"
    page = new_page(browser)
    st = Stepper(page, tag)
    log(f"=== Ricerca posti per {target:%d/%m/%Y} ===")
    try:
        prepare(page, st, from_st, to_st)
        if not goto_month(page, st, target) or not check_day(page, target)["enabled"]:
            return {"date": target, "status": "data non ancora aperta", "lines": []}

        page.locator("[data-mon-day='1']").click(timeout=3000)
        page.wait_for_timeout(500)
        try:
            val = page.locator("input[placeholder='Date']").first.input_value()
        except Exception:
            val = "?"
        log(f"  data inserita nel campo: '{val}'")
        st.shot("data_scelta")

        before = set(page.evaluate(PAGE_TEXT_JS)["lines"])  # testo prima della ricerca (per scartare banner ecc.)

        btn = page.get_by_role("button", name="Search")
        if not btn.count():
            btn = page.locator("button:has-text('Search'), input[value='Search']")
        log(f"  click su Search ({btn.count()} pulsanti trovati)")
        btn.first.click(timeout=5000)
        try:
            page.wait_for_load_state("networkidle", timeout=30000)
        except Exception:
            log("  (networkidle non raggiunto in 30s, proseguo)")
        page.wait_for_timeout(2500)
        st.shot("risultati")

        html_path = DEBUG_DIR / f"results_{target.isoformat()}.html"
        html_path.write_text(page.content(), encoding="utf-8")
        data = page.evaluate(PAGE_TEXT_JS)
        log(f"  URL risultati: {data['url']} | titolo: '{data['title']}' | HTML → {html_path}")

        import re
        rel = re.compile(RELEVANT, re.I)
        new_lines = [l for l in data["lines"] if l not in before]
        relevant = [l for l in new_lines if rel.search(l)]
        log(f"  righe nuove dopo la ricerca: {len(new_lines)}, rilevanti: {len(relevant)}")
        for l in relevant[:60]:
            log(f"    | {l}")
        for i, t in enumerate(data["tables"]):
            log(f"  tabella {i} ({len(t)} righe):")
            for r in t[:30]:
                log(f"    # {r}")

        # per il messaggio: righe di tabella se ci sono, altrimenti le righe rilevanti
        table_rows = [r for t in data["tables"] for r in t if rel.search(r)]
        lines = table_rows or relevant
        status = "ok" if lines else "nessun risultato leggibile (vedi HTML)"
        return {"date": target, "status": status, "lines": lines[:15]}
    except Exception as e:
        log(f"  ERRORE nella ricerca: {e}")
        st.shot("errore")
        return {"date": target, "status": f"errore: {str(e)[:80]}", "lines": []}
    finally:
        page.close()


def seats_report(target: dt.date, days_before: int, from_st: str, to_st: str, headed: bool = False) -> list[dict]:
    from playwright.sync_api import sync_playwright

    dates = [target - dt.timedelta(days=i) for i in range(days_before + 1)]
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=not headed, slow_mo=150 if headed else 0)
        try:
            return [search_one(browser, d, from_st, to_st) for d in dates]
        finally:
            browser.close()


def format_report(report: list[dict]) -> str:
    out = []
    for r in report:
        out.append(f"📅 {r['date']:%a %d/%m}: {r['status'] if r['status'] != 'ok' else ''}".rstrip())
        out += [f"  • {l}" for l in r["lines"]]
    return "\n".join(out)


# ---------- pianificazione ----------
def read(path: pathlib.Path) -> str:
    return path.read_text().strip() if path.exists() else ""


def counter_path(target: dt.date) -> pathlib.Path:
    return STATE_DIR / f"notified_{target.isoformat()}.txt"


def fails_path(target: dt.date) -> pathlib.Path:
    return STATE_DIR / f"fails_{target.isoformat()}.txt"


def fmt_times(now: dt.datetime) -> str:
    return f"{now.astimezone(IT_TZ):%H:%M} IT ({now.astimezone(SL_TZ):%H:%M} SL)"


def parse_window(w: str) -> tuple[int, int]:
    """'19:00-24:00' -> minuti dall'inizio del giorno (inizio, fine)."""
    a, b = w.strip().split("-")
    to_min = lambda x: int(x.split(":")[0]) * 60 + int(x.split(":")[1])
    return to_min(a), to_min(b)


def should_run(target: dt.date, now: dt.datetime) -> tuple[bool, str]:
    count = int(read(counter_path(target)) or 0)
    if count >= MAX_NOTIFICHE:
        return False, "già notificato 3 volte"
    if count > 0:
        return True, f"avvisi in corso ({count}/{MAX_NOTIFICHE})"

    window = os.environ.get("CHECK_WINDOW_IT", "").strip()
    if window:
        start, end = parse_window(window)
        it = now.astimezone(IT_TZ)
        m = it.hour * 60 + it.minute
        inside = (start <= m < end) if start < end else (m >= start or m < end)  # anche a cavallo di mezzanotte
        return inside, f"finestra IT {window}: ora {it:%H:%M} {'dentro' if inside else 'fuori'}"

    now = now.astimezone(SL_TZ)
    today, hm = now.date(), (now.hour, now.minute)
    open_day = target - dt.timedelta(days=OPEN_DAYS_BEFORE)
    if today > target:
        return False, "data già passata"
    if today == open_day:
        for a, b in OPENING_WINDOWS:
            if a <= hm < b:
                return True, f"giorno di apertura, finestra {a[0]:02d}:{a[1]:02d}-{b[0]:02d}:{b[1]:02d} SL"
        if read(LAST_HOURLY) != now.strftime("%Y-%m-%dT%H"):
            return True, "giorno di apertura, controllo orario"
        return False, "giorno di apertura, fuori finestra (controllo orario già fatto)"
    if hm >= DAILY_AT and read(LAST_DAILY) != today.isoformat():
        return True, "controllo giornaliero"
    return False, f"giorno normale, controllo giornaliero {'già fatto' if read(LAST_DAILY) == today.isoformat() else 'non ancora dovuto'}"


def main() -> None:
    ap = argparse.ArgumentParser(description="Controlla se una data è prenotabile su Sri Lanka Railways")
    ap.add_argument("date", type=parse_date, help="es. 2026-12-28, 28/12/2026 oppure auto (= prossima data che si sblocca)")
    ap.add_argument("--from", dest="from_st", default="Nanu Oya")
    ap.add_argument("--to", dest="to_st", default="Ella")
    ap.add_argument("--headed", action="store_true", help="mostra il browser (al rallentatore)")
    ap.add_argument("--seats", action="store_true", help="fa la ricerca vera e legge i posti per classe")
    ap.add_argument("--seats-days", type=int, default=3, help="quanti giorni precedenti cercare con --seats (default 3)")
    ap.add_argument("--seats-on-no", choices=["sera", "sempre", "mai"],
                    default=os.environ.get("SEATS_ON_NO", "sera").strip().lower() or "sera",
                    help="con --notify --seats: messaggio posti anche col NO (sera=1 volta a sera, sempre, mai)")
    ap.add_argument("--notify", action="store_true", help="manda WhatsApp se SÌ (modalità cloud)")
    ap.add_argument("--test-notify", action="store_true", help="manda un WhatsApp di prova")
    ap.add_argument("--should-run", action="store_true", help="dice solo se in questo momento va fatto il controllo")
    ap.add_argument("--force", action="store_true", help="con --should-run: rispondi sempre sì")
    a = ap.parse_args()

    label = f"{a.date:%d/%m/%Y} {a.from_st} → {a.to_st}"
    now = dt.datetime.now(SL_TZ)

    if a.should_run:
        open_day = a.date - dt.timedelta(days=OPEN_DAYS_BEFORE)
        run, why = (True, "run manuale") if a.force else should_run(a.date, now)
        log(f"Ora: {fmt_times(now)} | {label} | apertura prevista: {open_day:%d/%m}")
        log(f"Decisione: {'CONTROLLO' if run else 'SALTO'} ({why})")
        if os.environ.get("GITHUB_OUTPUT"):
            with open(os.environ["GITHUB_OUTPUT"], "a") as f:
                f.write(f"run={'true' if run else 'false'}\n")
        return

    log(f"Controllo {label} alle {fmt_times(now)}")
    reset_debug()

    if a.test_notify:
        notify(f"✅ Test: monitor attivo per {label}")

    if not a.notify:
        ok = is_available(a.date, a.from_st, a.to_st, a.headed)
        print(f"\n{label}: {'SÌ' if ok else 'NO'}")
        if a.seats:
            report = seats_report(a.date, a.seats_days, a.from_st, a.to_st, a.headed)
            print("\n" + format_report(report))
        return

    # --- modalità cloud ---
    STATE_DIR.mkdir(exist_ok=True)
    HEARTBEAT.write_text(now.date().isoformat() + "\n")
    LAST_HOURLY.write_text(now.strftime("%Y-%m-%dT%H") + "\n")
    if (now.hour, now.minute) >= DAILY_AT:
        LAST_DAILY.write_text(now.date().isoformat() + "\n")

    counter = counter_path(a.date)
    count = int(read(counter) or 0)
    if count >= MAX_NOTIFICHE:
        log("Già notificato, niente da fare")
        return

    ok = is_available(a.date, a.from_st, a.to_st)
    print(f"\n{label}: {'SÌ' if ok else 'NO'}")
    fails = fails_path(a.date)
    past = [l for l in read(fails).splitlines() if l]

    if not ok:
        past.append(fmt_times(now))
        fails.write_text("\n".join(past[-FAILS_KEEP:]) + "\n")
        # Riepilogo posti anche se la data è ancora chiusa, secondo --seats-on-no:
        #   sera   = una volta a sera (per data cercata)   [default]
        #   sempre = a ogni check NO
        #   mai    = solo nell'avviso di SÌ
        seats_sent = STATE_DIR / f"seats_sent_{a.date.isoformat()}.txt"
        mode = a.seats_on_no
        if a.seats and (mode == "sempre" or (mode == "sera" and not seats_sent.exists())):
            report = seats_report(a.date, a.seats_days, a.from_st, a.to_st)
            rep = format_report(report)
            print("\n" + rep)
            notify(f"📊 {label}: ancora chiusa (check {fmt_times(now)})\n\nPosti:\n{rep}")
            seats_sent.write_text(fmt_times(now) + "\n")
        return

    opened = STATE_DIR / f"opened_{a.date.isoformat()}.txt"
    if not opened.exists():
        opened.write_text(fmt_times(now) + "\n")
    last3 = ", ".join(reversed(past[-3:])) or "nessuno registrato"
    msg = (f"🚂 {label}: PRENOTABILE!\n"
           f"Check OK alle {fmt_times(now)}\n"
           f"Ultimi check NO: {last3}\n"
           f"{URL}")
    if a.seats:
        report = seats_report(a.date, a.seats_days, a.from_st, a.to_st)
        rep = format_report(report)
        print("\n" + rep)
        msg += "\n\nPosti:\n" + rep
    notify(msg)
    counter.write_text(str(count + 1))


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        log(f"ERRORE: {e} (guarda la cartella debug/)")
        sys.exit(2)
