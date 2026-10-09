import os
import json
import requests
from discord.ext import commands, tasks

class SalesTracker(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.group_id = "1003827405"  # Replace with your Roblox Group ID
        self.data_file = "session.json"
        self.processed_tx_ids = set()
        
        # Initialize an HTTP session for persistence
        self.session = requests.Session()
        self.setup_session()
        
        # Start the background polling task loop
        self.track_sales.start()

    def cog_unload(self):
        """Stops the task loop cleanly if the cog is reloaded or unloaded."""
        self.track_sales.cancel()

    def setup_session(self):
        """Loads cookie from storage and injects it into the HTTP adapter."""
        if not os.path.exists(self.data_file):
            print(f"[SalesTracker Error] Missing {self.data_file} in the project root.")
            return
            
        with open(self.data_file, "r") as f:
            data = json.load(f)
            cookie = data.get("ROBLOSECURITY")
            
        if cookie:
            self.session.cookies.set(".roblox.com", cookie, domain=".roblox.com")

    def save_and_update_cookie(self, response_headers):
        """Intercepts newer .ROBLOSECURITY configurations returned by Roblox endpoints."""
        for header, value in response_headers.items():
            if header.lower() == "set-cookie" and ".ROBLOSECURITY=" in value:
                parts = value.split(".ROBLOSECURITY=")
                if len(parts) > 1:
                    new_cookie_raw = parts[1].split(";")[0]
                    
                    if not new_cookie_raw.startswith("_|WARNING:"):
                        new_cookie_raw = f"_|WARNING:-DO-NOT-SHARE-THIS.--Keep-this-cookie-secret-or-your-account-will-be-stolen-v2_{new_cookie_raw}"
                    
                    # Update local state file
                    with open(self.data_file, "w") as f:
                        json.dump({"ROBLOSECURITY": new_cookie_raw}, f, indent=4)
                        
                    # Inject updated version back into the network pipeline
                    self.session.cookies.set(".roblox.com", new_cookie_raw, domain=".roblox.com")
                    print("[SalesTracker] Updated .ROBLOSECURITY token saved to session.json")
                    break

    @tasks.loop(seconds=60.0)
    async def track_sales(self):
        """Background loop executing requests cleanly over asynchronous routines."""
        await self.bot.wait_until_ready()
        
        url = f"https://roblox.com{self.group_id}/transactions"
        params = {"limit": 10, "transactionType": "Sale"}
        
        try:
            # Sync HTTP request made safe inside the async task loop
            response = self.session.get(url, params=params, timeout=10)
            self.save_and_update_cookie(response.headers)
            
            if response.status_code == 200:
                data = response.json()
                sales = data.get("data", [])
                
                for sale in sales:
                    tx_id = sale.get("id")
                    
                    if tx_id not in self.processed_tx_ids:
                        is_initial_load = len(self.processed_tx_ids) == 0
                        self.processed_tx_ids.add(tx_id)
                        
                        if is_initial_load:
                            continue
                            
                        # Extract precise buyer and data records
                        buyer_name = sale.get("agent", {}).get("name", "Unknown")
                        amount = sale.get("currency", {}).get("amount", 0)
                        item_name = sale.get("details", {}).get("name", "Unknown Item")
                        
                        # Dispatch tracking details to your terminal
                        print(f"[SALE LOG] {buyer_name} bought {item_name} for R$ {amount}")
                        
                        # --- DISCORD CHANNEL NOTIFICATION ---
                        # target_channel = self.bot.get_channel(YOUR_CHANNEL_ID_HERE)
                        # if target_channel:
                        #     await target_channel.send(f"🛍️ **{buyer_name}** bought **{item_name}** for **R$ {amount}**!")
                        
            elif response.status_code == 401:
                print("[SalesTracker Error] 401 Unauthorized - Cookie invalid or IP-locked.")
                
        except Exception as e:
            print(f"[SalesTracker Exception] Loop error: {e}")

async def setup(bot):
    await bot.add_cog(SalesTracker(bot))
