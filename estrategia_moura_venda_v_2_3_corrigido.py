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

# --- Estado do Bot ---
current_stake = INITIAL_STAKE
loss_count    = 0
contract_id   = None
buy_req_id    = 1000
candles_data  = []
ticks_current = None  # vela em construção a partir de ticks


def log(msg):
    ts = time.strftime('%Y-%m-%d %H:%M:%S')
    print(f"[{ts}] {msg}")


def send_purchase_request(ws, contract_type):
    global current_stake, buy_req_id, contract_id
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


def finalize_candle_and_detect(ws, candle):
    global candles_data, contract_id, loss_count, current_stake

    # log da vela que acabou de fechar
    log(f"🕒 Vela finalizada: O{candle['open']} H{candle['high']} L{candle['low']} C{candle['close']}")

    # adiciona e mantém tamanho máximo do histórico
    candles_data.append(candle)
    if len(candles_data) > HISTORY_COUNT:
        candles_data.pop(0)

    # só a partir de 4 velas inicia a análise
    if len(candles_data) < 4 or contract_id is not None or loss_count != 0:
        return

    # extrai velas V0,V1,V2,V3
    df = pd.DataFrame(candles_data)
    v0, v1, v2, v3 = df.iloc[-4], df.iloc[-3], df.iloc[-2], df.iloc[-1]

    log("--- [ANÁLISE MOURA VENDA] ---")
    log(f"V0: O{v0['open']} C{v0['close']} | "
        f"V1: O{v1['open']} C{v1['close']} | "
        f"V2: O{v2['open']} C{v2['close']} | "
        f"V3: O{v3['open']} C{v3['close']}")

    # condições do padrão
    pre = v0['close'] < v0['open']  # V0 vermelha
    c1  = (v1['close'] > v1['open']) and ((v1['open'] - v1['low']) > 0)  # V1 verde c/ pavio inf.
    c2  = (
        (v2['close'] > v2['open']) and
        (v2['close'] < v1['close']) and
        (v2['close'] > v1['open'])
    )  # V2 verde dentro do corpo de V1
    c3  = v3['close'] < v3['open']  # V3 vermelha

    log(f"Conds: V0 vermelha={pre}, V1 verde/pavio={c1}, "
        f"V2 in-body V1={c2}, V3 vermelha={c3}")

    if pre and c1 and c2 and c3:
        log("✔ Padrão Moura VENDA identificado! Enviando PUT.")
        send_purchase_request(ws, "PUT")


def on_open(ws):
    log("Conexão aberta. Autenticando…")
    ws.send(json.dumps({"authorize": API_TOKEN}))


def on_message(ws, message):
    global contract_id, loss_count, current_stake, ticks_current

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

    # monta candles a partir de ticks
    if mt == 'tick':
        epoch = msg['tick']['epoch']
        price = float(msg['tick']['quote'])
        bucket = epoch - (epoch % CANDLE_INTERVAL)

        # finaliza vela anterior
        if ticks_current is not None and ticks_current['bucket'] != bucket:
            candle = {
                'epoch': ticks_current['bucket'],
                'open':  ticks_current['open'],
                'high':  ticks_current['high'],
                'low':   ticks_current['low'],
                'close': ticks_current['close']
            }
            finalize_candle_and_detect(ws, candle)

        # inicia ou atualiza vela atual
        if ticks_current is None or ticks_current['bucket'] != bucket:
            ticks_current = {
                'bucket': bucket,
                'open':   price,
                'high':   price,
                'low':    price,
                'close':  price
            }
        else:
            ticks_current['high']  = max(ticks_current['high'], price)
            ticks_current['low']   = min(ticks_current['low'], price)
            ticks_current['close'] = price
        return

    # resposta ao enviar PUT
    if mt == 'buy':
        contract_id = msg['buy']['contract_id']
        ws.send(json.dumps({
            "proposal_open_contract": 1,
            "contract_id": contract_id,
            "subscribe": 1
        }))
        log(f"Contrato comprado: {contract_id}")
        return

    # resultado da operação
    if mt == 'proposal_open_contract':
        pc = msg['proposal_open_contract']
        if not pc.get('is_sold'):
            return
        profit = float(pc['profit'])
        contract_id = None
        if profit > 0:
            log(f"🎉 Vitória! Lucro: {profit:.2f}")
            loss_count    = 0
            current_stake = INITIAL_STAKE
        else:
            log(f"😭 Derrota! Prejuízo: {profit:.2f}")
            loss_count    += 1
            current_stake *= MARTINGALE_FACTOR
            log(f"Martingale Nível {loss_count}, stake agora {current_stake:.2f}")
        return


def on_error(ws, error):
    log(f"[WebSocket Error] {error}")


def on_close(ws, code, msg):
    log(f"Conexão fechada ({code}): {msg}")
    time.sleep(5)
    start_bot()


def start_bot():
    global current_stake, loss_count, contract_id, buy_req_id, candles_data, ticks_current
    current_stake = INITIAL_STAKE
    loss_count    = 0
    contract_id   = None
    buy_req_id    = 1000
    candles_data  = []
    ticks_current = None

    url = f"wss://ws.derivws.com/websockets/v3?app_id={APP_ID}"
    log("Iniciando Deriv Trading Bot (Moura VENDA v2.3 - Ticks)...")
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

