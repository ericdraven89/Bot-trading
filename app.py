import os
import re
import time
import threading
from collections import deque

import requests
import numpy as np
import pandas as pd
import yfinance as yf
from flask import Flask, request

TOKEN = os.environ["TOKEN"]
CHAT_ID = int(os.environ["CHAT_ID"])
SECRET = os.environ["SECRET"]
API = f"https://api.telegram.org/bot{TOKEN}"

app = Flask(__name__)

strumenti = {
    "S&P 500": "^GSPC",
    "Nasdaq 100": "^NDX",
    "Euro Stoxx 50": "^STOXX50E",
    "DAX": "^GDAXI",
    "Oro": "GC=F",
    "Petrolio WTI": "CL=F",
    "Gas naturale": "NG=F",
    "EUR/USD": "EURUSD=X",
    "GBP/USD": "GBPUSD=X",
    "USD/JPY": "USDJPY=X",
    "AUD/USD": "AUDUSD=X",
}

alias = {}
for _nome, _chiavi in {
    "S&P 500": ["sp500", "spx", "us500", "sep500"],
    "Nasdaq 100": ["nasdaq100", "nasdaq", "nas100", "us100", "ndx"],
    "Euro Stoxx 50": ["eurostoxx50", "eurostoxx", "stoxx50", "stoxx", "sx5e"],
    "DAX": ["dax", "dax40", "dax30", "ger40", "ger30", "de40"],
    "Oro": ["oro", "gold", "xauusd"],
    "Petrolio WTI": ["petrolio", "wti", "oil", "usoil", "crude", "petroliowti"],
    "Gas naturale": ["gas", "gasnaturale", "natgas", "ngas"],
    "EUR/USD": ["eurusd"],
    "GBP/USD": ["gbpusd"],
    "USD/JPY": ["usdjpy"],
    "AUD/USD": ["audusd"],
}.items():
    for _k in _chiavi:
        alias[_k] = _nome


def norm(t):
    return re.sub(r"[^a-z0-9]", "", t.lower())


def numero(t):
    try:
        v = float(t.replace(",", ".").replace(" ", ""))
        return v if v > 0 else None
    except ValueError:
        return None


def scarica(ticker):
    oggi = pd.Timestamp.now(tz="Europe/Rome").date()
    df = yf.download(ticker, period="3y", interval="1d",
                     auto_adjust=True, progress=False)
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    df = df.dropna(subset=["Open", "High", "Low", "Close"])
    df = df[(df["Open"] > 0) & (df["Close"] > 0)]
    return df[df.index.date < oggi]


_cache = {}


def calcola(nome):
    adesso = time.time()
    if nome in _cache and adesso - _cache[nome][0] < 10800:
        return _cache[nome][1]
    df = scarica(strumenti[nome])
    if len(df) < 300:
        return None
    rng = (df["High"] - df["Low"]) / df["Close"]
    rng = rng.where(rng > 0)
    atteso = rng.ewm(span=10, adjust=False).mean()
    a = atteso.iloc[-1]
    perc = (atteso.iloc[-500:] < a).mean()
    if perc < 0.25:
        livello = "BASSA"
    elif perc > 0.75:
        livello = "ALTA"
    else:
        livello = "normale"
    x = (a, livello)
    _cache[nome] = (adesso, x)
    return x


def eventi_oggi():
    oggi = pd.Timestamp.now(tz="Europe/Rome").date()
    try:
        r = requests.get("https://nfs.faireconomy.media/ff_calendar_thisweek.json",
                         headers={"User-Agent": "Mozilla/5.0"}, timeout=15)
        r.raise_for_status()
        dati = r.json()
    except Exception:
        return None
    out = []
    for e in dati:
        if e.get("impact") != "High":
            continue
        try:
            t = pd.to_datetime(e["date"], utc=True).tz_convert("Europe/Rome")
        except Exception:
            continue
        if t.date() == oggi:
            out.append((t.strftime("%H:%M"), e.get("country", ""), e.get("title", "")))
    return sorted(out)


def report():
    oggi = pd.Timestamp.now(tz="Europe/Rome").date()
    righe = [f"REPORT DEL {oggi.strftime('%d/%m/%Y')}", ""]
    for nome in strumenti:
        x = calcola(nome)
        if x is None:
            righe.append(f"{nome}: dati non disponibili")
            continue
        a, livello = x
        righe.append(f"{nome}: range atteso {a*100:.2f}% | volatilità {livello}")
        righe.append(f"  stop {0.4*a*100:.2f}% | obiettivo {0.6*a*100:.2f}% del prezzo")
    righe.append("")
    righe.append("EVENTI MACRO IMPORTANTI DI OGGI (ora italiana)")
    ev = eventi_oggi()
    if ev is None:
        righe.append("calendario non disponibile")
    elif len(ev) == 0:
        righe.append("nessun evento importante")
    else:
        for ora, paese, titolo in ev:
            righe.append(f"  {ora} {paese} - {titolo}")
    righe.append("")
    righe.append("Stop = 40% e obiettivo = 60% del range atteso (indicativi, non testati).")
    righe.append("Scrivi il nome di uno strumento per avere i livelli di prezzo.")
    return "\n".join(righe)


def livelli(nome, prezzo):
    x = calcola(nome)
    if x is None:
        return f"{nome}: dati non disponibili"
    a, livello = x
    dec = 4 if prezzo < 20 else 2
    punti = a * prezzo
    s = 0.4 * punti
    t = 0.6 * punti
    return (f"{nome}: prezzo {prezzo:.{dec}f}\n"
            f"range atteso {punti:.{dec}f} ({a*100:.2f}%) | volatilità {livello}\n"
            f"BUY:  stop {prezzo - s:.{dec}f} | obiettivo {prezzo + t:.{dec}f}\n"
            f"SELL: stop {prezzo + s:.{dec}f} | obiettivo {prezzo - t:.{dec}f}\n"
            f"(indicativi, non testati)")


def invia(testo):
    requests.post(f"{API}/sendMessage",
                  data={"chat_id": CHAT_ID, "text": testo}, timeout=20)


stato = None
visti = deque(maxlen=200)


def gestisci(u):
    global stato
    m = u.get("message")
    if not m or m["chat"]["id"] != CHAT_ID or "text" not in m:
        return
    t = m["text"].strip()
    try:
        if t.lower().startswith("/start"):
            stato = None
            invia("Calcolo il report, ci vuole un po'...")
            invia(report())
            return
        parti = t.rsplit(None, 1)
        if norm(t) in alias:
            stato = alias[norm(t)]
            invia(f"Qual è il prezzo attuale di {stato}?")
        elif len(parti) == 2 and norm(parti[0]) in alias and numero(parti[1]) is not None:
            stato = None
            invia(livelli(alias[norm(parti[0])], numero(parti[1])))
        elif stato is not None and numero(t) is not None:
            nome = stato
            stato = None
            invia(livelli(nome, numero(t)))
        elif stato is not None:
            invia("Scrivi solo il prezzo, per esempio 7722.5, oppure il nome di un altro strumento.")
        else:
            invia("Non ho capito. Scrivi /start per il report, il nome di uno strumento, "
                  "oppure nome e prezzo insieme (esempio: oro 4162.3).")
    except Exception as e:
        invia(f"Errore: {e}")


@app.route("/webhook", methods=["POST"])
def webhook():
    if request.headers.get("X-Telegram-Bot-Api-Secret-Token") != SECRET:
        return "no", 403
    u = request.get_json(silent=True) or {}
    uid = u.get("update_id")
    if uid in visti:
        return "ok"
    visti.append(uid)
    threading.Thread(target=gestisci, args=(u,), daemon=True).start()
    return "ok"


@app.route("/")
def home():
    return "ok"
