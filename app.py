"""Bac Bo Monitor Pro - coleta, estatísticas, alertas Telegram e laboratório de backtest.

Não prevê resultados e não aposta sozinho. Cada rodada é independente.
"""
import os, re, json, time, random, hashlib, sqlite3, threading, csv, io
from collections import deque
import math, secrets, subprocess, atexit, shutil
from datetime import datetime, timezone, timedelta
try:
    from zoneinfo import ZoneInfo
    TZ = ZoneInfo("Atlantic/Cape_Verde")
except Exception:
    TZ = timezone(timedelta(hours=-1))
import requests
from flask import Flask, render_template, jsonify, request, Response, g

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CFG = {
    "poll_seconds": 2, "timezone": "Atlantic/Cape_Verde",
    "history_newest_first": True,
    "telegram_token": "", "telegram_chat_id": "",
    "telegram_all_rounds": False, "streak_alert": 4, "history_limit": 200, "reset_on_start": True, "tunnel": True, "tie_emoji": "🟡",
    "tie_payout": 5, "tie_pushes_pb": True,
    "tie_as_green": True, "telegram_signals": True, "max_gale": 2,  # empate protegido = green; gale máx. (0-2)
    "telegram_pre_alert": True,  # avisa "a analisar padrão" 1 rodada antes do gatilho confirmar
    "tunnel_url": "",  # hostname público fixo, ex.: https://painel.seudominio.com
    "cloudflare_tunnel_token": "",  # token de um Cloudflare Tunnel nomeado
    "engine_signals": False,  # False = só "Meus padrões" (construtor) geram entrada/Telegram; os 9 padrões fixos ficam só na tabela de estatísticas
     # confirme na mesa: pagamento do Tie e o que acontece com Player/Banker no empate
}
_p = os.path.join(BASE_DIR, "config.json")
if os.path.exists(_p):
    with open(_p, encoding="utf-8") as f:
        CFG.update(json.load(f))
CFG["telegram_token"] = os.getenv("TELEGRAM_TOKEN", CFG["telegram_token"])
CFG["telegram_chat_id"] = os.getenv("TELEGRAM_CHAT_ID", CFG["telegram_chat_id"])

ROUND_ID = "cc71e81d-8b56-4868-91c7-7224be543dce"  # fixo: não é lido do config.json
CFG["round_id"] = ROUND_ID
BASE = f"https://api.core.public.tipminer.com/v1/bac-bo/rounds/{ROUND_ID}"
HISTORY_URL = BASE + "/history"   # ?limit=200&timezone=Atlantic/Cape_Verde
LIVE_URL = BASE + "/live"
DB = os.path.join(BASE_DIR, "bacbo.db")
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Origin": "https://www.tipminer.com", "Referer": "https://www.tipminer.com/",
}
EMOJI = {"PLAYER": "🔵", "BANKER": "🔴", "TIE": "🟡"}
P_THEORY = {"PLAYER": 0.4437, "BANKER": 0.4437, "TIE": 0.1127}  # dois dados por lado

app = Flask(__name__)
lock = threading.Lock()
state = {
    "version": 0, "started": time.time(), "ready": False,
    "history_status": None, "history_ok": False, "history_error": None,
    "live_status": None, "live_connected": False, "live_error": None,
    "ignored": 0, "last_ok": None, "tg_configured": bool(CFG["telegram_token"] and CFG["telegram_chat_id"]),
    "tg_last": None, "history_target": min(200, int(CFG["history_limit"])), "history_loaded": 0,
}
raw_log = deque(maxlen=14)


# ---------------------------------------------------------------- banco
def conn():
    c = sqlite3.connect(DB, timeout=10)
    c.row_factory = sqlite3.Row
    return c


def init_db():
    c = conn()
    c.execute("PRAGMA journal_mode=WAL")
    cols = [r["name"] for r in c.execute("PRAGMA table_info(rounds)").fetchall()]
    if cols and "side" not in cols:  # banco da versão antiga: guarda como backup e recria
        c.execute("DROP TABLE IF EXISTS rounds_antiga")
        c.execute("ALTER TABLE rounds RENAME TO rounds_antiga")
    c.execute("""CREATE TABLE IF NOT EXISTS rounds(
        id INTEGER PRIMARY KEY AUTOINCREMENT, key TEXT UNIQUE, ts TEXT NOT NULL,
        side TEXT NOT NULL, ps INTEGER, bs INTEGER, raw TEXT)""")
    c.commit(); c.close()


# ---------------------------------------------------------------- parser
SIDES = {
    "PLAYER": {"player", "p", "blue", "azul", "jogador"},
    "BANKER": {"banker", "b", "red", "vermelho", "banca"},
    "TIE": {"tie", "t", "empate", "yellow", "amarelo", "draw"},
}
RESULT_KEYS = ("result", "outcome", "winner", "winnerside", "resultado", "vencedor", "side", "color", "type")
ID_KEYS = ("roundid", "round_id", "gameid", "game_id", "id", "_id", "uuid")
TS_KEYS = ("createdat", "created_at", "timestamp", "time", "date", "finishedat", "settledat", "datetime")


def norm_side(v):
    if isinstance(v, (dict, list, bool)) or v is None:
        return None
    s = re.sub(r"([a-z])([A-Z])", r"\1 \2", str(v)).lower()
    for tok in re.findall(r"[a-zà-ú]+", s):
        for side, names in SIDES.items():
            if tok in names:
                return side
    return None


def flat(o, path="", depth=0):
    if depth > 4:
        return
    if isinstance(o, dict):
        for k, v in o.items():
            yield from flat(v, f"{path}.{k}".lower() if path else str(k).lower(), depth + 1)
    elif isinstance(o, list):
        yield path, o
        for i, v in enumerate(o[:20]):
            yield from flat(v, f"{path}[{i}]", depth + 1)
    else:
        yield path, o


def to_iso(v):
    try:
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            v = v / 1000 if v > 1e11 else v
            return datetime.fromtimestamp(v, timezone.utc).isoformat()
        if isinstance(v, str) and len(v) >= 10:
            d = datetime.fromisoformat(v.replace("Z", "+00:00"))
            return (d if d.tzinfo else d.replace(tzinfo=timezone.utc)).astimezone(timezone.utc).isoformat()
    except Exception:
        pass
    return None


def parse_round(o):
    """Devolve dict(side, ps, bs, rid, ts) ou None se o objeto não for uma rodada finalizada."""
    if isinstance(o, str):
        try:
            o = json.loads(o)
        except Exception:
            return None
    if not isinstance(o, dict):
        return None
    items = list(flat(o))
    side = None
    for rk in RESULT_KEYS:
        for p, v in items:
            if p.split(".")[-1] == rk and (side := norm_side(v)):
                break
        if side:
            break
    ps = bs = None
    dice = {}
    for p, v in items:
        leaf = p.split(".")[-1]
        who = "p" if "player" in p else "b" if "banker" in p else None
        if not who or isinstance(v, bool):
            continue
        val = None
        if isinstance(v, (int, float)) and re.search(r"score|total|sum|point", p):
            val = int(v)
        elif isinstance(v, int) and 1 <= v <= 6 and re.search(r"dic|die|dado", p):
            dice.setdefault(who, []).append(v)
            continue
        elif isinstance(v, list) and v and all(isinstance(x, int) and 1 <= x <= 6 for x in v) and leaf.startswith(("player", "banker")):
            val = sum(v)
        if val is not None:
            ps, bs = (val, bs) if who == "p" else (ps, val)
    if ps is None and len(dice.get("p", [])) >= 2:
        ps = sum(dice["p"])
    if bs is None and len(dice.get("b", [])) >= 2:
        bs = sum(dice["b"])
    if side is None and ps is not None and bs is not None:
        side = "PLAYER" if ps > bs else "BANKER" if bs > ps else "TIE"
    if side is None:
        return None
    rid = next((str(v) for p, v in items if p.split(".")[-1] in ID_KEYS and p.count(".") <= 1
                and isinstance(v, (str, int)) and str(v) != CFG["round_id"]), None)
    ts = next((to_iso(v) for p, v in items if p.split(".")[-1] in TS_KEYS and to_iso(v)), None)
    mult = next((v for p, v in items if p.split(".")[-1] in ("multiplier", "mult", "payout", "odds")
                 and isinstance(v, (int, float)) and not isinstance(v, bool)), None)
    return {"side": side, "ps": ps, "bs": bs, "rid": rid, "ts": ts, "mult": mult}


def unwrap_list(d):
    if isinstance(d, list):
        return d
    if isinstance(d, dict):
        for k in ("data", "results", "rounds", "history", "items", "docs", "rows"):
            v = d.get(k)
            if isinstance(v, list):
                return v
            if isinstance(v, dict) and (r := unwrap_list(v)):
                return r
    return None


# ---------------------------------------------------------------- registo
def log_raw(src, payload):
    txt = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
    with lock:
        raw_log.appendleft({"src": src, "at": datetime.now(timezone.utc).isoformat(), "text": txt[:1800]})


def record(payload, src, notify=True):
    p = parse_round(payload)
    if not p:
        with lock:
            state["ignored"] += 1
        return False
    key = "id:" + p["rid"] if p["rid"] else "h:" + hashlib.sha256(
        json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()
    now = datetime.now(timezone.utc).isoformat()
    c = conn()
    cur = c.execute("INSERT OR IGNORE INTO rounds(key,ts,side,ps,bs,raw) VALUES(?,?,?,?,?,?)",
                    (key, p["ts"] or now, p["side"], p["ps"], p["bs"], json.dumps(payload, ensure_ascii=False, default=str)[:4000]))
    c.commit(); added = cur.rowcount > 0; c.close()
    if added:
        with lock:
            state["version"] += 1
            state["last_ok"] = now
        if state["ready"]:
            # O motor de sinais deve funcionar no servidor, sem depender de
            # /api/snapshot ou de o navegador estar aberto. O snapshot continua
            # apenas como leitura da interface.
            def _round_signal_step():
                try:
                    sig_step()
                    cus = custom_state()
                    custom_step(cus)
                    an = analytics()
                    pre_step(an, cus)
                except Exception as e:
                    with lock:
                        state["tg_last"] = {
                            "ok": False,
                            "msg": f"erro no processamento de sinais: {e}",
                            "at": datetime.now(timezone.utc).isoformat()
                        }
                        state["version"] += 1
            threading.Thread(target=_round_signal_step, daemon=True).start()
        if notify and state["ready"]:
            threading.Thread(target=notify_round, args=(p,), daemon=True).start()
    return added


# ---------------------------------------------------------------- estatísticas
def sides_all():
    c = conn()
    rows = c.execute("SELECT side FROM rounds ORDER BY id").fetchall()
    c.close()
    return [r["side"] for r in rows]


def compute_stats(seq):
    n = len(seq)
    counts = {k: seq.count(k) for k in ("PLAYER", "BANKER", "TIE")}
    pct = {k: (counts[k] / n * 100 if n else 0) for k in counts}
    cur_side, cur_len = (seq[-1], 0) if seq else (None, 0)
    for s in reversed(seq):
        if s == cur_side:
            cur_len += 1
        else:
            break
    longest = {"PLAYER": 0, "BANKER": 0, "TIE": 0}
    run, prev = 0, None
    for s in seq:
        run = run + 1 if s == prev else 1
        prev = s
        longest[s] = max(longest[s], run)
    since_tie = next((i for i, s in enumerate(reversed(seq)) if s == "TIE"), n)
    return {"total": n, "counts": counts, "pct": pct, "streak": {"side": cur_side, "len": cur_len},
            "longest": longest, "since_tie": since_tie, "theory": {k: v * 100 for k, v in P_THEORY.items()}}


def _opp(x):
    return "BANKER" if x == "PLAYER" else "PLAYER"


STRATS = [  # id, nome, descrição, gera sinal
    ("contra2", "Contra sequência de 2", "Com exatamente 2 iguais, entra no lado oposto", True),
    ("seguir2", "Seguir sequência de 2", "Com exatamente 2 iguais, entra no mesmo lado", True),
    ("contra3", "Contra sequência de 3", "Com exatamente 3 iguais, entra no lado oposto", True),
    ("seguir3", "Seguir sequência de 3", "Com exatamente 3 iguais, entra no mesmo lado", True),
    ("contra4", "Contra sequência de 4", "Com exatamente 4 iguais, entra no lado oposto", True),
    ("seguir4", "Seguir sequência de 4", "Com exatamente 4 iguais, entra no mesmo lado", True),
    ("contra5", "Contra sequência de 5+", "Com 5 ou mais iguais, entra no lado oposto", True),
    ("seguir5", "Seguir sequência de 5+", "Com 5 ou mais iguais, entra no mesmo lado", True),
    ("alt2", "Alternância curta", "Após P-B ou B-P, continua alternando", True),
    ("alt3", "Alternância de 3", "Após P-B-P (ou B-P-B), continua alternando", True),
    ("alt4", "Alternância longa", "Após 4+ trocas seguidas, continua alternando", True),
    ("dupla22", "Padrão 2x2", "Após P-P-B-B (ou B-B-P-P), entra no lado que abriu o par", True),
    ("ultimo", "Repetir o último", "Entra sempre no último lado que saiu", False),
    ("maj10", "Maioria das últimas 10", "Entra no lado que mais saiu nas últimas 10", False),
    ("contramaj10", "Contra a maioria (10)", "Entra no lado que menos saiu nas últimas 10", False),
    ("tie10", "Tie após 10 sem empate", "Entra no Tie quando o empate está atrasado 10+", True),
    ("tie15", "Tie após 15 sem empate", "Entra no Tie quando o empate está atrasado 15+", True),
]
SIG_OF = {sid: sg for sid, _, _, sg in STRATS}
LAM = 0.5 ** (1 / 20)  # memória do motor: o peso de cada entrada cai pela metade a cada 20 entradas


def _base(sid):
    if sid.startswith("tie"): return P_THEORY["TIE"]
    return P_THEORY["PLAYER"] + P_THEORY["TIE"] if CFG.get("tie_as_green", True) else 0.5


def _z(st, sid):
    if st["dn"] < 10: return None
    b = _base(sid)
    return (st["dw"] / st["dn"] - b) / math.sqrt(b * (1 - b) / st["dn"])


def _rep(nt):  # taxa de repetição entre lados consecutivos (Tie ignorado)
    return sum(1 for x, y in zip(nt, nt[1:]) if x == y) / (len(nt) - 1) if len(nt) > 1 else None


def _regime(nt):
    r30, r20, rp = _rep(nt[-31:]), _rep(nt[-21:]), _rep(nt[-61:-20])
    ch = len(nt) >= 50 and r20 is not None and rp is not None and abs(r20 - rp) >= 0.25
    lab = "sem dados" if r30 is None else "Em sequências" if r30 > 0.60 else "Alternada" if r30 < 0.40 else "Misto"
    return {"label": lab, "rep": r30, "rep20": r20, "rep_prev": rp, "change": ch}


def pick_signal(sig, S, i, eres, change):
    """Escolhe o sinal olhando só para a forma RECENTE de cada padrão (memória com decaimento).
    Padrão sem forma, em queda (3 reds seguidos) ou contradito por outro mais forte não entra."""
    last = [r for j, r in eres[-8:] if j >= i - 30]
    caution = change or (len(last) >= 6 and last.count("r") >= 5)  # mesa mudou ou o próprio motor está errando: exige mais
    zmin = 1.9 if caution else 1.3
    by = {}
    for sid, bet in sig.items():
        if not SIG_OF[sid]: continue
        z = _z(S[sid], sid)
        if z is None or z < zmin or S[sid]["form"][-3:] == "rrr": continue
        by.setdefault(bet, []).append((z, sid))
    if not by: return None, caution, zmin
    rk = sorted(by.items(), key=lambda kv: -sum(z for z, _ in kv[1]))
    if len(rk) > 1 and sum(z for z, _ in rk[0][1]) - sum(z for z, _ in rk[1][1]) < 0.5: return None, caution, zmin
    bet, lst = rk[0]; lst.sort(reverse=True)
    return {"bet": bet, "main": lst[0][1], "sids": [s for _, s in lst], "z": round(lst[0][0], 2)}, caution, zmin


def _sig_at(t):
    """Sinais de gatilho (só os que dependem de sequência/alternância/2x2/Tie) a partir de um estado (ss, sl, alt, last, gap, h4)."""
    ss, sl, alt, last, gap, h4 = t["ss"], t["sl"], t["alt"], t["last"], t["gap"], t["h4"]
    sig = {}
    if ss and sl == 2: sig["contra2"] = _opp(ss); sig["seguir2"] = ss
    if ss and sl == 3: sig["contra3"] = _opp(ss); sig["seguir3"] = ss
    if ss and sl == 4: sig["contra4"] = _opp(ss); sig["seguir4"] = ss
    if ss and sl >= 5: sig["contra5"] = _opp(ss); sig["seguir5"] = ss
    if alt == 2 and last: sig["alt2"] = _opp(last)
    if alt == 3 and last: sig["alt3"] = _opp(last)
    if alt >= 4 and last: sig["alt4"] = _opp(last)
    if len(h4) == 4 and h4[0] == h4[1] != h4[2] == h4[3]: sig["dupla22"] = h4[0]
    if gap >= 10: sig["tie10"] = "TIE"
    if gap >= 15: sig["tie15"] = "TIE"
    return sig


def _advance(t, o):
    """Simula o estado (ss, sl, alt, last, gap, h4) depois de uma rodada hipotética com resultado o."""
    if o == "TIE":
        return {"ss": None, "sl": 0, "alt": 0, "last": None, "gap": 0, "h4": []}
    ss, sl, alt, last, gap, h4 = t["ss"], t["sl"], t["alt"], t["last"], t["gap"], list(t["h4"])
    sl = sl + 1 if o == ss else 1
    alt = alt + 1 if (last and o != last) else 1
    gap += 1
    h4 = (h4 + [o])[-4:]
    return {"ss": o, "sl": sl, "alt": alt, "last": o, "gap": gap, "h4": h4}


def pre_engine_signal(trail, S, zmin):
    """Pré-aviso: dos padrões fixos, qual completaria o gatilho se a PRÓXIMA rodada sair de um jeito específico.
    Só considera padrões com forma recente decente (mesmo filtro do motor ao vivo)."""
    cur = _sig_at(trail)
    best = None
    for o in ("PLAYER", "BANKER", "TIE"):
        nxt = _sig_at(_advance(trail, o))
        for sid, bet in nxt.items():
            if sid in cur or not SIG_OF.get(sid):
                continue
            z = _z(S[sid], sid)
            if z is None or z < zmin or S[sid]["form"][-3:] == "rrr":
                continue
            if best is None or z > best[0]:
                best = (z, sid, bet, o)
    if not best:
        return None
    z, sid, bet, need = best
    name = next(nm for i, nm, _, _ in STRATS if i == sid)
    return {"sid": sid, "name": name, "bet": bet, "need": need, "z": round(z, 2)}


def evaluate(seq, hours):
    R = {sid: {"g": 0, "r": 0, "p": 0, "units": 0.0, "h": [[0, 0], [0, 0]], "hr": {}, "run": 0, "maxrun": 0} for sid, *_ in STRATS}
    S = {sid: {"dw": 0.0, "dn": 0.0, "form": ""} for sid, *_ in STRATS}
    feed, n, mid = [], len(seq), len(seq) // 2
    ss, sl, alt, last, gap, win, h4, nt = None, 0, 0, None, 0, [], [], []
    eng, eres = {"g": 0, "r": 0, "exp": 0.0, "var": 0.0, "form": ""}, []
    for i in range(n + 1):
        sig = {}
        if ss and sl == 2: sig["contra2"] = _opp(ss); sig["seguir2"] = ss
        if ss and sl == 3: sig["contra3"] = _opp(ss); sig["seguir3"] = ss
        if ss and sl == 4: sig["contra4"] = _opp(ss); sig["seguir4"] = ss
        if ss and sl >= 5: sig["contra5"] = _opp(ss); sig["seguir5"] = ss
        if alt == 2 and last: sig["alt2"] = _opp(last)
        if alt == 3 and last: sig["alt3"] = _opp(last)
        if alt >= 4 and last: sig["alt4"] = _opp(last)
        if last: sig["ultimo"] = last
        if len(win) == 10:
            pc, bc = win.count("PLAYER"), win.count("BANKER")
            if pc != bc:
                sig["maj10"] = "PLAYER" if pc > bc else "BANKER"; sig["contramaj10"] = _opp(sig["maj10"])
        if len(h4) == 4 and h4[0] == h4[1] != h4[2] == h4[3]: sig["dupla22"] = h4[0]
        if gap >= 10: sig["tie10"] = "TIE"
        if gap >= 15: sig["tie15"] = "TIE"
        reg = _regime(nt)
        pick, caution, zmin = pick_signal(sig, S, i, eres, reg["change"])
        if i == n:
            trail = {"ss": ss, "sl": sl, "alt": alt, "last": last, "gap": gap, "h4": list(h4)}
            return R, sig, feed, {"S": S, "pick": pick, "caution": caution, "zmin": zmin, "regime": reg, "wf": eng, "trail": trail}
        o = seq[i]
        if pick:
            b = pick["bet"]
            er = "g" if (o == b or (o == "TIE" and b != "TIE" and CFG.get("tie_as_green", True))) else \
                 "p" if (o == "TIE" and b != "TIE" and CFG["tie_pushes_pb"]) else "r"
            if er != "p":
                eres.append((i, er)); eng[er] += 1; bb = _base(pick["main"]); eng["exp"] += bb; eng["var"] += bb * (1 - bb)
                eng["form"] = (eng["form"] + er)[-30:]
        for sid, bet in sig.items():
            d = R[sid]
            d.setdefault("ent", []).append((i, bet))
            if bet == "TIE":
                res = "g" if o == "TIE" else "r"
                d["units"] += CFG["tie_payout"] if res == "g" else -1
            elif o == bet:
                res = "g"; d["units"] += 1
            elif o == "TIE" and CFG.get("tie_as_green", True):
                res = "g"; d["units"] += 1  # empate protegido conta como green
            elif o == "TIE" and CFG["tie_pushes_pb"]:
                res = "p"
            else:
                res = "r"; d["units"] -= 1
            d[res] += 1
            if res != "p":
                st = S[sid]; st["dw"] = st["dw"] * LAM + (res == "g"); st["dn"] = st["dn"] * LAM + 1; st["form"] = (st["form"] + res)[-8:]
                d["h"][0 if i < mid else 1][0 if res == "g" else 1] += 1
                hh = d["hr"].setdefault(hours[i], [0, 0]); hh[0 if res == "g" else 1] += 1
                d["run"] = 0 if res == "g" else d["run"] + 1; d["maxrun"] = max(d["maxrun"], d["run"])
            if SIG_OF[sid]:
                feed.append({"i": i, "sid": sid, "bet": bet, "res": res, "out": o})
        # atualizar estado
        if o == "TIE":
            ss, sl, alt, last, gap, h4 = None, 0, 0, None, 0, []
            win.append(o)
        else:
            sl = sl + 1 if o == ss else 1
            alt = alt + 1 if (last and o != last) else 1
            ss, last = o, o
            gap += 1; win.append(o); h4 = (h4 + [o])[-4:]; nt = (nt + [o])[-80:]
        win = win[-10:]


def gale_rec(seq, ents, sid):
    """Viabilidade do gale (até G2) medida no histórico: com que frequência o mesmo lado falha 2 ou 3 rodadas seguidas."""
    tg = CFG.get("tie_as_green", True); c = [0, 0, 0, 0]
    for i, b in ents:
        if b == "TIE" or i + 2 >= len(seq):
            continue
        for k in range(3):
            if seq[i + k] == b or (seq[i + k] == "TIE" and tg):
                c[k] += 1; break
        else:
            c[3] += 1
    n = sum(c); cap = max(0, min(2, int(CFG.get("max_gale", 2))))
    if sid.startswith("tie"): m, why = 0, "Tie não usa gale"
    elif cap == 0: m, why = 0, "gale desligado nas configurações"
    elif n < 30: m, why = 0, f"amostra pequena ({n} entradas)"
    elif c[3] / n <= 0.10: m, why = 2, ""
    elif (c[2] + c[3]) / n <= 0.22: m, why = 1, ""
    else: m, why = 0, "perdas seguidas acima do normal neste padrão"
    return {"max": min(m, cap), "reason": why, "wins": c[:3], "fail": c[3], "n": n}


def _ad(eng, sid):
    st = eng["S"][sid]; z = _z(st, sid)
    if z is None: s = "observando"
    elif z >= eng["zmin"] and st["form"][-3:] != "rrr": s = "ativa"
    elif z <= -1.0 or (st["form"][-2:] == "rr" and z < 0): s = "suspensa"
    else: s = "observando"
    return {"hit": st["dw"] / st["dn"] * 100 if st["dn"] else None, "n": round(st["dn"], 1), "z": z, "form": st["form"], "status": s}


def analytics():
    c = conn()
    rows = c.execute("SELECT id,side,ts FROM rounds ORDER BY id").fetchall()
    c.close()
    key = (len(rows), rows[-1]["id"] if rows else 0)
    if _cache.get("k") == key:
        return _cache["v"]
    seq = [r["side"] for r in rows]
    hours = []
    for r in rows:
        try:
            hours.append(datetime.fromisoformat(r["ts"]).astimezone(TZ).hour)
        except Exception:
            hours.append(0)
    R, active, feed, eng = evaluate(seq, hours)
    out = []
    for sid, name, desc, sg in STRATS:
        d = R[sid]; tr = d["g"] + d["r"]
        p0 = P_THEORY["TIE"] if sid.startswith("tie") else 0.5
        hit = d["g"] / tr * 100 if tr else None
        z = ((d["g"] / tr - p0) / math.sqrt(p0 * (1 - p0) / tr)) if tr >= 10 else None
        hf = lambda h: (h[0] / (h[0] + h[1]) * 100 if h[0] + h[1] else None)
        out.append({"id": sid, "sig": sg, "name": name, "desc": desc, "entries": tr + d["p"], "green": d["g"], "red": d["r"],
                    "push": d["p"], "hit": hit, "units": round(d["units"], 2), "z": z, "baseline": p0 * 100,
                    "half": [hf(d["h"][0]), hf(d["h"][1])], "maxrun": d["maxrun"], "active": active.get(sid), "gale": gale_rec(seq, d.get("ent", []), sid), "ad": _ad(eng, sid),
                    "hours": {str(h): v for h, v in sorted(d["hr"].items())}})
    w = seq[-30:]; pn, bn, tn = w.count("PLAYER"), w.count("BANKER"), w.count("TIE")
    ch = sum(1 for a, b in zip(w, w[1:]) if a != b and "TIE" not in (a, b)) / max(1, len(w) - 1)
    since_tie = next((i for i, x in enumerate(reversed(seq)) if x == "TIE"), len(seq))
    hm = {}
    for x, h in zip(seq, hours):
        e = hm.setdefault(str(h), {"n": 0, "PLAYER": 0, "BANKER": 0, "TIE": 0}); e["n"] += 1; e[x] += 1
    w_ = eng["wf"]; nwf = w_["g"] + w_["r"]
    pk = eng["pick"]
    engine = {"regime": eng["regime"], "caution": eng["caution"], "zmin": eng["zmin"],
              "pick": pk if CFG.get("engine_signals", False) else None,
              "pre": None if (pk or not CFG.get("engine_signals", False)) else pre_engine_signal(eng["trail"], eng["S"], eng["zmin"]),
              "wf": {"n": nwf, "g": w_["g"], "r": w_["r"], "hit": w_["g"] / nwf * 100 if nwf else None,
                     "expected": w_["exp"] / nwf * 100 if nwf else None,
                     "z": (w_["g"] - w_["exp"]) / math.sqrt(w_["var"]) if w_["var"] > 0 else None, "form": w_["form"]}}
    v = {"engine": engine, "stats": compute_stats(seq), "strategies": out, "feed": feed[-14:][::-1], "hours_market": hm,
         "sentiment": {"n": len(w), "player": pn, "banker": bn, "tie": tn,
                       "bias": (pn - bn) / (pn + bn) * 100 if pn + bn else 0, "choppy": ch * 100, "since_tie": since_tie}}
    _cache.update(k=key, v=v)
    return v


_cache = {}

# ---------------------------------------------------------------- motor de sinais (servidor: funciona com o navegador fechado)
SIGF = os.path.join(BASE_DIR, "signals.json")
TELEGRAM_SCOREF = os.path.join(BASE_DIR, "telegram_stats.json")
TELEGRAM_SCORE = {"g": 0, "r": 0, "t": 0, "run": 0, "lv": [0, 0, 0]}
try:
    with open(TELEGRAM_SCOREF, encoding="utf-8") as f:
        TELEGRAM_SCORE.update(json.load(f))
except Exception:
    pass
if not isinstance(TELEGRAM_SCORE.get("lv"), list) or len(TELEGRAM_SCORE["lv"]) < 3:
    TELEGRAM_SCORE["lv"] = [0, 0, 0]


def _save_telegram_score():
    with open(TELEGRAM_SCOREF, "w", encoding="utf-8") as fstats:
        json.dump(TELEGRAM_SCORE, fstats, ensure_ascii=False)

SIG = {"n": None, "pend": None, "result": None, "score": {"g": 0, "r": 0, "t": 0, "run": 0, "lv": [0, 0, 0]}, "message_ids": []}
sig_lock = threading.Lock()
try:
    with open(SIGF, encoding="utf-8") as f:
        SIG["score"].update(json.load(f))
except Exception:
    pass


def _cur_signal(an):
    pk = an["engine"]["pick"]
    if not pk:
        return None
    by = {s["id"]: s for s in an["strategies"]}; m = by[pk["main"]]
    return {"bet": pk["bet"], "lvl": 0, "max": m["gale"]["max"], "why": m["gale"]["reason"], "regime": an["engine"]["regime"]["label"],
            "caution": an["engine"]["caution"], "z": pk["z"],
            "main": {"id": m["id"], "name": m["name"], "hit": m["hit"], "entries": m["entries"], "ahit": m["ad"]["hit"], "form": m["ad"]["form"]},
            "all": [{"id": i, "name": by[i]["name"], "bet": pk["bet"], "hit": by[i]["ad"]["hit"]} for i in pk["sids"]]}


def _res(bet, o):
    if o == bet or (o == "TIE" and bet != "TIE" and CFG.get("tie_as_green", True)): return "g"
    if o == "TIE" and bet != "TIE" and CFG["tie_pushes_pb"]: return "p"
    return "r"


def _acc():
    s = SIG["score"]; return f"{s['g'] / (s['g'] + s['r']) * 100:.0f}%" if s["g"] + s["r"] else "—"


def _pl(n, one, many):
    return f"{n} {one if n == 1 else many}"


def _entry_msg(p, an=None):
    b = p["bet"].upper()
    lvl = p["lvl"]
    # Cores visuais fixas no Telegram: Player = azul, Banker = vermelho, Tie = amarelo.
    emoji = {"P": "🔵", "PLAYER": "🔵", "B": "🔴", "BANKER": "🔴", "T": "🟡", "TIE": "🟡"}.get(b, "")
    name = {"P": "🔵PLAYER🔵", "🔵PLAYER🔵": "🔵PLAYER🔵", "B": "🔴BANKER🔴", "🔴BANKER🔴": "🔴BANKER🔴", "T": "TIE", "TIE": "TIE"}.get(b, b.title())
    title = f"GALE {lvl}" if lvl else "POSSÍVEL ENTRADA"
    L = [f"{emoji} <b>{title}</b>  {name} {emoji}", ""]
    if b not in ("TIE", "T") and CFG.get("tie_as_green", True):
        L.append("🟡 Proteger empate")
    if p["max"] and not p["lvl"]:
        L.append(f"♻️ Gale até {p['max']}")
    return "\n".join(L)


def _lv_label(lvl):
    return "DE PRIMEIRA" if not lvl else f"G{lvl}"


def _result_msg(res=None):
    # Estatísticas globais do Telegram (soma padrões do sistema + "Meus
    # padrões"): título e % de acerto na mesma linha, resto logo abaixo,
    # sem divisor no meio, e sempre com o nível que mais acerta.
    s = TELEGRAM_SCORE
    tot = s["g"] + s["r"]
    acc = f"{s['g'] / tot * 100:.0f}% de acerto" if tot else "sem histórico ainda"
    green_word = "green seguido" if s["run"] == 1 else "greens seguidos"
    red_word = "red" if s["r"] == 1 else "reds"
    tie_word = "empate" if s["t"] == 1 else "empates"
    lv = s.get("lv") or [0, 0, 0]
    if any(lv):
        best = max(range(len(lv)), key=lambda i: lv[i])
        best_line = f"🏆 Mais greens: <b>{_lv_label(best)}</b> ({lv[best]})"
    else:
        best_line = "🏆 Mais greens: sem histórico ainda"
    return (
        f"📊 <b>ESTATÍSTICAS</b>  {acc}\n"
        f"🟢 {s['run']} {green_word}\n"
        f"🔴 {s['r']} {red_word}\n"
        f"🟡 {s['t']} {tie_word}\n"
        f"{best_line}"
    )


def _result_header(res, lvl=None):
    # Cabeçalho visual do resultado em texto (sem imagem), tudo em uma
    # única linha, com bolinhas coloridas e o nível em que bateu (de
    # primeira, G1 ou G2).
    label = f" {_lv_label(lvl)}" if lvl is not None else ""
    if res == "g":
        return f"🟢🟢🟢 <b>GREEN{label}</b> 🟢🟢🟢 · Bateu certo! ✅"
    if res == "t":
        return f"🟡🟡🟡 <b>EMPATE PROTEGIDO{label}</b> 🟡🟡🟡 · Bateu certo! ✅"
    return "🔴🔴🔴 <b>RED</b> 🔴🔴🔴 · Não bateu dessa vez ❌"


def sig_step():
    if not state["ready"]:
        return
    with sig_lock:
        c = conn(); n = c.execute("SELECT COUNT(*) FROM rounds").fetchone()[0]
        row = c.execute("SELECT side FROM rounds ORDER BY id DESC LIMIT 1").fetchone(); c.close()
        if SIG["n"] == n or not row:
            return
        an = analytics(); cur = _cur_signal(an); prev, p = SIG["n"], SIG["pend"]; SIG["n"] = n
        msgs, on = [], CFG.get("telegram_signals", True)
        # Qualquer entrada/Gale pendente da rodada anterior deixa de ser válida
        # assim que uma nova rodada chega. Apaga a mensagem correspondente.
        if prev is not None and n != prev:
            old_ids = list(SIG.get("message_ids", []))
            SIG["message_ids"] = []
            for mid in old_ids:
                tg_delete(mid)
        if prev is None or n != prev + 1 or not p:
            SIG["pend"] = cur
            # Envia a entrada para o Telegram mesmo quando o bot acabou de
            # iniciar (prev is None) com uma entrada já pendente. Antes,
            # essa condição bloqueava o primeiro envio e o Telegram acabava
            # mostrando só o GREEN/RED quando a rodada seguinte chegava,
            # sem nunca ter mostrado a "POSSÍVEL ENTRADA" correspondente.
            if cur and on: msgs.append(("text", _entry_msg(cur, an)))
        else:
            o, b, lvl, sc = row["side"], p["bet"], p["lvl"], SIG["score"]
            res, final = _res(b, o), True
            if res == "g":
                sc["g"] += 1; sc["run"] += 1; sc["lv"][lvl] += 1; sc["t"] += 1 if o == "TIE" and b != "TIE" else 0
            elif res == "r":
                if lvl < p["max"]: final = False
                else: sc["r"] += 1; sc["run"] = 0
            others = [{"name": x["name"], "bet": x["bet"], "res": _res(x["bet"], o)} for x in p["all"] if x["id"] != p["main"]["id"]]
            SIG["result"] = {"id": n, "res": res, "final": final, "lvl": lvl, "bet": b, "out": o, "name": p["main"]["name"], "others": others}
            if final:
                SIG["pend"] = cur
                if on:
                    # Estatística do Telegram é GLOBAL: soma tanto os padrões
                    # fixos do sistema quanto os "Meus padrões" do construtor.
                    tie_protect = res == "g" and o == "TIE" and b != "TIE"
                    disp_res = "t" if tie_protect else res
                    if res == "g":
                        TELEGRAM_SCORE["t" if tie_protect else "g"] += 1
                        TELEGRAM_SCORE["run"] += 1
                        TELEGRAM_SCORE["lv"][lvl] += 1
                    else:
                        TELEGRAM_SCORE["r"] += 1; TELEGRAM_SCORE["run"] = 0
                    _save_telegram_score()
                    # Resultado em texto estilizado (sem imagem, tudo numa
                    # linha), seguido do bloco de estatísticas em mensagem própria.
                    msgs.append(("text", _result_header(disp_res, lvl)))
                    msgs.append(("text", _result_msg()))
                if cur: msgs.append(("text", _entry_msg(cur)))
            else:
                p["lvl"] = lvl + 1
                msgs.append(("text", _entry_msg(p)))
            if not on: msgs = []
            with open(SIGF, "w", encoding="utf-8") as f: json.dump(sc, f)
        with lock:
            state["version"] += 1
        if msgs:
            def _send_msgs():
                for kind, message in msgs:
                    ok, info = (tg_send_photo if kind == "photo" else tg_send)(message)
                    if ok and isinstance(info, dict) and info.get("message_id"):
                        # Somente mensagens de entrada/Gale ficam marcadas para
                        # exclusão na próxima rodada. Resultado/estatística fica.
                        if "POSSÍVEL ENTRADA" in message or "GALE" in message:
                            SIG["message_ids"].append(info["message_id"])
            threading.Thread(target=_send_msgs, daemon=True).start()


def snapshot():
    c = conn()
    rows = c.execute("SELECT id,ts,side,ps,bs FROM rounds ORDER BY id DESC LIMIT 200").fetchall()
    lastraw = c.execute("SELECT raw FROM rounds ORDER BY id DESC LIMIT 1").fetchone()
    c.close()
    recent = [dict(r) for r in reversed(rows)]
    if recent and lastraw:
        try:
            pr = parse_round(json.loads(lastraw["raw"]))
            if pr:
                recent[-1].update(ps=pr["ps"], bs=pr["bs"], mult=pr["mult"])
        except Exception:
            pass
    with lock:
        st = {k: v for k, v in state.items()}
        raws = list(raw_log)
    st["uptime"] = int(time.time() - st["started"])
    sig_step()
    an = analytics()
    # Os sinais Telegram já são processados quando uma nova rodada é gravada
    # em record(). Aqui apenas calculamos o estado para a interface.
    cus = custom_state()
    return {"custom": cus, "signal": {"pend": SIG["pend"], "result": SIG["result"], "score": SIG["score"]}, "state": st, "stats": an["stats"], "analytics": an, "recent": recent, "raw": raws,
            "config": {"tie_payout": CFG["tie_payout"], "tie_pushes_pb": CFG["tie_pushes_pb"], "tie_as_green": CFG.get("tie_as_green", True), "engine_signals": CFG.get("engine_signals", False), "round_id": CFG["round_id"], "history_url": HISTORY_URL, "live_url": LIVE_URL}}


# ---------------------------------------------------------------- Telegram
def _tg_state(ok, msg):
    with lock:
        state["tg_last"] = {"ok": ok, "msg": msg, "at": datetime.now(timezone.utc).isoformat()}
        state["version"] += 1


def tg_send(text):
    tok, chat = CFG["telegram_token"], CFG["telegram_chat_id"]
    if not (tok and chat):
        return False, "Telegram não configurado (config.json)"
    try:
        r = requests.post(f"https://api.telegram.org/bot{tok}/sendMessage",
                          json={"chat_id": chat, "text": text, "parse_mode": "HTML"}, timeout=10)
        ok = r.ok
        if ok:
            data = r.json().get("result") or {}
            msg = {"message_id": data.get("message_id")}
        else:
            msg = f"HTTP {r.status_code}: {r.text[:120]}"
    except Exception as e:
        ok, msg = False, str(e)
    _tg_state(ok, "enviado" if ok else msg)
    return ok, msg


def tg_send_photo(caption, filename="green.png"):
    tok, chat = CFG["telegram_token"], CFG["telegram_chat_id"]
    if not (tok and chat):
        return False, "Telegram não configurado (config.json)"
    path = os.path.join(BASE_DIR, "assets", filename)
    if not os.path.isfile(path):
        return tg_send(caption)
    try:
        with open(path, "rb") as photo:
            r = requests.post(f"https://api.telegram.org/bot{tok}/sendPhoto",
                              data={"chat_id": chat, "caption": caption, "parse_mode": "HTML"},
                              files={"photo": photo}, timeout=20)
        ok = r.ok
        if ok:
            data = r.json().get("result") or {}
            msg = {"message_id": data.get("message_id")}
        else:
            msg = f"HTTP {r.status_code}: {r.text[:120]}"
    except Exception as e:
        ok, msg = False, str(e)
    _tg_state(ok, "enviado" if ok else msg)
    return ok, msg


def tg_delete(message_id):
    """Apaga uma mensagem do bot no chat. Usado para remover o pré-alerta quando chega a próxima rodada."""
    tok, chat = CFG["telegram_token"], CFG["telegram_chat_id"]
    if not (tok and chat and message_id):
        return False
    try:
        r = requests.post(f"https://api.telegram.org/bot{tok}/deleteMessage",
                          json={"chat_id": chat, "message_id": int(message_id)}, timeout=10)
        return r.ok
    except Exception:
        return False


def tg_edit(message_id, text):
    """Edita uma mensagem existente do bot."""
    tok, chat = CFG["telegram_token"], CFG["telegram_chat_id"]
    if not (tok and chat and message_id):
        return False
    try:
        r = requests.post(f"https://api.telegram.org/bot{tok}/editMessageText",
                          json={"chat_id": chat, "message_id": int(message_id),
                                "text": text, "parse_mode": "HTML"}, timeout=10)
        return r.ok
    except Exception:
        return False


def notify_round(p):
    if not CFG["telegram_all_rounds"]:
        return  # por padrão o Telegram recebe só sinais de entrada e resultado
    seq = sides_all(); st = compute_stats(seq)
    sc = f" · {p['ps']} x {p['bs']}" if p["ps"] is not None and p["bs"] is not None else ""
    tg_send(f"{EMOJI[p['side']]} <b>{p['side'].title()}</b>{sc}\n{''.join(EMOJI[s] for s in seq[-10:])}\n"
            f"Player {st['pct']['PLAYER']:.1f}% · Banker {st['pct']['BANKER']:.1f}% · Tie {st['pct']['TIE']:.1f}% ({st['total']} rodadas)")


# ---------------------------------------------------------------- coleta
def poll_history():
    first = True
    while True:
        try:
            lim = min(200, int(CFG["history_limit"])) if first else 60
            r = requests.get(HISTORY_URL, params={"limit": lim, "timezone": "Atlantic/Cape_Verde"}, headers=HEADERS, timeout=25)
            if first and not r.ok and lim > 200:  # a API pode recusar limites grandes: cai para 200
                r = requests.get(HISTORY_URL, params={"limit": 200, "timezone": "Atlantic/Cape_Verde"}, headers=HEADERS, timeout=25)
            with lock:
                state.update(history_status=r.status_code, history_ok=r.ok,
                             history_error=None if r.ok else f"HTTP {r.status_code}: {r.text[:100]}")
            if r.ok:
                data = r.json()
                if first:
                    log_raw("history", data if len(json.dumps(data)) < 1800 else (unwrap_list(data) or data)[:2])
                items = unwrap_list(data) or [data]
                parsed = [(parse_round(i), i) for i in items]
                if parsed and all(p and p["ts"] for p, _ in parsed):
                    parsed.sort(key=lambda x: x[0]["ts"])
                elif CFG["history_newest_first"]:
                    parsed.reverse()
                for _, item in parsed:
                    record(item, "history", notify=not first)
                if first: state["history_loaded"] = len(parsed)
                first = False
                with lock:
                    state["ready"] = True
        except Exception as e:
            with lock:
                state.update(history_ok=False, history_error=str(e)[:160])
        time.sleep(max(1, CFG["poll_seconds"]))


def listen_live():
    while True:
        try:
            with requests.get(LIVE_URL, stream=True, timeout=(10, 90),
                              headers={**HEADERS, "Accept": "text/event-stream", "Cache-Control": "no-cache"}) as r:
                with lock:
                    state.update(live_status=r.status_code, live_connected=r.ok,
                                 live_error=None if r.ok else f"HTTP {r.status_code}")
                r.raise_for_status()
                buf = []
                for line in r.iter_lines(decode_unicode=True):
                    line = (line or "").strip()
                    if not line:
                        if buf:
                            txt = "\n".join(buf); buf = []
                            try:
                                payload = json.loads(txt)
                            except Exception:
                                payload = txt
                            log_raw("live", payload)
                            for item in (payload if isinstance(payload, list) else [payload]):
                                record(item, "live")
                        continue
                    if line.startswith("data:"):
                        buf.append(line[5:].lstrip())
                    elif line[0] in "{[":  # NDJSON sem prefixo "data:"
                        try:
                            payload = json.loads(line)
                            log_raw("live", payload)
                            for item in (payload if isinstance(payload, list) else [payload]):
                                record(item, "live")
                        except Exception:
                            pass
        except Exception as e:
            with lock:
                state.update(live_connected=False, live_error=str(e)[:160])
        time.sleep(3)


# ---------------------------------------------------------------- backtest
def run_bt(seq, n, mode, mart, tie, cap=6):
    stake, prof, peak, mdd = 1, 0, 0, 0
    bets = wins = losses = pushes = lstreak = maxl = 0
    eq = [0]
    ss, sl = None, 0
    for s in seq:
        if ss and sl >= n:
            target = ss if mode == "follow" else ("BANKER" if ss == "PLAYER" else "PLAYER")
            bets += 1
            if s == target:
                prof += stake; wins += 1; stake = 1; lstreak = 0
            elif s == "TIE" and tie == "push":
                pushes += 1
            else:
                prof -= stake; losses += 1; lstreak += 1; maxl = max(maxl, lstreak)
                stake = min(stake * 2, 2 ** cap) if mart else 1
            eq.append(prof); peak = max(peak, prof); mdd = max(mdd, peak - prof)
        if s == "TIE":
            ss, sl = None, 0
        elif s == ss:
            sl += 1
        else:
            ss, sl = s, 1
    return {"bets": bets, "wins": wins, "losses": losses, "pushes": pushes, "profit": prof,
            "max_drawdown": mdd, "max_loss_streak": maxl, "equity": eq}


@app.get("/api/backtest")
def api_backtest():
    n = max(1, min(int(request.args.get("n", 3)), 10))
    mode = request.args.get("mode", "opposite")
    mart = request.args.get("mart", "0") == "1"
    tie = request.args.get("tie", "push")
    seq = sides_all()
    real = run_bt(seq, n, mode, mart, tie)
    rng, sims = random.Random(7), []
    pool, w = list(P_THEORY), list(P_THEORY.values())
    for _ in range(300):
        sims.append(run_bt(rng.choices(pool, w, k=len(seq)), n, mode, mart, tie)["profit"])
    pct = round(sum(1 for x in sims if x <= real["profit"]) / len(sims) * 100) if sims else None
    return jsonify({**real, "rounds": len(seq), "percentile": pct,
                    "sim_median": sorted(sims)[len(sims) // 2] if sims else None})


# ---------------------------------------------------------------- rotas
# ---------------------------------------------------------------- padrões do utilizador (construtor por bolinhas)
CUSTOM_F = os.path.join(BASE_DIR, "custom.json")
CUS = {"list": [], "n": None, "keys": {}, "message_ids": {}}
cus_lock = threading.Lock()
SYM = {"PLAYER": "P", "BANKER": "B", "TIE": "T"}
SIDE = {"P": "PLAYER", "B": "BANKER", "T": "TIE"}
try:
    with open(CUSTOM_F, encoding="utf-8") as f:
        CUS["list"] = json.load(f)
except Exception:
    pass


def save_custom():
    with open(CUSTOM_F, "w", encoding="utf-8") as f:
        json.dump(CUS["list"], f, ensure_ascii=False, indent=1)


FDEF = {"trend": "off", "tw": 20, "ts": 55, "rhythm": "any", "stop": 0, "pause": 10, "cool": 0, "h0": None, "h1": None}


def clean_filters(f):
    f = f if isinstance(f, dict) else {}

    def pick(k, opts):
        try:
            v = type(opts[0])(f.get(k, FDEF[k]))
        except Exception:
            return FDEF[k]
        return v if v in opts else FDEF[k]
    out = {"trend": pick("trend", ("off", "not_against", "with")), "tw": pick("tw", (10, 20, 30)), "ts": pick("ts", (55, 60, 65)),
           "rhythm": pick("rhythm", ("any", "seq", "alt")), "stop": pick("stop", (0, 2, 3, 4, 5)),
           "pause": pick("pause", (5, 10, 20, 30)), "cool": pick("cool", (0, 1, 2, 3, 5, 10))}
    try:
        h0, h1 = int(f.get("h0")), int(f.get("h1"))
        if not (0 <= h0 <= 23 and 0 <= h1 <= 23):
            h0 = h1 = None
    except Exception:
        h0 = h1 = None
    out.update(h0=h0, h1=h1)
    return out


def clean_custom(d):
    pat = [x for x in (d.get("pattern") or []) if x in ("P", "B", "T", "*")]
    bet = d.get("bet")
    if not 1 <= len(pat) <= 8 or bet not in ("P", "B", "T", "same", "opp"):
        return None
    name = (str(d.get("name") or "").strip() or " ".join(pat))[:40]
    return {"name": name, "pattern": pat, "bet": bet, "gale": max(0, min(2, int(d.get("gale") or 0))), "on": bool(d.get("on", True)),
            "f": clean_filters(d.get("f"))}


def _trend(s, i, w, strength):
    win = [x for x in s[max(0, i - w):i] if x != "T"]
    p, b = win.count("P"), win.count("B"); tot = p + b
    if tot < max(6, w // 3) or p == b:
        return None
    return ("P" if p > b else "B") if max(p, b) / tot >= strength else None


def _rhythm(s, i, w):
    win = [x for x in s[max(0, i - w):i] if x != "T"]
    if len(win) < 6:
        return "mix"
    ch = sum(1 for a, b in zip(win, win[1:]) if a != b) / (len(win) - 1)
    return "seq" if ch < 0.42 else "alt" if ch > 0.58 else "mix"


def _wilson(k, n, z=1.28):
    if not n:
        return 0.0
    ph = k / n
    return (ph + z * z / (2 * n) - z * math.sqrt(ph * (1 - ph) / n + z * z / (4 * n * n))) / (1 + z * z / n) * 100


def custom_eval(seq, st, hours=None, since=None):
    """Percorre o histórico: padrão + filtros -> entrada (com gale) -> green/red. Mede o total e a janela da escala (120)."""
    pat, k, gale = st["pattern"], len(st["pattern"]), int(st.get("gale", 0))
    F = {**FDEF, **(st.get("f") or {})}
    tg, push = CFG.get("tie_as_green", True), CFG.get("tie_pushes_pb", True)
    s = [SYM[x] for x in seq]; n = len(s)
    since = max(0, n - 120) if since is None else since
    ent, pend, blocked, rr, pause_until, next_ok = [], None, 0, 0, 0, 0
    for i in range(n + 1):
        if pend is None and i >= k and all(pt == "*" or pt == s[i - k + j] for j, pt in enumerate(pat)):
            ref, b = s[i - 1], st["bet"]
            bet = b if b in ("P", "B", "T") else (ref if b == "same" else {"P": "B", "B": "P"}.get(ref)) if ref in ("P", "B") else None
            if bet:
                why = None
                if i < pause_until or i < next_ok:
                    why = "pausa"
                elif F["h0"] is not None:
                    h = hours[i] if hours and i < len(hours) else datetime.now(TZ).hour
                    if not ((F["h0"] <= h <= F["h1"]) if F["h0"] <= F["h1"] else (h >= F["h0"] or h <= F["h1"])):
                        why = "hora"
                if not why and bet in ("P", "B") and F["trend"] != "off":
                    tr = _trend(s, i, F["tw"], F["ts"] / 100)
                    if (F["trend"] == "with" and tr != bet) or (F["trend"] == "not_against" and tr not in (None, bet)):
                        why = "tendência"
                if not why and F["rhythm"] != "any" and _rhythm(s, i, F["tw"]) != F["rhythm"]:
                    why = "ritmo"
                if why:
                    blocked += 1
                else:
                    pend = {"bet": bet, "lvl": 0, "start": i}
        if i == n:
            break
        if pend:
            o, b = s[i], pend["bet"]; res = None
            if o == b:
                res = "g"
            elif o == "T" and b != "T" and tg:
                res = "t"
            elif o == "T" and b != "T" and push:
                res = "p"
            elif pend["lvl"] < gale:
                pend["lvl"] += 1
            else:
                res = "r"
            if res:
                ent.append((i, res, pend["lvl"], b, pend["start"])); next_ok = i + 1 + F["cool"]
                if res == "r":
                    rr += 1
                    if F["stop"] and rr >= F["stop"]:
                        pause_until, rr = i + 1 + F["pause"], 0
                elif res != "p":
                    rr = 0
                pend = None

    def summ(E):
        g, t, r = (sum(1 for e in E if e[1] == x) for x in "gtr")
        return g, t, r, g + t + r
    g, t, r, tot = summ(ent)
    run = 0
    for e in reversed(ent):
        if e[1] == "p": continue
        if e[1] in "gt": run += 1
        else: break
    rec = [e for e in ent if e[1] != "p"][-30:]
    q = P_THEORY["TIE"] if st["bet"] == "T" else P_THEORY["PLAYER"] + (P_THEORY["TIE"] if tg else 0)
    p0 = 1 - (1 - q) ** (gale + 1)
    z = ((g + t) / tot - p0) / math.sqrt(p0 * (1 - p0) / tot) if tot >= 10 else None
    verdict = "poucos dados" if tot < 20 else "acima do acaso" if z >= 2 else "abaixo do acaso" if z <= -2 else "dentro do acaso"
    last = {"i": ent[-1][0], "res": ent[-1][1], "lvl": ent[-1][2], "bet": ent[-1][3], "out": s[ent[-1][0]]} if ent else None
    W = [e for e in ent if e[4] >= since]; wg, wt, wr, wtot = summ(W)
    sc = {"entries": wtot, "g": wg, "t": wt, "r": wr, "hit": (wg + wt) / wtot * 100 if wtot else None, "score": _wilson(wg + wt, wtot),
          "marks": [{"s": e[4] - since, "i": e[0] - since, "res": e[1], "lvl": e[2]} for e in W if e[1] != "p"], "since": since}
    forming = None  # pré-aviso: falta exatamente 1 rodada para o padrão completar
    if pend is None and k >= 2 and n >= k - 1:
        tail = s[n - (k - 1):]
        if all(pt == "*" or pt == tl for pt, tl in zip(pat[:-1], tail)):
            need = pat[-1]
            b = st["bet"]
            if b in ("P", "B", "T"): pred_bet = b
            elif need != "*": pred_bet = need if b == "same" else {"P": "B", "B": "P"}.get(need)
            else: pred_bet = None
            forming = {"need": need, "bet": SIDE.get(pred_bet)}
    return {"entries": tot, "g": g, "t": t, "r": r, "push": len(ent) - tot, "run": run, "hit": (g + t) / tot * 100 if tot else None,
            "recent": (sum(1 for e in rec if e[1] in "gt") / len(rec) * 100 if len(rec) >= 5 else None),
            "p0": p0 * 100, "z": z, "verdict": verdict, "last": last, "n": n, "blocked": blocked, "sc": sc, "f": F,
            "active": {"bet": SIDE[pend["bet"]], "lvl": pend["lvl"], "start": pend["start"]} if pend else None,
            "forming": forming}


def seq_hours():
    c = conn()
    rows = c.execute("SELECT side,ts FROM rounds ORDER BY id").fetchall()
    c.close()
    hrs = []
    for r in rows:
        try:
            hrs.append(datetime.fromisoformat(r["ts"]).astimezone(TZ).hour)
        except Exception:
            hrs.append(0)
    return [r["side"] for r in rows], hrs


def custom_state():
    seq, hrs = seq_hours()
    return [{"id": c["id"], "name": c["name"], "pattern": c["pattern"], "bet": c["bet"], "gale": c["gale"], "on": c.get("on", True),
             **custom_eval(seq, c, hrs)} for c in CUS["list"]]


def custom_step(rows):
    """Telegram (opcional): avisa o sinal e o resultado dos padrões do utilizador, uma vez por rodada.

    Estatística é GLOBAL (TELEGRAM_SCORE): soma tanto os padrões fixos do
    motor (sig_step) quanto os "Meus padrões" daqui, então o placar e o
    "mais greens" refletem o sistema inteiro. Cada padrão guarda o id da
    sua própria mensagem de entrada/Gale pendente em CUS["message_ids"]
    para poder apagá-la sozinho assim que deixa de valer (resultado saiu
    ou avançou para o próximo Gale).
    """
    if not state["ready"]:
        return
    n = rows[0]["n"] if rows else len(sides_all())
    on = CFG.get("telegram_signals", True)
    with cus_lock:
        prev = CUS["n"]; CUS["n"] = n
        first = prev is None
        if prev == n:
            return
        msgs = []  # cada item: ("result", texto) ou ("entry", pattern_id, texto)
        for c in rows:
            a, L = c["active"], c["last"]
            key = (c["id"], a["start"], a["lvl"]) if a else None
            if c["on"]:
                if not first and L and L["i"] == n - 1 and L["res"] != "p":
                    # O resultado chegou: a mensagem de entrada/Gale deste
                    # padrão não vale mais, apaga-a do Telegram.
                    old_mid = CUS["message_ids"].pop(c["id"], None)
                    if old_mid: tg_delete(old_mid)
                    if L["res"] == "r":
                        TELEGRAM_SCORE["r"] += 1
                        TELEGRAM_SCORE["run"] = 0
                    else:
                        TELEGRAM_SCORE[L["res"]] += 1  # "g" ou "t"
                        TELEGRAM_SCORE["run"] += 1
                        TELEGRAM_SCORE["lv"][L["lvl"]] += 1
                    _save_telegram_score()
                    if on:
                        msgs.append(("result", _result_header(L["res"], L["lvl"])))
                        msgs.append(("result", _result_msg()))
                if key and CUS["keys"].get(c["id"]) != key:
                    # Nova entrada ou avançou para o próximo Gale: a
                    # mensagem anterior deste padrão (se ainda existir)
                    # deixou de valer.
                    old_mid = CUS["message_ids"].pop(c["id"], None)
                    if old_mid: tg_delete(old_mid)
                    if on:
                        b = a["bet"]
                        bet_name = {"P": "PLAYER", "B": "BANKER", "T": "TIE"}.get(b, str(b).upper())
                        # Envia a entrada para o Telegram mesmo quando o bot acabou
                        # de iniciar com uma entrada já pendente. Antes, `first`
                        # bloqueava esse envio e o Telegram acabava mostrando apenas
                        # o GREEN quando a rodada seguinte chegava.
                        emoji = {"P": "🔵", "PLAYER": "🔵", "B": "🔴", "BANKER": "🔴", "T": "🟡", "TIE": "🟡"}.get(b, "⚪")
                        title = f"GALE {a['lvl']}" if a["lvl"] else "POSSÍVEL ENTRADA"
                        lines = [f"{emoji} <b>{title}</b>  {bet_name} {emoji}", ""]
                        if b in ("P", "B", "PLAYER", "BANKER") and CFG.get("tie_as_green", True):
                            lines.append("🟡 Proteger empate")
                        if a.get("lvl", 0) == 0 and c.get("gale", 0):
                            lines.append(f"♻️ Gale até {c['gale']}")
                        msgs.append(("entry", c["id"], "\n".join(lines)))
            else:
                # Padrão desligado: não deixa mensagem pendente presa no chat.
                old_mid = CUS["message_ids"].pop(c["id"], None)
                if old_mid: tg_delete(old_mid)
            CUS["keys"][c["id"]] = key
    if msgs:
        def _send_custom_msgs():
            for item in msgs:
                if item[0] == "entry":
                    _, pattern_id, message = item
                    ok, info = tg_send(message)
                    if ok and isinstance(info, dict) and info.get("message_id"):
                        CUS["message_ids"][pattern_id] = info["message_id"]
                else:
                    tg_send(item[1])
        threading.Thread(target=_send_custom_msgs, daemon=True).start()


PRE = {"eng": None, "cus": {}, "n": None, "message_ids": [], "anim_stop": None, "anim_thread": None}
_SIDEWORD = {"PLAYER": "Player", "BANKER": "Banker", "TIE": "Tie", "P": "Player", "B": "Banker", "T": "Tie", "*": "qualquer lado"}


def _stop_pre_animation():
    ev = PRE.get("anim_stop")
    if ev:
        ev.set()
    PRE["anim_stop"] = None
    PRE["anim_thread"] = None


def _animate_pre(message_id):
    ev = threading.Event()
    PRE["anim_stop"] = ev
    def run():
        frames = [".", "..", "..."]
        i = 0
        while not ev.wait(0.75):
            if message_id not in PRE.get("message_ids", []):
                break
            tg_edit(message_id, f"🔎 <b>Analisando a mesa</b>{frames[i % len(frames)]}")
            i += 1
    th = threading.Thread(target=run, daemon=True)
    PRE["anim_thread"] = th
    th.start()


def pre_step(an, rows):
    """Mostra apenas 'Analisando a mesa' com animação e remove ao chegar nova rodada."""
    if not state["ready"] or not CFG.get("telegram_pre_alert", True):
        return
    n = an["stats"]["total"]

    if PRE["n"] is not None and n != PRE["n"]:
        _stop_pre_animation()
        old_ids = list(PRE.get("message_ids", []))
        PRE["message_ids"] = []
        for mid in old_ids:
            tg_delete(mid)
        PRE["eng"] = None
        PRE["cus"] = {}
    PRE["n"] = n

    pre = an["engine"].get("pre")
    forming = [c for c in rows if c.get("on") and c.get("forming")]
    active_key = (f"engine:{pre['sid']}:{pre['need']}:{pre['bet']}" if pre else "")
    if forming:
        active_key += "|" + "|".join(f"{c['id']}:{c['forming']['need']}" for c in forming)

    if not pre and not forming:
        _stop_pre_animation()
        return
    if active_key == PRE["eng"] and PRE.get("message_ids"):
        return

    PRE["eng"] = active_key
    _stop_pre_animation()
    for mid in list(PRE.get("message_ids", [])):
        tg_delete(mid)
    PRE["message_ids"] = []

    ok, info = tg_send("🔎 <b>Analisando a mesa</b>.")
    if ok and isinstance(info, dict) and info.get("message_id"):
        mid = info["message_id"]
        PRE["message_ids"] = [mid]
        _animate_pre(mid)


@app.get("/builder")
def builder_page():
    return render_template("builder.html")


@app.get("/api/custom")
def api_custom_list():
    return jsonify(custom_state())


@app.post("/api/custom/preview")
def api_custom_preview():
    c = clean_custom(request.get_json(silent=True) or {})
    if not c:
        return jsonify({"error": "padrão inválido"}), 400
    seq, hrs = seq_hours()
    out = custom_eval(seq, c, hrs); out.pop("last", None)
    return jsonify(out)


@app.get("/api/scale")
def api_scale():
    seq, _ = seq_hours(); n = len(seq); s = [SYM[x] for x in seq]
    tr = _trend(s, n, 20, 0.55); rh = _rhythm(s, n, 20)
    w = [x for x in s[-20:] if x != "T"]
    share = max(w.count("P"), w.count("B")) / len(w) * 100 if w else 0
    return jsonify({"cells": s[-120:], "n": n, "since": max(0, n - 120),
                    "trend": {"side": SIDE.get(tr), "share": round(share)}, "rhythm": rh})


@app.post("/api/custom")
def api_custom_save():
    d = request.get_json(silent=True) or {}
    c = clean_custom(d)
    if not c:
        return jsonify({"error": "Monte um padrão de 1 a 8 bolinhas e escolha a entrada."}), 400
    with cus_lock:
        ex = next((x for x in CUS["list"] if x["id"] == d.get("id")), None)
        if ex:
            ex.update(c)
        elif len(CUS["list"]) >= 20:
            return jsonify({"error": "Máximo de 20 padrões."}), 400
        else:
            CUS["list"].append({"id": secrets.token_hex(4), **c})
        save_custom()
    with lock:
        state["version"] += 1
    return jsonify({"ok": True})


@app.post("/api/custom/<cid>/toggle")
def api_custom_toggle(cid):
    with cus_lock:
        for x in CUS["list"]:
            if x["id"] == cid:
                x["on"] = not x.get("on", True)
        save_custom()
    with lock:
        state["version"] += 1
    return jsonify({"ok": True})


@app.delete("/api/custom/<cid>")
def api_custom_delete(cid):
    with cus_lock:
        CUS["list"] = [x for x in CUS["list"] if x["id"] != cid]
        CUS["keys"].pop(cid, None)
        save_custom()
    with lock:
        state["version"] += 1
    return jsonify({"ok": True})


MOBILE_RE = re.compile(r"Mobi|Android|iPhone|iPad", re.I)
ACCESS_KEY_FILE = os.path.join(BASE_DIR, "access_key.txt")
def _load_access_key():
    try:
        if os.path.exists(ACCESS_KEY_FILE):
            v = open(ACCESS_KEY_FILE, encoding="utf-8").read().strip()
            if v:
                return v
        v = secrets.token_urlsafe(9)
        with open(ACCESS_KEY_FILE, "w", encoding="utf-8") as f:
            f.write(v)
        return v
    except Exception:
        return secrets.token_urlsafe(9)

ACCESS_KEY = _load_access_key()
PUBLIC = {"url": None, "status": "off", "msg": ""}
REMOTE_OK = {"/", "/m", "/api/stream", "/api/snapshot"}  # o que o celular (via link público) pode aceder


def is_remote():
    return "Cf-Connecting-Ip" in request.headers  # pedidos que chegam pelo túnel


@app.before_request
def guard():
    if not is_remote():
        return None
    if request.path not in REMOTE_OK and request.path != "/builder" and not request.path.startswith("/api/custom"):
        return Response("Não permitido.", 403)
    if request.args.get("k") == ACCESS_KEY:
        g.set_key = True
        return None
    if request.cookies.get("bb_key") != ACCESS_KEY:
        return Response("Acesso restrito. Abra o link completo (com a chave) ou leia o QR do painel.", 403)
    return None


@app.after_request
def keep_key(resp):
    if getattr(g, "set_key", False):
        resp.set_cookie("bb_key", ACCESS_KEY, max_age=86400 * 14, httponly=True, secure=True, samesite="Lax")
    return resp


@app.get("/")
def index():
    if is_remote() or (MOBILE_RE.search(request.headers.get("User-Agent", "")) and request.args.get("desktop") != "1"):
        return render_template("mobile.html")
    return render_template("index.html")


@app.get("/m")
def mobile_page():
    return render_template("mobile.html")


def full_link():
    return f"{PUBLIC['url']}/?k={ACCESS_KEY}" if PUBLIC["url"] else None


@app.get("/api/mobile")
def api_mobile():
    link, qr = full_link(), None
    if link:
        try:
            import segno
            q = segno.make(link, error="m")
            try:
                qr = q.svg_inline(scale=5, dark="#0b1220", light="#ffffff", border=2)
            except Exception:
                buf = io.BytesIO(); q.save(buf, kind="svg", scale=5, dark="#0b1220", light="#ffffff", border=2, xmldecl=False)
                qr = buf.getvalue().decode("utf-8")
        except Exception:
            qr = None
    return jsonify({"url": link, "qr": qr, "status": PUBLIC["status"], "msg": PUBLIC["msg"]})


def _find_cloudflared():
    exe = shutil.which("cloudflared")
    if exe:
        return exe
    local = os.path.join(BASE_DIR, "cloudflared.exe" if os.name == "nt" else "cloudflared")
    if os.path.exists(local):
        return local
    if os.name == "nt":  # baixa uma vez, ao lado do app
        PUBLIC.update(status="download", msg="A descarregar o cloudflared (só na primeira vez)…")
        url = "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-windows-amd64.exe"
        with requests.get(url, stream=True, timeout=90) as r:
            r.raise_for_status()
            with open(local + ".part", "wb") as f:
                for ch in r.iter_content(1 << 16):
                    f.write(ch)
        os.replace(local + ".part", local)
        return local
    return None


def start_tunnel():
    try:
        exe = _find_cloudflared()
        if not exe:
            PUBLIC.update(status="erro", msg="cloudflared não encontrado. Instale-o e reinicie.")
            return

        fixed_url = str(CFG.get("tunnel_url") or "").strip().rstrip("/")
        token = str(CFG.get("cloudflare_tunnel_token") or os.getenv("CLOUDFLARE_TUNNEL_TOKEN", "")).strip()

        # URL fixa: use um Cloudflare Tunnel nomeado. O trycloudflare.com é um
        # Quick Tunnel e, por desenho, recebe outro subdomínio a cada arranque.
        if fixed_url and token:
            PUBLIC.update(url=fixed_url, status="ligando", msg="A ligar ao túnel fixo…")
            cmd = [exe, "tunnel", "run", "--token", token]
        elif fixed_url and not token:
            # Permite usar uma URL fixa já publicada por outro proxy/túnel.
            PUBLIC.update(url=fixed_url, status="ok", msg="URL fixa configurada; túnel externo deve estar ativo.")
            print("\n  LINK PARA O CELULAR:", full_link(), "\n")
            with lock:
                state["version"] += 1
            return
        else:
            PUBLIC.update(status="ligando", msg="A criar o link temporário…")
            cmd = [exe, "tunnel", "--url", "http://127.0.0.1:5000", "--no-autoupdate"]

        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace",
                                creationflags=(0x08000000 if os.name == "nt" else 0))
        atexit.register(lambda: proc.terminate())
        if fixed_url and token:
            PUBLIC.update(status="ok", msg="")
            print("\n  LINK PARA O CELULAR:", full_link(), "\n")
            with lock:
                state["version"] += 1
        for line in proc.stdout:
            if not PUBLIC["url"]:
                m = re.search(r"https://[a-z0-9-]+\.trycloudflare\.com", line)
                if m:
                    PUBLIC.update(url=m.group(0), status="ok", msg="URL temporária; configure tunnel_url + token para manter o mesmo link.")
                    print("\n  LINK PARA O CELULAR:", full_link(), "\n")
                    with lock:
                        state["version"] += 1
            elif token and PUBLIC["status"] == "ligando":
                # O hostname fixo já é conhecido; o processo só precisa continuar vivo.
                PUBLIC.update(status="ok", msg="")
                print("\n  LINK PARA O CELULAR:", full_link(), "\n")
                with lock:
                    state["version"] += 1
        if PUBLIC["status"] not in ("ok",):
            PUBLIC.update(status="erro", msg="O túnel não arrancou. Verifique a configuração do Cloudflare.")
    except Exception as e:
        PUBLIC.update(status="erro", msg=str(e)[:150])


SET_KEYS = {"telegram_chat_id": str, "telegram_signals": bool, "telegram_all_rounds": bool, "tie_as_green": bool,
            "max_gale": int, "streak_alert": int, "tie_payout": float, "telegram_pre_alert": bool, "engine_signals": bool,
            "tunnel_url": str, "cloudflare_tunnel_token": str}


@app.get("/settings")
def settings_page():
    return render_template("settings.html")


@app.get("/api/settings")
def api_settings_get():
    t = CFG["telegram_token"]
    d = {k: CFG.get(k) for k in SET_KEYS if k != "cloudflare_tunnel_token"}
    d.update(has_token=bool(t), token_hint=("…" + t[-4:]) if t else "",
             has_cf_token=bool(CFG.get("cloudflare_tunnel_token")), tg_configured=state["tg_configured"])
    return jsonify(d)


@app.post("/api/settings")
def api_settings_set():
    d = request.get_json(silent=True) or {}
    upd = {}
    for k, ty in SET_KEYS.items():
        if k in d and k != "cloudflare_tunnel_token":
            try: upd[k] = ty(d[k]) if ty is not bool else bool(d[k])
            except Exception: pass
    cf_token = str(d.get("cloudflare_tunnel_token", "")).strip()
    if cf_token:
        upd["cloudflare_tunnel_token"] = cf_token
    if "max_gale" in upd: upd["max_gale"] = max(0, min(2, upd["max_gale"]))
    tok = str(d.get("telegram_token", "")).strip()
    if tok: upd["telegram_token"] = tok
    if d.get("clear_token"): upd["telegram_token"] = ""
    if "telegram_chat_id" in upd: upd["telegram_chat_id"] = upd["telegram_chat_id"].strip()
    CFG.update(upd)
    try:
        disk = {}
        if os.path.exists(_p):
            with open(_p, encoding="utf-8") as f: disk = json.load(f)
        disk.update(upd)
        with open(_p, "w", encoding="utf-8") as f: json.dump(disk, f, indent=2, ensure_ascii=False)
    except Exception as e:
        return jsonify({"ok": False, "msg": f"não foi possível gravar config.json: {e}"}), 500
    state["tg_configured"] = bool(CFG["telegram_token"] and CFG["telegram_chat_id"])
    _cache.clear(); SIG["n"] = None
    with lock: state["version"] += 1
    return jsonify({"ok": True, "msg": "Configurações guardadas"})


@app.post("/api/signals/reset")
def api_sig_reset():
    SIG["score"].update(g=0, r=0, t=0, run=0, lv=[0, 0, 0])
    with open(SIGF, "w", encoding="utf-8") as f: json.dump(SIG["score"], f)
    TELEGRAM_SCORE.update(g=0, r=0, t=0, run=0, lv=[0, 0, 0])
    _save_telegram_score()
    with lock: state["version"] += 1
    return jsonify({"ok": True})


@app.get("/api/snapshot")
def api_snapshot():
    return jsonify(snapshot())


@app.get("/api/stream")
def api_stream():
    def gen():
        seen, tick = -1, 0
        while True:
            v = state["version"]
            if v != seen or tick >= 20:
                seen, tick = v, 0
                yield f"data: {json.dumps(snapshot(), default=str)}\n\n"
            time.sleep(0.5); tick += 1
    return Response(gen(), mimetype="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.post("/api/telegram/test")
def api_tg_test():
    ok, msg = tg_send("✅ Bac Bo Monitor: ligação com o Telegram a funcionar.")
    return jsonify({"ok": ok, "msg": msg})


@app.get("/export.csv")
def export_csv():
    c = conn()
    rows = c.execute("SELECT id,ts,side,ps,bs FROM rounds ORDER BY id").fetchall()
    c.close()
    out = io.StringIO()
    w = csv.writer(out); w.writerow(["id", "ts", "resultado", "player", "banker"])
    for r in rows:
        w.writerow(list(r))
    return Response(out.getvalue(), mimetype="text/csv", headers={"Content-Disposition": "attachment; filename=bacbo.csv"})


if __name__ == "__main__":
    init_db()
    if CFG.get("reset_on_start", True):  # sem buracos: recomeça com as 200 rodadas mais recentes da API
        _c = conn(); _c.execute("DELETE FROM rounds"); _c.execute("DELETE FROM sqlite_sequence WHERE name='rounds'"); _c.commit(); _c.close()
    threading.Thread(target=poll_history, daemon=True).start()
    threading.Thread(target=listen_live, daemon=True).start()
    # Em produção (Railway, Render, etc.) a plataforma expõe a própria URL
    # pública e define a variável de ambiente PORT; nesse caso não faz
    # sentido (nem funciona, pois o cloudflared.exe é um binário Windows)
    # tentar abrir o túnel do Cloudflare. Localmente (Windows, sem PORT
    # definido) o comportamento continua igual a antes.
    is_cloud = "PORT" in os.environ
    if CFG.get("tunnel", True) and not is_cloud:
        threading.Thread(target=start_tunnel, daemon=True).start()
    port = int(os.environ.get("PORT", 5000))
    host = "0.0.0.0" if is_cloud else "127.0.0.1"
    print(f"Abra http://127.0.0.1:{port}" if not is_cloud else f"A correr na porta {port}")
    app.run(host=host, port=port, debug=False, threaded=True)
