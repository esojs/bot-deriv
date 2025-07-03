import asyncio
import json
import websockets
import pandas as pd
import pandas_ta as ta
import time

# --- Configurações do Bot ---
API_TOKEN = 'uFedqbTqaKZgzBR'  # Seu token de API da Deriv (coloque um token real para operar)
APP_ID = 1089 # Use 1089 para produção/live, 30589 para demo
SYMBOL = 'R_100'  # Símbolo do ativo (e.g., 'R_100' para Volatility 100 Index)
TIMEFRAME = 60  # Timeframe em segundos (e.g., 60 para 1 minuto)
TRADE_TYPE = 'CALL'  # Tipo de operação padrão (pode ser 'CALL' ou 'PUT')

# Parâmetros de gerenciamento de banca
INITIAL_STAKE = 1.00
MARTINGALE_FACTOR = 2.0
MARTINGALE_LEVELS = 1 # Quantas vezes tentar Martingale após uma perda

# Parâmetros dos indicadores
EMA_FAST_PERIOD = 5
EMA_SLOW_PERIOD = 10
MACD_FAST_PERIOD = 12
MACD_SLOW_PERIOD = 26
MACD_SIGNAL_PERIOD = 9

# --- Variáveis de Estado ---
candles = pd.DataFrame(columns=['time', 'open', 'high', 'low', 'close'])
last_candle_time = 0
balance = 0
current_stake = INITIAL_STAKE
consecutive_losses = 0

# --- Funções Auxiliares ---
def calculate_indicators(df):
    # Assegura que o DataFrame tem dados suficientes para calcular os indicadores
    min_periods_required = max(EMA_SLOW_PERIOD, MACD_SLOW_PERIOD + MACD_SIGNAL_PERIOD)
    if len(df) < min_periods_required:
        return (None,) * 9 

    # Calcula as EMAs
    df['EMA_FAST'] = ta.ema(df['close'], length=EMA_FAST_PERIOD)
    df['EMA_SLOW'] = ta.ema(df['close'], length=EMA_SLOW_PERIOD)

    # Calcula o MACD
    macd_data = ta.macd(df['close'], fast=MACD_FAST_PERIOD, slow=MACD_SLOW_PERIOD, signal=MACD_SIGNAL_PERIOD)
    if macd_data is not None and not macd_data.empty:
        macd_col = f'MACD_{MACD_FAST_PERIOD}_{MACD_SLOW_PERIOD}_{MACD_SIGNAL_PERIOD}'
        macd_signal_col = f'MACDS_{MACD_FAST_PERIOD}_{MACD_SLOW_PERIOD}_{MACD_SIGNAL_PERIOD}' 
        macd_hist_col = f'MACDH_{MACD_FAST_PERIOD}_{MACD_SLOW_PERIOD}_{MACD_SIGNAL_PERIOD}'
        
        if macd_col in macd_data.columns:
            df['MACD'] = macd_data[macd_col]
        else:
            df['MACD'] = pd.NA
        
        if macd_signal_col in macd_data.columns:
            df['MACD_SIGNAL'] = macd_data[macd_signal_col] 
        else:
            df['MACD_SIGNAL'] = pd.NA
            
        if macd_hist_col in macd_data.columns:
            df['MACD_HIST'] = macd_data[macd_hist_col]
        else:
            df['MACD_HIST'] = pd.NA
    else:
        df['MACD'] = pd.NA
        df['MACD_SIGNAL'] = pd.NA
        df['MACD_HIST'] = pd.NA

    def get_safe_value(series, index):
        if len(series) > index and pd.notna(series.iloc[index]):
            return series.iloc[index]
        return None

    last_ema_fast = get_safe_value(df['EMA_FAST'], -1)
    last_ema_slow = get_safe_value(df['EMA_SLOW'], -1)
    last_macd = get_safe_value(df['MACD'], -1)
    last_macd_signal = get_safe_value(df['MACD_SIGNAL'], -1)
    last_macd_hist = get_safe_value(df['MACD_HIST'], -1)

    prev_ema_fast = get_safe_value(df['EMA_FAST'], -2)
    prev_ema_slow = get_safe_value(df['EMA_SLOW'], -2)
    prev_macd = get_safe_value(df['MACD'], -2)
    prev_macd_signal = get_safe_value(df['MACD_SIGNAL'], -2)
    
    return last_ema_fast, last_ema_slow, last_macd, last_macd_signal, last_macd_hist, \
           prev_ema_fast, prev_ema_slow, prev_macd, prev_macd_signal

def determine_trade_direction(current_data, prev_data, current_candle_time_str):
    if current_data is None or prev_data is None:
        print(f"DEBUG: Dados de entrada para determine_trade_direction incompletos.")
        return None

    current_ema_fast, current_ema_slow, current_macd, current_macd_signal, current_macd_hist = current_data
    prev_ema_fast, prev_ema_slow, prev_macd, prev_macd_signal = prev_data

    if any(val is None for val in [current_ema_fast, current_ema_slow, current_macd, current_macd_signal, current_macd_hist,
                                   prev_ema_fast, prev_ema_slow, prev_macd, prev_macd_signal]):
        print(f"DEBUG: Valores de indicadores calculados são None para a vela {current_candle_time_str}. Pulando análise de sinal.")
        return None

    print(f"DEBUG: Vela {current_candle_time_str} -> Prev: EMA5={prev_ema_fast:.4f}, EMA10={prev_ema_slow:.4f} | MACD={prev_macd:.4f}, MACDS={prev_macd_signal:.4f}")
    print(f"DEBUG: Vela {current_candle_time_str} -> Current: EMA5={current_ema_fast:.4f}, EMA10={current_ema_slow:.4f} | MACD={current_macd:.4f}, MACDS={current_macd_signal:.4f}, MACDH={current_macd_hist:.4f}")

    ema_crossover_up = (prev_ema_fast < prev_ema_slow and current_ema_fast > current_ema_slow)
    ema_crossover_down = (prev_ema_fast > prev_ema_slow and current_ema_fast < current_ema_slow)

    macd_crossover_up = (prev_macd < prev_macd_signal and current_macd > current_macd_signal)
    macd_crossover_down = (prev_macd > prev_macd_signal and current_macd < current_macd_signal)

    if ema_crossover_up:
        print(f"[LOG Crossover] EMA5 ({current_ema_fast:.4f}) cruzou EMA10 ({current_ema_slow:.4f}) para CIMA na vela {current_candle_time_str}.")
    elif ema_crossover_down:
        print(f"[LOG Crossover] EMA5 ({current_ema_fast:.4f}) cruzou EMA10 ({current_ema_slow:.4f}) para BAIXO na vela {current_candle_time_str}.")

    if macd_crossover_up:
        print(f"[LOG Crossover] MACD ({current_macd:.4f}) cruzou Signal ({current_macd_signal:.4f}) para CIMA na vela {current_candle_time_str}.")
    elif macd_crossover_down:
        print(f"[LOG Crossover] MACD ({current_macd:.4f}) cruzou Signal ({current_macd_signal:.4f}) para BAIXO na vela {current_candle_time_str}.")

    call_condition = ema_crossover_up and macd_crossover_up and (current_macd_hist > 0)
    put_condition = ema_crossover_down and macd_crossover_down and (current_macd_hist < 0)

    if call_condition:
        return 'CALL'
    elif put_condition:
        return 'PUT'
    else:
        return None

# --- Conexão WebSocket e Lógica Principal ---
async def connect():
    global balance, candles, last_candle_time, current_stake, consecutive_losses

    uri = f"wss://ws.derivws.com/websockets/v3?app_id={APP_ID}"
    
    print(f"[INFO] Iniciando Deriv Trading Bot (EMA/MACD Crossover Strategy, v1.0).")
    print(f"[INFO] Configurações: Stake Inicial={INITIAL_STAKE:.2f}, Fator Martingale={MARTINGALE_FACTOR}, Níveis Martingale={MARTINGALE_LEVELS}")
    print(f"[INFO] Indicadores: EMA({EMA_FAST_PERIOD},{EMA_SLOW_PERIOD}), MACD({MACD_FAST_PERIOD},{MACD_SLOW_PERIOD},{MACD_SIGNAL_PERIOD})")

    async with websockets.connect(uri) as websocket:
        print("[INFO] Conexão aberta. Autenticando…")
        await websocket.send(json.dumps({"authorize": API_TOKEN}))

        async for message in websocket:
            data = json.loads(message)
            
            # --- LINHA DE DEBUG TEMPORÁRIA ---
            # Esta linha irá imprimir todas as mensagens recebidas para depuração.
            # Remova-a após resolvermos o problema.
            print(json.dumps(data, indent=2))
            # --- FIM DA LINHA DE DEBUG ---

            if data.get('msg_type') == 'authorize':
                if data.get('authorize', {}).get('loginid'): 
                    print(f"[INFO] Autenticação bem-sucedida. Conta: {data['authorize']['loginid']}")
                    balance = data['authorize']['balance']
                    print(f"[INFO] Saldo inicial: {balance:.2f} {data['authorize']['currency']}")
                    
                    await websocket.send(json.dumps({
                        "ticks_history": SYMBOL,
                        "adjust_start_time": 1, 
                        "count": 250, 
                        "end": "latest",
                        "start": 1, 
                        "style": "candles", 
                        "granularity": TIMEFRAME, 
                        "subscribe": 1 
                    }))
                    print(f"[INFO] Solicitando histórico de velas e assinando novas para {SYMBOL} no timeframe de {TIMEFRAME} segundos...")
                else:
                    print(f"[ERRO] Falha na autenticação. Resposta da API: {data}")
                    return 
            
            elif data.get('msg_type') == 'candles':
                # Este bloco processa apenas a resposta inicial do histórico de velas
                if 'candles' in data: 
                    new_candles_data = []
                    for c in data['candles']:
                        new_candles_data.append({
                            'time': c['epoch'],
                            'open': c['open'],
                            'high': c['high'],
                            'low': c['low'],
                            'close': c['close']
                        })
                    candles = pd.concat([candles, pd.DataFrame(new_candles_data)], ignore_index=True)
                    candles['time'] = pd.to_numeric(candles['time'])
                    candles['open'] = pd.to_numeric(candles['open'])
                    candles['high'] = pd.to_numeric(candles['high'])
                    candles['low'] = pd.to_numeric(candles['low'])
                    candles['close'] = pd.to_numeric(candles['close'])
                    
                    candles.drop_duplicates(subset=['time'], inplace=True)
                    candles.sort_values(by='time', inplace=True)
                    
                    print(f"[INFO] Histórico de {len(candles)} velas carregado.")
                    
                    if 'subscription' in data:
                        print(f"[INFO] Inscrição para novas velas real-time bem-sucedida. ID: {data['subscription']['id']}")
                        
                    print("[INFO] Histórico inicial carregado. Aguardando a primeira vela FECHADA para iniciar a análise de sinais.")

            elif data.get('msg_type') == 'ohlc':
                # Este bloco processa todas as atualizações de velas em tempo real (abertas e fechadas)
                ohlc = data['ohlc']
                
                # Apenas processa a vela se ela estiver fechada
                if ohlc.get('is_candle_closed') == 1:
                    current_candle_time_str = time.strftime('%H:%M:%S GMT', time.gmtime(ohlc['open_time']))
                    
                    new_candle_df = pd.DataFrame([{
                        'time': ohlc['open_time'],
                        'open': ohlc['open'],
                        'high': ohlc['high'],
                        'low': ohlc['low'],
                        'close': ohlc['close']
                    }])
                    
                    candles = pd.concat([candles, new_candle_df], ignore_index=True)
                    candles['time'] = pd.to_numeric(candles['time'])
                    candles['open'] = pd.to_numeric(candles['open'])
                    candles['high'] = pd.to_numeric(candles['high'])
                    candles['low'] = pd.to_numeric(candles['low'])
                    candles['close'] = pd.to_numeric(candles['close'])
                    
                    candles.drop_duplicates(subset=['time'], inplace=True)
                    candles.sort_values(by='time', inplace=True)
                    
                    candles = candles.tail(250)
                    
                    print(f"\n--- VELA FECHADA (via OHLC) --- Epoch: {ohlc['open_time']}, O:{ohlc['open']:.5f}, H:{ohlc['high']:.5f}, L:{ohlc['low']:.5f}, C:{ohlc['close']:.5f}")

                    min_candles_required = max(EMA_SLOW_PERIOD, MACD_SLOW_PERIOD + MACD_SIGNAL_PERIOD) + 1 
                    if len(candles) >= min_candles_required:
                        
                        last_ema_fast, last_ema_slow, last_macd, last_macd_signal, last_macd_hist, \
                        prev_ema_fast, prev_ema_slow, prev_macd, prev_macd_signal = calculate_indicators(candles.copy())

                        print(f"\n--- Análise de Velas para Entrada --- (Total de velas no buffer: {len(candles)})") 
                        num_candles_to_display = min(len(candles), 3) 
                        for i in range(num_candles_to_display):
                            c_idx = len(candles) - num_candles_to_display + i
                            c_data = candles.iloc[c_idx]
                            
                            temp_df = candles.iloc[:c_idx+1].copy()
                            ema_f, ema_s, macd_v, macd_sig, macd_h, _, _, _, _ = calculate_indicators(temp_df)
                            
                            ema_f_str = f"{ema_f:.4f}" if ema_f is not None else "N/A"
                            ema_s_str = f"{ema_s:.4f}" if ema_s is not None else "N/A"
                            macd_v_str = f"{macd_v:.4f}" if macd_v is not None else "N/A"
                            macd_sig_str = f"{macd_sig:.4f}" if macd_sig is not None else "N/A"
                            macd_h_str = f"{macd_h:.4f}" if macd_h is not None else "N/A"

                            print(f"Vela ({num_candles_to_display-i}) | {time.strftime('%H:%M:%S GMT', time.gmtime(c_data['time']))} | O:{c_data['open']:.2f}, C:{c_data['close']:.2f} | EMA5:{ema_f_str}, EMA10:{ema_s_str} | MACD:{macd_v_str}, MACDS:{macd_sig_str}, MACDH:{macd_h_str}")


                        trade_direction = determine_trade_direction(
                            (last_ema_fast, last_ema_slow, last_macd, last_macd_signal, last_macd_hist),
                            (prev_ema_fast, prev_ema_slow, prev_macd, prev_macd_signal),
                            current_candle_time_str 
                        )

                        if trade_direction:
                            print(f"[SINAL] Sinal de {trade_direction} detectado! Preço de entrada: {ohlc['close']:.5f}")
                            await websocket.send(json.dumps({
                                "buy": 1,                     
                                "price": current_stake,       
                                "parameters": {               
                                    "amount": current_stake,  
                                    "basis": "stake",         
                                    "contract_type": trade_direction, 
                                    "currency": "USD",        
                                    "duration": 1,            
                                    "duration_unit": "m",     
                                    "symbol": SYMBOL,         
                                    "barrier": "auto",        
                                    "barrier2": "auto"        
                                }                             
                            }))                               
                            print(f"[TRADE] Ordem de {trade_direction} enviada com stake de {current_stake:.2f}.") 
                            consecutive_losses = 0            
                            current_stake = INITIAL_STAKE     
                        else:
                            print("[SINAL] Nenhum sinal de entrada claro. Aguardando a próxima vela.")
                    else:
                        print(f"[INFO] Aguardando mais velas para calcular indicadores. Velas atuais: {len(candles)}/{min_candles_required}")
                else:
                    # Esta é uma vela aberta ou uma atualização da vela atual que ainda não fechou
                    new_candle_open_time = ohlc['open_time']
                    if new_candle_open_time > last_candle_time:
                        last_candle_time = new_candle_open_time
                        # Opcional: print para ver velas abertas
                        # print(f"[INFO] Nova vela ABERTA: Epoch {new_candle_open_time} ({time.strftime('%H:%M:%S GMT', time.gmtime(new_candle_open_time))}) O:{ohlc['open']:.2f}")

            elif data.get('msg_type') == 'buy':
                if data['buy']['status'] == 'Evidência de contrato comprado.':
                    print(f"[SUCESSO] Contrato comprado! ID do contrato: {data['buy']['contract_id']}")
                    print(f"[SUCESSO] Preço de entrada: {data['buy']['buy_price']:.2f}")
                else:
                    print(f"[ERRO] Falha ao comprar contrato: {data['buy']['longcode']}")
                    
            elif data.get('msg_type') == 'proposal_open_contract':
                contract = data['proposal_open_contract']
                if contract['is_sold']:
                    if contract['profit'] > 0:
                        print(f"[RESULTADO] VITÓRIA! Lucro: {contract['profit']:.2f}")
                        consecutive_losses = 0
                        current_stake = INITIAL_STAKE
                    else:
                        print(f"[RESULTADO] DERROTA! Perda: {contract['profit']:.2f}")
                        consecutive_losses += 1
                        if consecutive_losses <= MARTINGALE_LEVELS:
                            current_stake *= MARTINGALE_FACTOR
                            print(f"[MARTINGALE] Próximo stake: {current_stake:.2f} (nível {consecutive_losses})")
                        else:
                            print(f"[MARTINGALE] Limite de Martingale atingido. Resetando stake.")
                            consecutive_losses = 0
                            current_stake = INITIAL_STAKE
                    
                    balance = contract['balance_after']
                    print(f"[INFO] Saldo atual: {balance:.2f}")
                
            elif data.get('error'):
                print(f"[ERRO API] Código: {data['error']['code']}, Mensagem: {data['error']['message']}")

async def main():
    await connect()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("[INFO] Bot encerrado pelo usuário.")
    except Exception as e:
        print(f"[ERRO CRÍTICO] {e}")