# Nome do arquivo: esojs-fractal-terceirado-rompimento-v16.4.py
# Versão: 16.4  Entrada imediata, reconexao automatica
# Descrição: Bot para Deriv rompe, confirma, entra

import websocket
import json
import time
import pandas as pd
import numpy as np

# --- Configurações do Bot ---
APP_ID              = 1089
API_TOKEN           = ''  # Use seu token real aqui
SYMBOL              = 'R_75'
CANDLE_INTERVAL     = 60      # 1 minuto
STAKE               = 50      # Valor fixo para cada operação
FRACTAL_PERIOD      = 2       # Fractais de 2 candles antes/depois
HISTORY_COUNT       = 200

DOJI_TOLERANCE_PERCENT = 0.00005  # 0.005%

# --- Estado do Bot ---
contract_id         = None
history_req_id      = 1
buy_req_id          = 1000
last_update_epoch   = None
candles_data        = []
awaiting_confirmation = None  # 'CALL' ou 'PUT'
confirmation_epoch    = None  # epoch da vela de confirmação

def log(msg):
    ts = time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime())
    print(f"[{ts} GMT] {msg}")

def calculate_fractal_chaos_bands(df, period=FRACTAL_PERIOD):
    if len(df) < period*2+1:
        return None, None
    df = df.copy()
    df['is_high_fractal'] = False
    df['is_low_fractal'] = False

    for i in range(period, len(df)-period):
        h = df.loc[i, 'high']
        if h > df['high'].iloc[i-period:i].max() and h > df['high'].iloc[i+1:i+period+1].max():
            df.at[i, 'is_high_fractal'] = True
        l = df.loc[i, 'low']
        if l < df['low'].iloc[i-period:i].min() and l < df['low'].iloc[i+1:i+period+1].min():
            df.at[i, 'is_low_fractal'] = True

    ub = df.loc[df['is_high_fractal'], 'high'].ffill().iloc[-1] if df['is_high_fractal'].any() else None
    lb = df.loc[df['is_low_fractal'], 'low'].ffill().iloc[-1] if df['is_low_fractal'].any() else None
    if ub is not None and lb is not None and lb >= ub:
        return None, None
    return ub, lb

def send_purchase_request(ws, contract_type):
    global buy_req_id, contract_id
    if contract_id is not None:
        log("❌ Já existe uma operação em andamento.")
        return
    params = {
        "amount": float(f"{STAKE:.2f}"),
        "basis": "stake",
        "contract_type": contract_type,
        "currency": "USD",
        "symbol": SYMBOL,
        "duration": 1,
        "duration_unit": "m"
    }
    req = {"buy": 1, "price": float(f"{STAKE:.2f}"), "parameters": params, "req_id": buy_req_id}
    ws.send(json.dumps(req))
    log(f"▶ Enviando {contract_type} • stake={STAKE:.2f} (req_id={buy_req_id})")
    buy_req_id += 1

def on_open(ws):
    log("Conexão aberta. Autenticando…")
    ws.send(json.dumps({"authorize": API_TOKEN}))

def on_message(ws, message):
    global history_req_id, contract_id, last_update_epoch, candles_data
    global awaiting_confirmation, confirmation_epoch

    msg = json.loads(message)
    if 'error' in msg:
        log(f"[ERRO] {msg['error']['message']}")
        return

    mt = msg.get('msg_type')
    # Quando recebe o histórico inicial de candles
    if mt == 'authorize':
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
        return

    if mt == 'candles':
        arr = msg.get('candles', [])
        candles_data.clear()
        for c in arr:
            candles_data.append({
                'epoch': c['epoch'],
                'open': float(c['open']),
                'high': float(c['high']),
                'low': float(c['low']),
                'close': float(c['close'])
            })
        log(f"[candles] Histórico carregado: {len(candles_data)} velas")
        return

    # Atualização intra-vela e final de vela
    if mt == 'ohlc':
        o = msg['ohlc']
        epoch = o['open_time']

        # Mesma vela: atualiza high/low/close provisório
        if epoch == last_update_epoch:
            c = candles_data[-1]
            c['high'] = max(c['high'], float(o['high']))
            c['low']  = min(c['low'],  float(o['low']))
            c['close']= float(o['close'])
            return

        # Nova vela: finaliza a anterior e adiciona nova
        if last_update_epoch is not None:
            candles_data[-1]['close'] = float(o['open'])
        candles_data.append({
            'epoch': epoch,
            'open': float(o['open']),
            'high': float(o['high']),
            'low': float(o['low']),
            'close': float(o['close'])
        })
        if len(candles_data) > HISTORY_COUNT:
            candles_data.pop(0)
        last_update_epoch = epoch

        df = pd.DataFrame(candles_data)
        # Se estamos aguardando confirmação
        if awaiting_confirmation:
            # a vela de confirmação acabou de fechar: df.iloc[-2]
            conf = df.iloc[-2]
            body = abs(conf['close'] - conf['open'])
            doji_th = conf['open'] * DOJI_TOLERANCE_PERCENT
            bullish = conf['close'] > conf['open'] and body > doji_th
            bearish = conf['close'] < conf['open'] and body > doji_th

            log(f"⏱ Vela de confirmação fechou (epoch {conf['epoch']}). Preço C={conf['close']:.5f}")

            if (awaiting_confirmation == 'CALL' and bullish) or (awaiting_confirmation == 'PUT' and bearish):
                log(f"✔ Confirmação de {awaiting_confirmation} válida: entrando IMEDIATO.")
                send_purchase_request(ws, awaiting_confirmation)
            else:
                log(f"❌ Confirmação falhou (esperado {awaiting_confirmation}).")
            awaiting_confirmation = None
            confirmation_epoch = None
            return

        # Caso contrário, procuramos rompimento
        if contract_id is None and len(df) >= 3:
            last_closed = df.iloc[-2]
            prev = df.iloc[-3]
            ub, lb = calculate_fractal_chaos_bands(df.iloc[:-1])
            if ub and lb:
                if last_closed['close'] > ub and prev['close'] <= ub:
                    awaiting_confirmation = 'CALL'
                    confirmation_epoch = df.iloc[-1]['epoch']  # próxima vela
                    log(f"🔥 Rompimento de ALTA detectado! Aguardando confirmação na vela epoch {confirmation_epoch}.")
                elif last_closed['close'] < lb and prev['close'] >= lb:
                    awaiting_confirmation = 'PUT'
                    confirmation_epoch = df.iloc[-1]['epoch']
                    log(f"🔥 Rompimento de BAIXA detectado! Aguardando confirmação na vela epoch {confirmation_epoch}.")
        return

    # Resposta à compra
    if mt == 'buy':
        contract_id = msg['buy']['contract_id']
        ws.send(json.dumps({"proposal_open_contract": 1, "contract_id": contract_id, "subscribe": 1}))
        return

    if mt == 'proposal_open_contract':
        pc = msg['proposal_open_contract']
        if not pc.get('is_sold'):
            return
        profit = float(pc['profit'])
        log(f"{'🎉 Vitória' if profit>0 else '😭 Derrota'}! {('Lucro' if profit>0 else 'Perda')}: {profit:.2f}")
        # Libera para nova operação
        contract_id = None

def run():
    """
    Loop de conexão: reconecta automaticamente após 5s em caso de queda.
    """
    while True:
        ws_app = websocket.WebSocketApp(
            f"wss://ws.derivws.com/websockets/v3?app_id={APP_ID}",
            on_open=on_open,
            on_message=on_message,
            on_error=lambda ws, err: log(f"[ERROR] {err}"),
            on_close=lambda ws, code, msg: log(f"[CLOSE] code={code}, msg={msg}")
        )
        try:
            log("Conectando ao WebSocket…")
            ws_app.run_forever(ping_interval=20, ping_timeout=10)
        except Exception as e:
            log(f"[EXCEPTION] {e}")
        log("Reconectando em 5 segundos…")
        time.sleep(5)

if __name__ == "__main__":
    run()

