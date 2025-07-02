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
STAKE                      = 1.0      # Valor fixo para cada operação (garantir float)
FRACTAL_PERIOD             = 2        # Fractais de 2 candles antes/depois
HISTORY_COUNT              = 200      # Número de candles no histórico para análise

# Tolerância para considerar uma vela como "não-Doji"
DOJI_TOLERANCE_PERCENT     = 0.00005  # 0.005%

# --- Estado do Bot ---
contract_id                = None
history_req_id             = 1
buy_req_id                 = 1000
last_update_epoch          = None     # Epoch da última atualização de candle (intra-vela)
candles_data               = []
awaiting_confirmation      = None     # Armazena 'CALL' ou 'PUT' se um rompimento foi detectado e aguarda confirmação
confirmation_candle_epoch  = None     # Epoch da vela em que a confirmação é esperada
band_break_status          = 'none'   # 'none', 'upper_broken', 'lower_broken' - NOVO/REINTRODUZIDO

def log(msg):
    ts = time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime())
    print(f"[{ts} GMT] {msg}")

def calculate_fractal_chaos_bands(df, period=FRACTAL_PERIOD):
    # Garante que temos dados suficientes para o cálculo do fractal
    if len(df) < period * 2 + 1:
        return None, None
    
    df_copy = df.copy() # Trabalha em uma cópia para evitar warnings
    df_copy['is_high_fractal'] = False
    df_copy['is_low_fractal'] = False

    # Identifica fractais (ponto alto/baixo que tem 'period' candles menores/maiores de cada lado)
    for i in range(period, len(df_copy) - period):
        high = df_copy['high'].iloc[i]
        if high == df_copy['high'].iloc[i-period : i+period+1].max():
            df_copy.at[df_copy.index[i], 'is_high_fractal'] = True
        
        low = df_copy['low'].iloc[i]
        if low == df_copy['low'].iloc[i-period : i+period+1].min():
            df_copy.at[df_copy.index[i], 'is_low_fractal'] = True

    # Preenche as bandas com os últimos fractais válidos
    ub_series = df_copy[df_copy['is_high_fractal']]['high'].ffill()
    lb_series = df_copy[df_copy['is_low_fractal']]['low'].ffill()
    
    ub = ub_series.iloc[-1] if not ub_series.empty else None
    lb = lb_series.iloc[-1] if not lb_series.empty else None
    
    # Validação para evitar bandas invertidas ou inválidas
    if lb is not None and ub is not None and lb >= ub:
        log(f"[AVISO] Bandas invertidas detectadas (lower={lb:.5f}, upper={ub:.5f}). Retornando nulo.")
        return None, None
        
    return ub, lb

def send_purchase_request(ws, contract_type, expiry_duration_minutes):
    global buy_req_id, contract_id
    
    if contract_id is not None:
        log("❌ Já existe uma operação em andamento. Não é possível enviar nova compra.")
        return
    
    params = {
        "amount": float(f"{STAKE:.2f}"),
        "basis": "stake",
        "contract_type": contract_type,
        "currency": "USD",
        "symbol": SYMBOL,
        "duration": expiry_duration_minutes,
        "duration_unit": "m"
    }
    
    req = {
        "buy": 1,
        "price": float(f"{STAKE:.2f}"),
        "parameters": params,
        "req_id": buy_req_id
    }
    
    log_msg = (
        f"▶ Enviando {contract_type} • "
        f"stake={STAKE:.2f} • "
        f"Duração={expiry_duration_minutes} minuto(s) (req_id={buy_req_id})"
    )
    
    ws.send(json.dumps(req))
    log(log_msg)
    buy_req_id += 1

def on_open(ws):
    log("Conexão aberta. Autenticando…")
    ws.send(json.dumps({"authorize": API_TOKEN}))

def on_message(ws, message):
    global history_req_id, contract_id, last_update_epoch, candles_data
    global awaiting_confirmation, confirmation_candle_epoch, band_break_status # Adicionado band_break_status

    msg = json.loads(message)
    if 'error' in msg:
        log(f"[ERRO API] {msg['error']['message']}")
        if msg['error']['code'] == 'InvalidToken':
            log("Token inválido. Por favor, verifique seu API_TOKEN.")
            ws.close()
        return
    
    mt = msg.get('msg_type')

    if mt == 'authorize':
        loginid = msg['authorize']['loginid']
        log(f"Autenticado como {loginid}")
        # Solicita o histórico de candles e subscreve para atualizações futuras
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

    elif mt == 'candles':
        # Carrega o histórico inicial de candles ao iniciar
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
            # last_update_epoch será o epoch da última vela completa do histórico
            last_update_epoch = candles_data[-1]['epoch']
            log(f"Última vela do histórico: {time.strftime('%H:%M:%S', time.gmtime(last_update_epoch))} GMT")
        return

    # Processamento de velas (ohlc - open, high, low, close)
    elif mt == 'ohlc':
        o = msg['ohlc']
        current_ohlc_epoch = o['open_time'] # Epoch de início da vela atual (que está abrindo ou em atualização)

        # Se a mensagem OHLC é para a vela que já estamos acompanhando (ainda aberta)
        if current_ohlc_epoch == last_update_epoch:
            # Atualiza os dados de high, low e close da vela mais recente
            c = candles_data[-1]
            c['high'] = max(c['high'], float(o['high']))
            c['low']  = min(c['low'],  float(o['low']))
            c['close']= float(o['close'])
            return # Não faz mais nada, pois a vela ainda está aberta

        # Se é uma NOVA vela (significa que a vela anterior fechou)
        elif current_ohlc_epoch > last_update_epoch:
            if candles_data:
                # Antes de adicionar a nova vela, "fecha" a vela anterior com o 'open' da nova
                candles_data[-1]['close'] = float(o['open']) 
                closed_candle_info = candles_data[-1]
                log(f"--- VELA FECHADA --- Epoch: {closed_candle_info['epoch']}, O:{closed_candle_info['open']:.5f}, H:{closed_candle_info['high']:.5f}, L:{closed_candle_info['low']:.5f}, C:{closed_candle_info['close']:.5f}")

            # Adiciona a nova vela ao histórico
            candles_data.append({
                'epoch': current_ohlc_epoch,
                'open':  float(o['open']),
                'high':  float(o['high']),
                'low':   float(o['low']),
                'close': float(o['close']) # O close da vela recém-aberta é o tick atual
            })
            # Mantém o histórico no tamanho definido
            if len(candles_data) > HISTORY_COUNT:
                candles_data.pop(0) 
            last_update_epoch = current_ohlc_epoch # Atualiza o epoch da vela mais recente (agora aberta)

            df = pd.DataFrame(candles_data) # Cria DataFrame com os dados atualizados

            # --- Lógica de Reset da Memória de Banda (APÓS fechamento de vela) ---
            if band_break_status != 'none' and len(df) >= 2:
                # Usa a vela que acabou de fechar para verificar se retornou às bandas atuais
                last_closed_candle_for_reset = df.iloc[-2]
                ub, lb = calculate_fractal_chaos_bands(df.iloc[:-1]) # Recalcula bandas para verificar posição atual
                if ub and lb:
                    if lb < last_closed_candle_for_reset['close'] < ub:
                        log(f"   [INFO] Vela anterior (Epoch {last_closed_candle_for_reset['epoch']}) fechou entre as bandas ATUAIS (L={lb:.5f}, U={ub:.5f}). Resetando status de banda.")
                        band_break_status = 'none'

            # --- Lógica de Confirmação e Entrada ---
            # Se estamos aguardando uma confirmação de rompimento
            if awaiting_confirmation:
                # A vela que acabou de fechar é a vela de CONFIRMAÇÃO (df.iloc[-2])
                conf_candle = df.iloc[-2]
                
                body = abs(conf_candle['close'] - conf_candle['open'])
                doji_th = conf_candle['open'] * DOJI_TOLERANCE_PERCENT
                
                # Verifica se a vela de confirmação é de alta e não é Doji
                bullish_conf = conf_candle['close'] > conf_candle['open'] and body > doji_th
                # Verifica se a vela de confirmação é de baixa e não é Doji
                bearish_conf = conf_candle['close'] < conf_candle['open'] and body > doji_th

                log(f"⏱ Vela de confirmação (Epoch {conf_candle['epoch']}) fechou. Close={conf_candle['close']:.5f}. Esperado: {awaiting_confirmation}")

                # Se a confirmação for válida (vela fechou na direção esperada e não é Doji)
                # E NÃO há outra operação em andamento
                if contract_id is None: # Garante que não compra se já estiver operando
                    if (awaiting_confirmation == 'CALL' and bullish_conf) or \
                       (awaiting_confirmation == 'PUT' and bearish_conf):
                        log(f"✔ Confirmação de {awaiting_confirmation} válida: entrando IMEDIATO.")
                        # A entrada é feita para expirar no final da VELA ATUAL (a que acabou de abrir)
                        send_purchase_request(ws, awaiting_confirmation, 1) # Duração de 1 minuto
                    else:
                        log(f"❌ Confirmação falhou (esperado {awaiting_confirmation}). Vela não confirmou ou é Doji.")
                else:
                    log("→ Operação em andamento. Não é possível entrar, mesmo com confirmação válida.")
                
                # Reseta o estado de espera de confirmação APÓS a tentativa de entrada
                awaiting_confirmation = None
                confirmation_candle_epoch = None
                return # Se já lidou com a confirmação, não procura por novo rompimento nesta mesma vela

            # --- Lógica de Detecção de Rompimento (se não houver operação e histórico suficiente) ---
            if contract_id is None and band_break_status == 'none' and len(df) >= 3: # Adicionada condição band_break_status
                last_closed_candle = df.iloc[-2] # A vela que acabou de fechar (potencial rompimento)
                prev_to_last_closed = df.iloc[-3] # A vela anterior à vela de rompimento

                # Calcula as bandas de fractal usando as velas fechadas, exceto a que acabou de abrir
                ub, lb = calculate_fractal_chaos_bands(df.iloc[:-1]) 
                
                if ub and lb:
                    # Condição de rompimento de ALTA: vela anterior fechou acima da banda E a antepenúltima estava abaixo/na banda
                    if last_closed_candle['close'] > ub and prev_to_last_closed['close'] <= ub:
                        awaiting_confirmation = 'CALL'
                        # A próxima vela (a que acabou de abrir) será a vela de confirmação
                        confirmation_candle_epoch = df.iloc[-1]['epoch'] 
                        band_break_status = 'upper_broken' # Marca a banda como rompida
                        log(f"�� Rompimento de ALTA detectado pela vela (Epoch: {last_closed_candle['epoch']}). Aguardando confirmação na vela Epoch {confirmation_candle_epoch}.")
                    # Condição de rompimento de BAIXA: vela anterior fechou abaixo da banda E a antepenúltima estava acima/na banda
                    elif last_closed_candle['close'] < lb and prev_to_last_closed['close'] >= lb:
                        awaiting_confirmation = 'PUT'
                        # A próxima vela (a que acabou de abrir) será a vela de confirmação
                        confirmation_candle_epoch = df.iloc[-1]['epoch']
                        band_break_status = 'lower_broken' # Marca a banda como rompida
                        log(f"🔥 Rompimento de BAIXA detectado pela vela (Epoch: {last_closed_candle['epoch']}). Aguardando confirmação na vela Epoch {confirmation_candle_epoch}.")
                    else:
                        log(f"→ Nenhum rompimento fresco detectado. Status banda: {band_break_status}")
                else:
                    log("→ Bandas Fractal não puderam ser calculadas ou são inválidas.")
            elif contract_id is not None:
                log("→ Operação em andamento. Não procurando novo rompimento.")
            elif band_break_status != 'none':
                log(f"→ Banda já rompida ({band_break_status}). Aguardando retorno do preço ou conclusão do trade.")
            else:
                log("→ Histórico insuficiente para buscar rompimento.")
            return

    # Resposta da compra de contrato
    elif mt == 'buy':
        contract_id = msg['buy']['contract_id']
        log(f"Contrato comprado: {contract_id}. Valor de entrada: {msg['buy']['buy_price']:.2f}")
        # Subscreve para receber atualizações sobre este contrato específico
        ws.send(json.dumps({"proposal_open_contract": 1, "contract_id": contract_id, "subscribe": 1}))
        return

    # Atualizações e resultado do contrato aberto
    elif mt == 'proposal_open_contract':
        pc = msg['proposal_open_contract']
        if not pc.get('is_sold'): # Se ainda não foi vendido, ignora
            return 
        
        # O contrato foi vendido (resultado disponível)
        profit = float(pc['profit'])
        log(f"{'🎉 Vitória' if profit > 0 else '😭 Derrota'}! {('Lucro' if profit > 0 else 'Prejuízo')}: {profit:.2f}")
        
        contract_id = None # Libera o slot para uma nova operação
        
        if profit > 0:
            log("[INFO] Vitória! Resetando status de banda.")
            band_break_status = 'none' # Resetar status da banda após vitória

def on_error(ws, error):
    log(f"[ERRO WEBSOCKET] {error}. Reconectando em 5s…")
    # Fechar a conexão fará com que o loop em 'start_bot' tente reconectar
    ws.close()

def on_close(ws, code, msg):
    log(f"[CONEXÃO FECHADA] Código: {code} / Mensagem: {msg}. Reiniciando bot em 5s…")
    # A pausa aqui é para que o loop externo não tente reconectar imediatamente
    time.sleep(5) 

def start_bot():
    """
    Função principal para iniciar e gerenciar o ciclo de vida do bot, incluindo a reconexão.
    """
    # Reinicia todas as variáveis de estado do bot a cada nova tentativa de inicialização/reconexão
    global contract_id, history_req_id, buy_req_id, last_update_epoch, candles_data
    global awaiting_confirmation, confirmation_candle_epoch, band_break_status

    contract_id               = None
    history_req_id            = 1
    buy_req_id                = 1000
    last_update_epoch         = None
    candles_data              = []
    awaiting_confirmation     = None
    confirmation_candle_epoch = None
    band_break_status         = 'none' # Reinicia status da banda

    url = f"wss://ws.derivws.com/websockets/v3?app_id={APP_ID}"
    log("Iniciando Deriv Trading Bot (v17.2 – Rompimento + Confirmação na Vela Seguinte + Memória de Banda + Reconexão Automática)…")
    log(f"Configurações: Stake={STAKE:.2f}, Período Fractal={FRACTAL_PERIOD}")
    
    while True: # Loop infinito para tentar manter o bot rodando
        try:
            ws_app = websocket.WebSocketApp(
                url,
                on_open=on_open,
                on_message=on_message,
                on_error=on_error,
                on_close=on_close
            )
            log("Conectando ao WebSocket…")
            # Inicia o loop de eventos do WebSocket. Ele só retorna se a conexão cair ou for fechada.
            ws_app.run_forever(ping_interval=20, ping_timeout=10)
        except Exception as e:
            log(f"[ERRO FATAL NO BOT] {e}. Tentando reiniciar em 10s…")
            time.sleep(10) # Pausa maior em caso de erro não tratado

if __name__ == "__main__":
    start_bot()
