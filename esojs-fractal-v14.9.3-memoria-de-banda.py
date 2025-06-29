# esojs-fractal-v14.9.2-memoria-de-banda-entrada-intra-vela.py
# ATUALIZADO: ENTRADA INTRA-VELA NO ROMPIMENTO, MEMÓRIA DE BANDA, MARTINGALE INTRA-VELA E RECONNECT

import websocket
import json
import time
import pandas as pd
import numpy as np

# --- Configurações do Bot ---
APP_ID               = 1089
API_TOKEN            = ''  # Use seu token real aqui
SYMBOL               = 'R_100'
CANDLE_INTERVAL      = 60       # 1 minuto
INITIAL_STAKE        = 50
MARTINGALE_FACTOR    = 2
MAX_MARTINGALE_LEVEL = 1
FRACTAL_PERIOD       = 2        # Ajustado para o padrão da Deriv (2 de cada lado)
HISTORY_COUNT        = 200
GALE_EXPIRY_SECONDS_THRESHOLD = 30  # ainda pode usar se quiser, mas não será mais aplicado aqui

# Tolerância para considerar uma vela como "não-Doji"
DOJI_TOLERANCE_PERCENT = 0.00005  # 0.005%

# --- Estado do Bot ---
current_stake        = INITIAL_STAKE
loss_count           = 0
contract_id          = None
history_req_id       = 1
buy_req_id           = 1000
last_update_epoch    = None  # epoch da última vela OHLC processada
candles_data         = []
last_trade_direction = None  # 'CALL' ou 'PUT' da última operação

# --- Estado para memória de banda ---
# 'none', 'upper_broken', 'lower_broken'
band_break_status = 'none'

def log(msg):
    ts = time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime())
    print(f"[{ts} GMT] {msg}")

def calculate_fractal_chaos_bands(df, period=FRACTAL_PERIOD):
    # garante que estamos trabalhando numa cópia e não numa view
    df = df.copy()
    if len(df) < period * 2 + 1:
        log("   [FCB Calc] Dados insuficientes para cálculo de fractais.")
        return None, None

    df.loc[:, 'is_high_fractal'] = False
    df.loc[:, 'is_low_fractal']  = False

    for i in range(period, len(df) - period):
        high = df['high'].iloc[i]
        if high > df['high'].iloc[i-period:i].max() and high > df['high'].iloc[i+1:i+period+1].max():
            df.loc[df.index[i], 'is_high_fractal'] = True
    for i in range(period, len(df) - period):
        low = df['low'].iloc[i]
        if low < df['low'].iloc[i-period:i].min() and low < df['low'].iloc[i+1:i+period+1].min():
            df.loc[df.index[i], 'is_low_fractal'] = True

    ub_series = df[df['is_high_fractal']]['high'].ffill()
    lb_series = df[df['is_low_fractal']]['low'].ffill()
    ub = ub_series.iloc[-1] if not ub_series.empty else None
    lb = lb_series.iloc[-1] if not lb_series.empty else None

    if lb is not None and ub is not None and lb >= ub:
        log(f"[AVISO] Bandas invertidas detectadas (lower={lb:.2f}, upper={ub:.2f}). Ignorando.")
        return None, None

    log(f"   [FCB Calc] Bandas calculadas: L={lb:.2f}, U={ub:.2f}")
    return ub, lb

def send_purchase_request(ws, contract_type, expiry_timestamp=None):
    global current_stake, buy_req_id, contract_id, last_trade_direction
    if contract_id is not None:
        log("❌ Já existe uma operação em andamento.")
        return

    params = {
        "amount": float(f"{current_stake:.2f}"),
        "basis": "stake",
        "contract_type": contract_type,
        "currency": "USD",
        "symbol": SYMBOL
    }
    if expiry_timestamp:
        params["date_expiry"] = int(expiry_timestamp)
        log_msg = (
            f"▶ Enviando {contract_type} (Gale/Intra-Vela) • "
            f"stake={current_stake:.2f} • "
            f"Expiry={time.strftime('%H:%M:%S', time.gmtime(expiry_timestamp))} GMT"
        )
    else:
        log("[ERRO LÓGICO] expiry_timestamp ausente.")
        return

    req = {
        "buy": 1,
        "price": float(f"{current_stake:.2f}"),
        "parameters": params,
        "req_id": buy_req_id
    }
    last_trade_direction = contract_type
    ws.send(json.dumps(req))
    log(f"{log_msg} (req_id={buy_req_id})")
    buy_req_id += 1

def on_open(ws):
    log("Conexão aberta. Autenticando…")
    ws.send(json.dumps({"authorize": API_TOKEN}))

def on_message(ws, message):
    global history_req_id, contract_id, loss_count, current_stake
    global last_update_epoch, candles_data, last_trade_direction, band_break_status

    msg = json.loads(message)
    if 'error' in msg:
        log(f"[ERRO] {msg['error']['message']}")
        return
    mt = msg.get('msg_type')

    if mt == 'authorize':
        loginid = msg['authorize']['loginid']
        log(f"Autenticado como {loginid}")
        ws.send(json.dumps({
            "ticks_history": SYMBOL,
            "granularity": CANDLE_INTERVAL,
            "style": "candles",
            "subscribe": 1,
            "count": HISTORY_COUNT,
            "end": "latest",
            "req_id": history_req_id
        }))
        history_req_id += 1

    elif mt == 'candles':
        arr = msg.get('candles', [])
        log(f"[candles] Recebidos {len(arr)} candles iniciais")
        candles_data.clear()
        for c in arr:
            candles_data.append({
                'epoch': c['epoch'],
                'open': float(c['open']),
                'high': float(c['high']),
                'low': float(c['low']),
                'close': float(c['close'])
            })
        log(f"Histórico carregado: {len(candles_data)} candles")

    elif mt == 'ohlc':
        o = msg['ohlc']
        epoch = o['open_time']

        # atualização da vela atual
        if epoch == last_update_epoch:
            if candles_data:
                candles_data[-1]['high']  = max(candles_data[-1]['high'],  float(o['high']))
                candles_data[-1]['low']   = min(candles_data[-1]['low'],   float(o['low']))
                candles_data[-1]['close'] = float(o['close'])
            return

        # vela fechada
        if last_update_epoch is not None and candles_data:
            candles_data[-1]['close'] = float(o['open'])
            c = candles_data[-1]
            log(f"--- VELA FECHADA --- Epoch: {c['epoch']}, O:{c['open']:.5f}, H:{c['high']:.5f}, L:{c['low']:.5f}, C:{c['close']:.5f}")

        # adiciona nova vela
        candles_data.append({
            'epoch': epoch,
            'open':  float(o['open']),
            'high':  float(o['high']),
            'low':   float(o['low']),
            'close': float(o['close'])
        })
        if len(candles_data) > HISTORY_COUNT:
            candles_data.pop(0)
        last_update_epoch = epoch

        df = pd.DataFrame(candles_data)
        if len(df) < 3:
            log("→ Histórico insuficiente para rompimento.")
            return

        # PRIORIDADE 1: operação em andamento?
        if contract_id is not None:
            log("→ Operação em andamento.")
            return

        # PRIORIDADE 2: Gale pendente? — agora intra-vela
        if loss_count > 0:
            now = int(time.time())
            start = (now // CANDLE_INTERVAL) * CANDLE_INTERVAL
            expiry = start + CANDLE_INTERVAL - 1
            left = expiry - now
            if left < 5:
                log(f"   [AVISO] Tempo insuficiente para Gale intra-vela ({left}s). Ignorando.")
                return
            log(f"✔ Gale intra-vela: expirando em {time.strftime('%H:%M:%S', time.gmtime(expiry))} GMT")
            send_purchase_request(ws, last_trade_direction, expiry)
            return

        # Procurando rompimento intra-vela normal
        log("→ Procurando rompimento intra-vela…")
        ub, lb = calculate_fractal_chaos_bands(df.iloc[:-1])
        if ub is None or lb is None:
            log("→ Bandas inválidas.")
            return

        # reset memória de banda
        last_close = df.iloc[-2]['close']
        if lb < last_close < ub and band_break_status != 'none':
            log(f"   Resetando memória de banda (voltou entre L={lb:.2f} e U={ub:.2f}).")
            band_break_status = 'none'

        now = int(time.time())
        expiry = (now // CANDLE_INTERVAL) * CANDLE_INTERVAL + CANDLE_INTERVAL - 1
        if expiry - now < 5:
            log(f"   [AVISO] Tempo insuficiente para expiração ({expiry-now}s).")
            return

        r = df.iloc[-2]
        p = df.iloc[-3]
        body = abs(r['close'] - r['open'])
        th   = r['open'] * DOJI_TOLERANCE_PERCENT
        is_doji = body <= th
        bull = r['close'] > r['open'] and not is_doji
        bear = r['close'] < r['open'] and not is_doji
        up   = r['close'] > ub and p['close'] <= ub
        down = r['close'] < lb and p['close'] >= lb

        if up and bull and band_break_status == 'none':
            log(f"🔥 Romp ALTA fresco! {r['close']:.2f}>{ub:.2f}. CALL.")
            send_purchase_request(ws, "CALL", expiry)
            band_break_status = 'upper_broken'
        elif down and bear and band_break_status == 'none':
            log(f"🔥 Romp BAIXA fresco! {r['close']:.2f}<{lb:.2f}. PUT.")
            send_purchase_request(ws, "PUT", expiry)
            band_break_status = 'lower_broken'
        else:
            log("→ Nenhum rompimento.")

    elif mt == 'buy':
        contract_id = msg['buy']['contract_id']
        ws.send(json.dumps({
            "proposal_open_contract": 1,
            "contract_id": contract_id,
            "subscribe": 1
        }))
        log(f"Contrato comprado: {contract_id}")

    elif mt == 'proposal_open_contract':
        pc = msg['proposal_open_contract']
        if not pc.get('is_sold'):
            return
        contract_id = None
        profit = float(pc['profit'])
        if profit > 0:
            log(f"🎉 Vitória! +{profit:.2f}")
            loss_count = 0
            current_stake = INITIAL_STAKE
            band_break_status = 'none'
        else:
            log(f"😭 Derrota! {profit:.2f}")
            loss_count += 1
            if loss_count > MAX_MARTINGALE_LEVEL:
                log("Limite de Martingale; resetando stake.")
                loss_count = 0
                current_stake = INITIAL_STAKE
                band_break_status = 'none'
            # caso contrário, o Gale será tratado no próximo on_message

# --- reconnect logic ---
def on_error(ws, error):
    log(f"[ERRO] {error}. Reconectando em 5s…")
    ws.close()

def on_close(ws, code, msg):
    log(f"[CLOSED] {code} / {msg}. Reconectando em 5s…")
    time.sleep(5)
    start_bot()

def start_bot():
    global current_stake, loss_count, contract_id, candles_data, last_update_epoch
    global last_trade_direction, history_req_id, buy_req_id, band_break_status

    current_stake        = INITIAL_STAKE
    loss_count           = 0
    contract_id          = None
    candles_data         = []
    last_update_epoch    = None
    last_trade_direction = None
    history_req_id       = 1
    buy_req_id           = 1000
    band_break_status    = 'none'

    url = f"wss://ws.derivws.com/websockets/v3?app_id={APP_ID}"
    log("Iniciando Deriv Trading Bot (v14.9.2 – Fractal, Memória de Banda, Intra-Vela, Martingale Intra-Vela e Reconnect)…")
    ws_app = websocket.WebSocketApp(
        url,
        on_open=on_open,
        on_message=on_message,
        on_error=on_error,
        on_close=on_close
    )
    ws_app.run_forever(ping_interval=20, ping_timeout=10)

if __name__ == "__main__":
    start_bot()
