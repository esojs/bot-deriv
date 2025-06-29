# Nome do arquivo: cruzamento-de-medias-e-macd-deriv.py
# Versão: 1.6 (Adicionado Log de Diagnóstico Detalhado e Horário GMT)
# Descrição: Bot para Deriv com estratégia de Cruzamento de EMAs e confirmação do MACD.

import websocket
import json
import time
import pandas as pd
import numpy as np
from datetime import datetime, timedelta, timezone

# --- Configurações do Bot ---
APP_ID               = 1089
API_TOKEN            = '' # <-- ATUALIZE SEU TOKEN AQUI!
SYMBOL               = 'R_100'           # Ativo para operar (Volatilidade 100 Index)
CANDLE_INTERVAL      = 60                # Em segundos (1 minuto)
INITIAL_STAKE        = 0.35              # Valor da operação (ex: 0.35 USD)
MARTINGALE_FACTOR    = 2.1
MAX_MARTINGALE_LEVEL = 1
HISTORY_COUNT        = 200               # Número de velas para manter no histórico

# --- Configurações da Estratégia ---
EMA_FAST_PERIOD      = 5                 # Período da EMA rápida
EMA_SLOW_PERIOD      = 10                # Período da EMA lenta
MACD_FAST_PERIOD     = 12                # Períodos do MACD (padrão 12, 26, 9)
MACD_SLOW_PERIOD     = 26
MACD_SIGNAL_PERIOD   = 9

# Tolerância para considerar uma vela como "não-Doji"
DOJI_TOLERANCE_PERCENT = 0.00005 # 0.005%


# --- Estado Global do Bot ---
candles_data         = []
contract_id          = None
current_stake        = INITIAL_STAKE
loss_count           = 0
last_trade_direction = None
last_update_epoch    = None
current_balance      = 0
buy_req_id           = 1000


# --- Definições de Funções ---

def log(msg):
    """
    Função de log que imprime o timestamp em GMT.
    """
    ts = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S GMT')
    print(f"[{ts}] {msg}")

def send_purchase_request(ws, contract_type, stake_amount, expiry_timestamp):
    global contract_id, buy_req_id, last_trade_direction
    if contract_id is not None:
        log("❌ Já existe uma operação em andamento.")
        return

    params = {
        "amount": float(f"{stake_amount:.2f}"),
        "basis": "stake",
        "contract_type": contract_type,
        "currency": "USD",
        "symbol": SYMBOL,
        "date_expiry": int(expiry_timestamp)
    }
    
    expiry_gmt_time = datetime.fromtimestamp(expiry_timestamp, tz=timezone.utc).strftime('%H:%M:%S GMT')
    log_msg = f"▶ ENVIANDO ORDEM: {contract_type.upper()} • Stake={stake_amount:.2f} • Expira: {expiry_gmt_time}"
    req = {"buy": 1, "price": float(f"{stake_amount:.2f}"), "parameters": params, "req_id": buy_req_id}
    
    last_trade_direction = contract_type
    ws.send(json.dumps(req))
    log(f"{log_msg} (req_id={buy_req_id})")
    buy_req_id += 1

def check_strategy_and_trade(ws, trade_entry_epoch):
    global candles_data, contract_id, current_stake, loss_count, last_trade_direction

    min_candles_required = max(EMA_SLOW_PERIOD, MACD_SLOW_PERIOD, MACD_SIGNAL_PERIOD) + 4 
    
    if len(candles_data) < min_candles_required:
        log(f"[DEBUG] Número insuficiente de velas para análise da estratégia ({len(candles_data)}/{min_candles_required} mínimo).")
        return

    if contract_id is not None:
        return

    df = pd.DataFrame(candles_data)
    
    df.loc[:, 'ema_fast'] = df['close'].ewm(span=EMA_FAST_PERIOD, adjust=False).mean()
    df.loc[:, 'ema_slow'] = df['close'].ewm(span=EMA_SLOW_PERIOD, adjust=False).mean()
    
    ema_macd_fast = df['close'].ewm(span=MACD_FAST_PERIOD, adjust=False).mean()
    ema_macd_slow = df['close'].ewm(span=MACD_SLOW_PERIOD, adjust=False).mean()
    df.loc[:, 'macd_line'] = ema_macd_fast - ema_macd_slow
    df.loc[:, 'signal_line'] = df['macd_line'].ewm(span=MACD_SIGNAL_PERIOD, adjust=False).mean()

    if len(df) < 4: 
        log("[DEBUG] DataFrame muito pequeno para análise da estratégia (menos de 4 velas após cálculos).")
        return
        
    vela_conf     = df.iloc[-2] # Vela de Confirmação (Y)
    vela_cruz     = df.iloc[-3] # Vela do Cruzamento (X)
    vela_anterior = df.iloc[-4] # Vela Anterior ao Cruzamento

    # --- Debugging: Log dos valores das velas relevantes ---
    log(f"[DEBUG] --- Análise da Vela de Entrada {datetime.fromtimestamp(trade_entry_epoch, tz=timezone.utc).strftime('%Y-%m-%d %H:%M:%S GMT')} ---")
    log(f"[DEBUG] Vela Anterior ({datetime.fromtimestamp(vela_anterior['epoch'], tz=timezone.utc).strftime('%H:%M:%S GMT')}): EMA F={vela_anterior['ema_fast']:.4f}, S={vela_anterior['ema_slow']:.4f}")
    log(f"[DEBUG] Vela Cruzamento ({datetime.fromtimestamp(vela_cruz['epoch'], tz=timezone.utc).strftime('%H:%M:%S GMT')}): O={vela_cruz['open']:.4f}, C={vela_cruz['close']:.4f}, EMA F={vela_cruz['ema_fast']:.4f}, S={vela_cruz['ema_slow']:.4f}")
    log(f"[DEBUG] Vela Confirmação ({datetime.fromtimestamp(vela_conf['epoch'], tz=timezone.utc).strftime('%H:%M:%S GMT')}): O={vela_conf['open']:.4f}, C={vela_conf['close']:.4f}, MACD={vela_conf['macd_line']:.4f}, Signal={vela_conf['signal_line']:.4f}")

    is_vela_cruz_doji = abs(vela_cruz['close'] - vela_cruz['open']) <= (vela_cruz['open'] * DOJI_TOLERANCE_PERCENT)
    is_vela_conf_doji = abs(vela_conf['close'] - vela_conf['open']) <= (vela_conf['open'] * DOJI_TOLERANCE_PERCENT)

    cor_cruz_verde    = vela_cruz['close'] > vela_cruz['open'] and not is_vela_cruz_doji
    cor_cruz_vermelha = vela_cruz['close'] < vela_cruz['open'] and not is_vela_cruz_doji
    cor_conf_verde    = vela_conf['close'] > vela_conf['open'] and not is_vela_conf_doji
    cor_conf_vermelha = vela_conf['close'] < vela_conf['open'] and not is_vela_conf_doji

    cruzou_para_cima  = vela_anterior['ema_fast'] <= vela_anterior['ema_slow'] and vela_cruz['ema_fast'] > vela_cruz['ema_slow']
    cruzou_para_baixo = vela_anterior['ema_fast'] >= vela_anterior['ema_slow'] and vela_cruz['ema_fast'] < vela_cruz['ema_slow']
    
    macd_conf_compra  = vela_conf['macd_line'] > vela_conf['signal_line']
    macd_conf_venda   = vela_conf['macd_line'] < vela_conf['signal_line']
    
    expiry_for_trade = trade_entry_epoch + CANDLE_INTERVAL - 1 

    # --- Debugging: Log do resultado de cada condição ---
    log(f"[DEBUG] Condições Avaliadas para Entrada:")
    log(f"[DEBUG]   Cruzamento para Cima (Vela Cruzamento): {cruzou_para_cima}")
    log(f"[DEBUG]   Cruzamento para Baixo (Vela Cruzamento): {cruzou_para_baixo}")
    log(f"[DEBUG]   Vela Cruzamento VERDE: {cor_cruz_verde}")
    log(f"[DEBUG]   Vela Cruzamento VERMELHA: {cor_cruz_vermelha}")
    log(f"[DEBUG]   Vela Cruzamento é DOJI: {is_vela_cruz_doji}")
    log(f"[DEBUG]   Vela Confirmação VERDE: {cor_conf_verde}")
    log(f"[DEBUG]   Vela Confirmação VERMELHA: {cor_conf_vermelha}")
    log(f"[DEBUG]   Vela Confirmação é DOJI: {is_vela_conf_doji}")
    log(f"[DEBUG]   MACD Confirmação COMPRA (MACD > Signal): {macd_conf_compra}")
    log(f"[DEBUG]   MACD Confirmação VENDA (MACD < Signal): {macd_conf_venda}")


    if cruzou_para_cima and cor_cruz_verde and cor_conf_verde and macd_conf_compra:
        log(f"✔ SINAL DE COMPRA: Cruzamento de EMAs e MACD confirmados.")
        send_purchase_request(ws, "CALL", current_stake, expiry_for_trade)
    elif cruzou_para_baixo and cor_cruz_vermelha and cor_conf_vermelha and macd_conf_venda:
        log(f"✔ SINAL DE VENDA: Cruzamento de EMAs e MACD confirmados.")
        send_purchase_request(ws, "PUT", current_stake, expiry_for_trade)
    else:
        # Apenas loga se nenhuma condição foi atendida. Não é um erro.
        log("→ Nenhuma condição de entrada atendida.")

def on_message(ws, message):
    global current_stake, loss_count, contract_id, last_trade_direction, current_balance, last_update_epoch, candles_data, buy_req_id

    msg = json.loads(message)
    if 'error' in msg:
        log(f"❌ [ERRO API] {msg['error']['message']}")
        if 'Expiry time cannot be in the past' in msg['error']['message']:
            contract_id = None
        return
    mt = msg.get('msg_type')

    if mt == 'authorize':
        loginid = msg['authorize']['loginid']
        log(f"✅ Autenticado como {loginid}")
        ws.send(json.dumps({
            "ticks_history": SYMBOL,
            "granularity": CANDLE_INTERVAL,
            "style": "candles",
            "subscribe": 1,
            "count": HISTORY_COUNT,
            "end": "latest",
            "req_id": 1
        }))
        log(f"🔔 Subscrito em velas de {SYMBOL} (intervalo: {CANDLE_INTERVAL}s)")
        ws.send(json.dumps({"balance": 1, "subscribe": 1}))

    elif mt == 'candles':
        arr = msg.get('candles', [])
        log(f"🕯️ Histórico recebido com {len(arr)} velas iniciais.")
        candles_data.clear()
        for c in arr:
            candles_data.append({'epoch': c['epoch'], 'open': float(c['open']), 'high': float(c['high']), 'low': float(c['low']), 'close': float(c['close'])})
        
    elif mt == 'ohlc':
        o = msg['ohlc']
        current_ohlc_epoch = o['open_time']
        
        if current_ohlc_epoch == last_update_epoch:
            # Atualiza apenas a vela atual se ainda não fechou
            if len(candles_data) > 0:
                candles_data[-1]['high'] = max(candles_data[-1]['high'], float(o['high']))
                candles_data[-1]['low'] = min(candles_data[-1]['low'], float(o['low']))
                candles_data[-1]['close'] = float(o['close'])
            return

        # Uma nova vela abriu. A vela anterior fechou.
        if last_update_epoch is not None and len(candles_data) > 0:
            log("[INFO] Analisando nova vela...")
            
            # Garante que a vela anterior tenha seu 'close' correto antes da análise.
            # No Deriv, o 'ohlc' da nova vela traz o 'open' da vela que acabou de abrir.
            # O 'close' da vela anterior é o 'open' da nova, se não houver gap.
            # No entanto, para fins de cálculo da estratégia, usamos o 'close'
            # recebido no pacote ohlc anterior para a vela que fechou.
            # Como a API envia a vela completa ao final de cada período, candles_data[-1]['close']
            # já deve estar atualizado no momento de chamar check_strategy_and_trade.
            check_strategy_and_trade(ws, current_ohlc_epoch)

        new_ohlc_candle_data = {
            'epoch': current_ohlc_epoch, 
            'open': float(o['open']), 
            'high': float(o['high']), 
            'low': float(o['low']), 
            'close': float(o['close'])
        }
        candles_data.append(new_ohlc_candle_data)
        if len(candles_data) > HISTORY_COUNT:
            candles_data.pop(0) 
        
        last_update_epoch = current_ohlc_epoch

    elif mt == 'buy':
        if 'error' in msg:
             log(f"❌ Falha na compra do contrato: {msg['error']['message']}")
             contract_id = None
        else:
             contract_id = msg['buy']['contract_id']
             ws.send(json.dumps({"proposal_open_contract": 1, "contract_id": contract_id, "subscribe": 1}))
             log(f"📈 Contrato comprado com sucesso: {contract_id}")

    elif mt == 'proposal_open_contract':
        pc = msg['proposal_open_contract']
        if not pc.get('is_sold'):
            return

        contract_id = None # Libera para nova operação
        profit = float(pc['profit'])

        if profit > 0:
            log(f"🎉 VITÓRIA! Lucro: ${profit:.2f}")
            loss_count = 0
            current_stake = INITIAL_STAKE
        else:
            log(f"😭 DERROTA! Perda: ${abs(profit):.2f}")
            loss_count += 1
            if loss_count <= MAX_MARTINGALE_LEVEL:
                current_stake *= MARTINGALE_FACTOR
                log(f"🟠 Aplicando Martingale {loss_count}, nova stake ${current_stake:.2f}")
                
                # Para Martingale, a nova entrada é na próxima vela.
                # O epoch da "nova vela" é o current_ohlc_epoch da última vela completa,
                # que já foi definida quando o on_message recebeu o 'ohlc' que fechou a vela anterior.
                # Então, trade_entry_epoch é o epoch da vela ATUAL que está se formando,
                # onde a operação de martingale será executada.
                expiry_for_martingale_trade = candles_data[-1]['epoch'] + CANDLE_INTERVAL - 1
                
                send_purchase_request(ws, last_trade_direction, current_stake, expiry_for_martingale_trade)
            else:
                log("🛑 Limite de Martingale atingido. Resetando stake.")
                loss_count = 0
                current_stake = INITIAL_STAKE
        
    elif mt == 'balance':
        current_balance = msg['balance']['balance']
        log(f"💰 Saldo atualizado: ${current_balance:.2f}")


def on_open(ws):
    log("🔌 Conexão aberta. Autenticando...")
    ws.send(json.dumps({"authorize": API_TOKEN}))

def on_error(ws, error):
    log(f"🔥 [ERRO WS] {error}")

def on_close(ws, code, msg):
    log(f"🔌 Conexão WS fechada. Código: {code}, Mensagem: {msg}")

def start_bot():
    global current_stake, loss_count, contract_id, candles_data, last_update_epoch, last_trade_direction, current_balance, buy_req_id
    
    current_stake        = INITIAL_STAKE
    loss_count           = 0
    contract_id          = None
    candles_data         = []
    last_update_epoch    = None
    last_trade_direction = None
    current_balance      = 0
    buy_req_id           = 1000 
    
    url = f"wss://ws.derivws.com/websockets/v3?app_id={APP_ID}"
    log(f"🚀 Iniciando Deriv Trading Bot para {SYMBOL}...")
    
    ws_app = websocket.WebSocketApp(url,
                                    on_open=on_open,
                                    on_message=on_message,
                                    on_error=on_error,
                                    on_close=on_close)
    
    # Adicionado ping_interval e ping_timeout para manter a conexão ativa
    ws_app.run_forever(ping_interval=20, ping_timeout=10, reconnect=5)

if __name__ == "__main__":
    while True:
        try:
            start_bot()
            log("🔌 Bot desconectado. Tentando reconectar em 15 segundos...")
            time.sleep(15)
        except Exception as e:
            log(f"🔥 Um erro fatal ocorreu no loop principal: {e}")
            log("💤 Reiniciando o bot em 30 segundos...")
            time.sleep(30)
