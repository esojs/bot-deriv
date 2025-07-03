import websocket
import json
import time
import pandas as pd

# --- Configurações do Bot ---
APP_ID = 1089
API_TOKEN = 'uFedqbTqaKZgzBR'  # <-- COLOQUE SEU TOKEN REAL AQUI
SYMBOL = 'R_100'
CANDLE_INTERVAL = 60  # em segundos
INITIAL_STAKE = 0.35
MARTINGALE_FACTOR = 2
MAX_MARTINGALE_LEVEL = 2
HISTORY_COUNT = 200
GALE_EXPIRY_SECONDS_THRESHOLD = 30
# Stop loss / stop win (valores em USD)
STOP_LOSS = -2.0
STOP_WIN = 2.0

# --- Estado do Bot ---
current_stake = INITIAL_STAKE
loss_count = 0
contract_id = None
buy_req_id = 1000
candles_data = []
ticks_current = None  # candle em construção a partir de ticks
total_profit = 0.0  # saldo acumulado das operações


def log(msg):
    ts = time.strftime('%Y-%m-%d %H:%M:%S')
    print(f"[{ts}] {msg}")


def send_purchase_request(ws, contract_type, expiry_timestamp=None):
    global current_stake, buy_req_id, contract_id, total_profit
    # não dispara ordens se stop loss/win atingido
    if total_profit <= STOP_LOSS:
        log(f"🛑 Stop loss atingido ({total_profit:.2f}). Interrompendo negociações.")
        ws.close()
        return
    if total_profit >= STOP_WIN:
        log(f"🏆 Stop win atingido ({total_profit:.2f}). Interrompendo negociações.")
        ws.close()
        return
    if contract_id is not None:
        log("❌ Já existe uma operação em andamento.")
        return
    params = {
        "amount": float(f"{current_stake:.2f}"),
        "basis": "stake",
        "contract_type": contract_type,
        "currency": "USD",
        "symbol": SYMBOL,
        "duration": 1,
        "duration_unit": "m"
    }
    req = {
        "buy": 1,
        "price": float(f"{current_stake:.2f}"),
        "parameters": params,
        "req_id": buy_req_id
    }
    ws.send(json.dumps(req))
    log(f"▶ Enviando {contract_type} • stake={current_stake:.2f} (req_id={buy_req_id})")
    buy_req_id += 1


def on_open(ws):
    log("Conexão aberta. Autenticando…")
    ws.send(json.dumps({"authorize": API_TOKEN}))


def finalize_candle_and_detect(ws, candle):
    global total_profit, loss_count, current_stake, contract_id
    # adiciona vela finalizada e mantém histórico
    candles_data.append(candle)
    if len(candles_data) > HISTORY_COUNT:
        candles_data.pop(0)
    # bloqueia detecção se stop loss/win atingido
    if total_profit <= STOP_LOSS or total_profit >= STOP_WIN:
        return
    # só detecta padrão se houver pelo menos 4 velas e sem contrato ativo
    if len(candles_data) < 4 or contract_id is not None or loss_count != 0:
        return
    df = pd.DataFrame(candles_data)
    v0, v1, v2, v3 = df.iloc[-4], df.iloc[-3], df.iloc[-2], df.iloc[-1]
    log("--- [ANÁLISE MOURA COMPRA] ---")
    log(f"V0: O{v0['open']} C{v0['close']} | V1: O{v1['open']} C{v1['close']} | "
        f"V2: O{v2['open']} C{v2['close']} | V3: O{v3['open']} C{v3['close']}")
    pre = v0['close'] > v0['open']
    c1 = (v1['close'] < v1['open']) and ((v1['high'] - v1['open']) > 0)
    # V2 deve ser vermelha, abaixo de V1 e corpo não ultrapassar pavio de V1
    c2 = (
        (v2['close'] < v2['open']) and  # vela 2 vermelha
        (v2['open'] < v1['high']) and    # abertura de V2 abaixo do pavio de V1
        (v2['close'] < v1['high']) and   # fechamento de V2 abaixo do pavio de V1
        (v2['close'] < v1['close'])      # fechamento de V2 abaixo do corpo de V1
    )
    c3 = v3['close'] > v3['open']
    log(f"Conds: V0 verde={pre}, V1 red c/pavio={c1}, "
        f"V2 red c/corpo não rompe pavio V1={c2}, V3 verde={c3}")
    if pre and c1 and c2 and c3:
        log("✔ Padrão Moura COMPRA identificado! Enviando CALL.")
        send_purchase_request(ws, "CALL")


def on_message(ws, message):
    global contract_id, loss_count, current_stake, ticks_current, total_profit
    msg = json.loads(message)
    if 'error' in msg:
        log(f"[ERRO] {msg['error']['message']}")
        return
    mt = msg.get('msg_type')
    # autorização
    if mt == 'authorize':
        log(f"Autenticado como {msg['authorize']['loginid']}")
        ws.send(json.dumps({"ticks": SYMBOL, "subscribe": 1}))
        log(f"Subscrito em ticks de {SYMBOL}")
        return
    # construção de candles
    if mt == 'tick':
        epoch = msg['tick']['epoch']
        price = float(msg['tick']['quote'])
        bucket = epoch - (epoch % CANDLE_INTERVAL)
        if ticks_current is None or ticks_current['bucket'] != bucket:
            if ticks_current is not None:
                candle = {
                    'epoch': ticks_current['bucket'],
                    'open': ticks_current['open'],
                    'high': ticks_current['high'],
                    'low': ticks_current['low'],
                    'close': ticks_current['close']
                }
                finalize_candle_and_detect(ws, candle)
            ticks_current = {'bucket': bucket, 'open': price, 'high': price, 'low': price, 'close': price}
        else:
            ticks_current['high'] = max(ticks_current['high'], price)
            ticks_current['low'] = min(ticks_current['low'], price)
            ticks_current['close'] = price
        return
    # compra executada
    if mt == 'buy':
        contract_id = msg['buy']['contract_id']
        ws.send(json.dumps({"proposal_open_contract": 1, "contract_id": contract_id, "subscribe": 1}))
        log(f"Contrato comprado: {contract_id}")
        return
    # resultado da compra
    if mt == 'proposal_open_contract':
        if not msg['proposal_open_contract'].get('is_sold'):
            return
        profit = float(msg['proposal_open_contract']['profit'])
        contract_id = None
        total_profit += profit
        if profit > 0:
            log(f"🎉 Vitória! Lucro: {profit:.2f} | PnL total: {total_profit:.2f}")
            loss_count = 0
            current_stake = INITIAL_STAKE
        else:
            log(f"😭 Derrota! Prejuízo: {profit:.2f} | PnL total: {total_profit:.2f}")
            loss_count += 1
            current_stake *= MARTINGALE_FACTOR
            log(f"Martingale Nível {loss_count}, stake agora {current_stake:.2f}")
        # checar stop loss/win após resultado
        if total_profit <= STOP_LOSS:
            log(f"🛑 Stop loss atingido ({total_profit:.2f}). Encerrando bot.")
            ws.close()
        elif total_profit >= STOP_WIN:
            log(f"🏆 Stop win atingido ({total_profit:.2f}). Encerrando bot.")
            ws.close()
        return


def on_error(ws, error):
    log(f"[WebSocket Error] {error}")


def on_close(ws, code, msg):
    log(f"Conexão fechada ({code}): {msg}")
    time.sleep(5)
    start_bot()


def start_bot():
    global current_stake, loss_count, contract_id, buy_req_id, candles_data, ticks_current, total_profit
    current_stake = INITIAL_STAKE
    loss_count = 0
    contract_id = None
    buy_req_id = 1000
    candles_data = []
    ticks_current = None
    total_profit = 0.0
    url = f"wss://ws.derivws.com/websockets/v3?app_id={APP_ID}"
    log("Iniciando Deriv Trading Bot (Moura COMPRA v2.3 - Ticks e SL/SW)...")
    ws_app = websocket.WebSocketApp(
        url,
        on_open=on_open,
        on_message=on_message,
        on_error=on_error,
        on_close=on_close
    )
    ws_app.run_forever(ping_interval=30, ping_timeout=10)

if __name__ == "__main__":
    start_bot()
