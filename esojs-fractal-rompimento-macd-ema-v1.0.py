import websocket
import json
import time
import pandas as pd
import numpy as np

# --- Configurações do Bot ---
APP_ID                     = 1089
API_TOKEN                  = ''  # Use seu token real aqui
SYMBOL                     = 'R_100'
CANDLE_INTERVAL            = 60       # 1 minuto
INITIAL_STAKE              = 50.0     # Garanta que é float
MARTINGALE_FACTOR          = 2.0      # Garanta que é float
MAX_MARTINGALE_LEVEL       = 1
FRACTAL_PERIOD             = 2        # Ajustado para o padrão da Deriv (2 de cada lado)
HISTORY_COUNT              = 200

# Tolerância para considerar uma vela como "não-Doji"
DOJI_TOLERANCE_PERCENT     = 0.00005  # 0.005%

# --- Estado do Bot ---
current_stake        = INITIAL_STAKE
loss_count           = 0
contract_id          = None
history_req_id       = 1
buy_req_id           = 1000
last_update_epoch    = None
candles_data         = []
last_trade_direction = None
band_break_status    = 'none'  # 'none', 'upper_broken', 'lower_broken'
last_candle_epoch    = 0
pending_gale_trade_delayed = False # Nova flag para gale atrasado

def log(msg):
    ts = time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime())
    print(f"[{ts} GMT] {msg}")

def calculate_fractal_chaos_bands(df, period=FRACTAL_PERIOD):
    df = df.copy()
    if len(df) < period * 2 + 1:
        return None, None

    df.loc[:, 'is_high_fractal'] = False
    df.loc[:, 'is_low_fractal']  = False

    for i in range(period, len(df) - period):
        high = df['high'].iloc[i]
        if high == df['high'].iloc[i-period : i+period+1].max():
            df.loc[df.index[i], 'is_high_fractal'] = True
        low = df['low'].iloc[i]
        if low == df['low'].iloc[i-period : i+period+1].min():
            df.loc[df.index[i], 'is_low_fractal'] = True

    ub_series = df[df['is_high_fractal']]['high'].ffill()
    lb_series = df[df['is_low_fractal']]['low'].ffill()
    ub = ub_series.iloc[-1] if not ub_series.empty else None
    lb = lb_series.iloc[-1] if not lb_series.empty else None

    if lb is not None and ub is not None and lb >= ub:
        log(f"[AVISO] Bandas invertidas detectadas (lower={lb:.5f}, upper={ub:.5f}). Ignorando.")
        return None, None

    return ub, lb

def calculate_ema(series, period):
    return series.ewm(span=period, adjust=False).mean()

def calculate_macd(df, fast_period, slow_period, signal_period):
    if len(df) < slow_period: # MACD precisa de dados suficientes para as EMAs
        return None, None
    
    ema_fast = calculate_ema(df['close'], fast_period)
    ema_slow = calculate_ema(df['close'], slow_period)
    
    macd_line = ema_fast - ema_slow
    
    if len(macd_line) < signal_period: # Linha de Sinal precisa de dados suficientes para sua EMA
        return None, None
        
    signal_line = calculate_ema(macd_line, signal_period)
    
    return macd_line, signal_line

def send_purchase_request(ws, contract_type, expiry_timestamp):
    global current_stake, buy_req_id, contract_id, last_trade_direction

    if contract_id is not None:
        log("❌ Já existe uma operação em andamento.")
        return

    params = {
        "amount": float(f"{current_stake:.2f}"),
        "basis": "stake",
        "contract_type": contract_type,
        "currency": "USD",
        "symbol": SYMBOL,
        "date_expiry": expiry_timestamp
    }
    
    req = {
        "buy": 1,
        "price": float(f"{current_stake:.2f}"),
        "parameters": params,
        "req_id": buy_req_id
    }
    last_trade_direction = contract_type 
    
    log_msg = (
        f"▶ Enviando {contract_type} • "
        f"stake={current_stake:.2f} • "
        f"Expiry={time.strftime('%H:%M:%S', time.gmtime(expiry_timestamp))} GMT"
    )
    
    ws.send(json.dumps(req))
    log(f"{log_msg} (req_id={buy_req_id})")
    buy_req_id += 1

def on_open(ws):
    log("Conexão aberta. Autenticando…")
    ws.send(json.dumps({"authorize": API_TOKEN}))

def on_message(ws, message):
    global history_req_id, contract_id, loss_count, current_stake
    global last_update_epoch, candles_data, last_trade_direction, band_break_status
    global last_candle_epoch, pending_gale_trade_delayed

    msg = json.loads(message)
    if 'error' in msg:
        log(f"[ERRO] {msg['error']['message']}")
        if msg['error']['code'] == 'InvalidToken':
            log("Token inválido. Por favor, verifique seu API_TOKEN.")
            ws.close()
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
        candles_data.clear()
        for c in arr:
            candles_data.append({
                'epoch': c['epoch'],
                'open': float(c['open']),
                'high': float(c['high']),
                'low': float(c['low']),
                'close': float(c['close'])
            })
        log(f"[candles] Histórico carregado: {len(candles_data)} candles")
        if candles_data:
            last_candle_epoch = candles_data[-1]['epoch']
            log(f"Última vela do histórico: {time.strftime('%H:%M:%S', time.gmtime(last_candle_epoch))} GMT")


    elif mt == 'ohlc':
        o = msg['ohlc']
        current_candle_epoch = o['open_time']

        if current_candle_epoch == last_candle_epoch and candles_data:
            candles_data[-1]['high']  = max(candles_data[-1]['high'],  float(o['high']))
            candles_data[-1]['low']   = min(candles_data[-1]['low'],   float(o['low']))
            candles_data[-1]['close'] = float(o['close'])

        elif current_candle_epoch > last_candle_epoch:
            if candles_data:
                # Fechar a vela anterior com o open da nova vela
                candles_data[-1]['close'] = float(o['open']) 
                c = candles_data[-1]
                log(f"--- VELA FECHADA --- Epoch: {c['epoch']}, O:{c['open']:.5f}, H:{c['high']:.5f}, L:{c['low']:.5f}, C:{c['close']:.5f}")

            # Adicionar a nova vela
            candles_data.append({
                'epoch': current_candle_epoch,
                'open':  float(o['open']),
                'high':  float(o['high']),
                'low':   float(o['low']),
                'close': float(o['close'])
            })
            if len(candles_data) > HISTORY_COUNT:
                candles_data.pop(0)
            last_candle_epoch = current_candle_epoch
            
            # --- Lógica de Martingale Atrasada (se o gale imediato não coube na vela anterior) ---
            if pending_gale_trade_delayed and contract_id is None: # Só tenta se não tiver operação ativa
                log(f"✔ GALE atrasado ativado! Enviando {last_trade_direction} com stake {current_stake:.2f} (Epoch: {time.strftime('%H:%M:%S', time.gmtime(current_candle_epoch))} GMT).")
                expiry_timestamp = current_candle_epoch + CANDLE_INTERVAL - 1
                send_purchase_request(ws, last_trade_direction, expiry_timestamp)
                pending_gale_trade_delayed = False # Já enviou, desativa a flag
                return # Já enviou o gale, não procura rompimento nesta vela

            # --- Lógica de Entrada de Rompimento com Confluência ---
            if contract_id is not None:
                log("→ Operação em andamento. Aguardando resultado para nova análise.")
                return # Se tem operação, não faz nada até ela ser vendida

            df = pd.DataFrame(candles_data)
            if len(df) < FRACTAL_PERIOD * 2 + 1:
                log("→ Histórico insuficiente para cálculo de rompimento.")
                return

            log(f"→ Nova vela aberta. Epoch: {time.strftime('%H:%M:%S', time.gmtime(current_candle_epoch))} GMT. Procurando rompimento…")

            # --- Cálculo dos Indicadores ---
            ub, lb = calculate_fractal_chaos_bands(df.iloc[:-1]) # Analisa com base nas velas fechadas
            if ub is None or lb is None:
                log("→ Bandas inválidas ou dados insuficientes.")
                return

            macd_line, signal_line = calculate_macd(df, 12, 26, 9)
            if macd_line is None or signal_line is None or len(macd_line) < 2 or len(signal_line) < 2:
                log("→ Dados insuficientes para cálculo de MACD.")
                return

            ema5 = calculate_ema(df['close'], 5)
            ema10 = calculate_ema(df['close'], 10)
            if len(ema5) < 2 or len(ema10) < 2:
                log("→ Dados insuficientes para cálculo de EMAs.")
                return

            prev_candle = df.iloc[-2] # A vela que acabou de fechar
            
            # --- Lógica de Reset da Memória de Banda ---
            if band_break_status != 'none':
                if lb < prev_candle['close'] < ub:
                    log(f"   [INFO] Vela anterior fechou entre as bandas ATUAIS (L={lb:.5f}, U={ub:.5f}). Resetando status de banda.")
                    band_break_status = 'none'
            
            body = abs(prev_candle['close'] - prev_candle['open'])
            th   = prev_candle['open'] * DOJI_TOLERANCE_PERCENT
            is_doji = body <= th

            bull_candle = prev_candle['close'] > prev_candle['open'] and not is_doji
            bear_candle = prev_candle['close'] < prev_candle['open'] and not is_doji
            
            up_break = prev_candle['close'] > ub and df.iloc[-3]['close'] <= ub
            down_break = prev_candle['close'] < lb and df.iloc[-3]['close'] >= lb

            # --- Confluência dos Indicadores ---
            # MACD: Linha MACD acima da Linha de Sinal para alta, abaixo para baixa
            macd_up_confluence = macd_line.iloc[-2] > signal_line.iloc[-2] # Usar a vela fechada
            macd_down_confluence = macd_line.iloc[-2] < signal_line.iloc[-2] # Usar a vela fechada

            # EMAs: EMA5 acima de EMA10 para alta, abaixo para baixa
            ema_up_confluence = ema5.iloc[-2] > ema10.iloc[-2] # Usar a vela fechada
            ema_down_confluence = ema5.iloc[-2] < ema10.iloc[-2] # Usar a vela fechada


            # Calcula a expiração para o final da VELA ATUAL (a que acabou de abrir)
            expiry_timestamp = current_candle_epoch + CANDLE_INTERVAL - 1

            if expiry_timestamp - int(time.time()) < 5:
                 log(f"   [AVISO] Tempo insuficiente para expiração da entrada inicial ({expiry_timestamp - int(time.time())}s).")
                 return

            # --- Condições Finais de Entrada com Confluência ---
            if up_break and bull_candle and band_break_status == 'none' and macd_up_confluence and ema_up_confluence:
                log(f"🔥 Rompimento de ALTA + MACD de Alta + EMA5 > EMA10 detectado! {prev_candle['close']:.5f} > {ub:.5f}. Enviando CALL.")
                send_purchase_request(ws, "CALL", expiry_timestamp)
                band_break_status = 'upper_broken'
            elif down_break and bear_candle and band_break_status == 'none' and macd_down_confluence and ema_down_confluence:
                log(f"🔥 Rompimento de BAIXA + MACD de Baixa + EMA5 < EMA10 detectado! {prev_candle['close']:.5f} < {lb:.5f}. Enviando PUT.")
                send_purchase_request(ws, "PUT", expiry_timestamp)
                band_break_status = 'lower_broken'
            else:
                log(f"→ Nenhum rompimento fresco detectado ou confluência não atendida. Status banda: {band_break_status}")


    elif mt == 'buy':
        contract_id = msg['buy']['contract_id']
        log(f"Contrato comprado: {contract_id}. Valor de entrada: {msg['buy']['buy_price']:.2f}")
        ws.send(json.dumps({
            "proposal_open_contract": 1,
            "contract_id": contract_id,
            "subscribe": 1
        }))

    elif mt == 'proposal_open_contract':
        pc = msg['proposal_open_contract']
        if not pc.get('is_sold'):
            return 

        contract_id = None # Libera o contract_id assim que o resultado é recebido
        profit = float(pc['profit'])
        
        if profit > 0:
            log(f"🎉 Vitória! Lucro: +{profit:.2f} USD")
            loss_count = 0
            current_stake = INITIAL_STAKE
            band_break_status = 'none' # Resetar após vitória também, pois o ciclo de rompimento/retorno pode ser considerado finalizado
            pending_gale_trade_delayed = False # Zera a flag de gale atrasado em caso de vitória
        else:
            log(f"😭 Derrota! Prejuízo: {profit:.2f} USD")
            loss_count += 1
            
            if loss_count <= MAX_MARTINGALE_LEVEL:
                current_stake = INITIAL_STAKE * (MARTINGALE_FACTOR ** loss_count)
                log(f"   Aplicando Martingale nível {loss_count}. Próximo stake: {current_stake:.2f} USD")

                now = int(time.time())
                current_candle_start_epoch = (now // CANDLE_INTERVAL) * CANDLE_INTERVAL
                expiry_timestamp_gale = current_candle_start_epoch + CANDLE_INTERVAL - 1
                
                time_left_gale = expiry_timestamp_gale - now
                
                if time_left_gale >= 5:
                    log(f"✔ GALE ativado! Enviando {last_trade_direction} com stake {current_stake:.2f} para expirar NESTA VELA ({time.strftime('%H:%M:%S', time.gmtime(expiry_timestamp_gale))} GMT).")
                    send_purchase_request(ws, last_trade_direction, expiry_timestamp_gale)
                    pending_gale_trade_delayed = False # Gale imediato enviado, desativa a flag
                else:
                    log(f"   [AVISO] Tempo insuficiente ({time_left_gale}s) para GALE NESTA VELA. Agendando para a próxima vela.")
                    pending_gale_trade_delayed = True # Ativa para o próximo OHLC
            else:
                log(f"   Limite de Martingale ({MAX_MARTINGALE_LEVEL} níveis) atingido. Resetando stake.")
                loss_count = 0
                current_stake = INITIAL_STAKE
                band_break_status = 'none' # Resetar o status da banda ao atingir limite de martingale
                pending_gale_trade_delayed = False # Zera a flag de gale atrasado ao resetar Martingale

    elif mt == 'error':
        log(f"[ERRO API] {msg['error']['message']} (Code: {msg['error'].get('code')})")

def on_error(ws, error):
    log(f"[ERRO WEBSOCKET] {error}. Reconectando em 5s…")
    ws.close()

def on_close(ws, code, msg):
    log(f"[CONEXÃO FECHADA] Código: {code} / Mensagem: {msg}. Reiniciando bot em 5s…")
    time.sleep(5)
    start_bot()

def start_bot():
    global current_stake, loss_count, contract_id, candles_data, last_update_epoch
    global last_trade_direction, history_req_id, buy_req_id, band_break_status
    global last_candle_epoch, pending_gale_trade_delayed

    current_stake        = INITIAL_STAKE
    loss_count           = 0
    contract_id          = None
    candles_data         = []
    last_update_epoch    = None
    last_trade_direction = None
    history_req_id       = 1
    buy_req_id           = 1000
    band_break_status    = 'none'
    last_candle_epoch    = 0
    pending_gale_trade_delayed = False

    url = f"wss://ws.derivws.com/websockets/v3?app_id={APP_ID}"
    log("Iniciando Deriv Trading Bot (v15.4 – Fractal, Martingale Imediato/00 próxima vela, Nova Memória de Banda, Confluência MACD/EMA, Reconnect)…")
    log(f"Configurações: Stake Inicial={INITIAL_STAKE}, Fator Martingale={MARTINGALE_FACTOR}, Níveis Martingale={MAX_MARTINGALE_LEVEL}, Período Fractal={FRACTAL_PERIOD}")
    
    while True:
        try:
            ws_app = websocket.WebSocketApp(
                url,
                on_open=on_open,
                on_message=on_message,
                on_error=on_error,
                on_close=on_close
            )
            ws_app.run_forever(ping_interval=20, ping_timeout=10)
        except Exception as e:
            log(f"[ERRO FATAL NO BOT] {e}. Tentando reiniciar em 10s…")
            time.sleep(10)

if __name__ == "__main__":
    start_bot()
