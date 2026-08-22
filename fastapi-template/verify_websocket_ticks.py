import asyncio
import json
import websockets
from datetime import datetime

async def test_live_stream():
    uri = "ws://127.0.0.1:8000/ws/market/stream"
    print(f"Connecting to {uri}...")
    ticks_by_symbol = {}
    
    try:
        async with websockets.connect(uri) as ws:
            print("Connected to WebSocket market stream!")
            start_time = datetime.now()
            
            # Read ticks for 15 seconds
            while (datetime.now() - start_time).total_seconds() < 15:
                msg = await asyncio.wait_for(ws.recv(), timeout=5.0)
                try:
                    data = json.loads(msg)
                    sym = data.get("symbol")
                    price = data.get("price")
                    if sym and price is not None:
                        if sym not in ticks_by_symbol:
                            ticks_by_symbol[sym] = []
                        ticks_by_symbol[sym].append(price)
                        print(f"[{datetime.now().strftime('%H:%M:%S')}] Tick received: {sym} -> Price: {price}, Change: {data.get('change_pct')}%")
                except json.JSONDecodeError:
                    pass

            print("\n" + "="*50)
            print("LIVE STREAM TICK VERIFICATION RESULTS:")
            print("="*50)
            for sym, prices in ticks_by_symbol.items():
                price_changed = len(set(prices)) > 1 if len(prices) > 1 else False
                first_p = prices[0]
                last_p = prices[-1]
                print(f"Symbol: {sym:<12} | Ticks: {len(prices):<3} | First: {first_p:<10} | Last: {last_p:<10} | Changed: {price_changed}")
            
            all_changed = any(len(set(p)) > 1 for p in ticks_by_symbol.values() if len(p) > 1)
            print(f"\nOverall Price Ticking Verified: {all_changed}")
            return all_changed
    except Exception as e:
        print(f"WebSocket verification error: {e}")
        return False

if __name__ == "__main__":
    asyncio.run(test_live_stream())
