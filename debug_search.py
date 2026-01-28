import os
import asyncio
from supabase import create_client

# Try to get from env
SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")

async def test_search():
    if not SUPABASE_URL or not SUPABASE_KEY:
        print("CRITICAL: SUPABASE_URL or SUPABASE_KEY not in environment variables.")
        # Try to read from potential .env file if it exists (simple parser)
        try:
            with open(".env", "r") as f:
                for line in f:
                    if line.startswith("SUPABASE_URL="):
                        globals()["SUPABASE_URL"] = line.strip().split("=", 1)[1]
                    elif line.startswith("SUPABASE_KEY="):
                        globals()["SUPABASE_KEY"] = line.strip().split("=", 1)[1]
        except:
            pass

    if not SUPABASE_URL or not SUPABASE_KEY:
        print("STILL MISSING CREDENTIALS. Aborting.")
        return

    print("Connecting to Supabase...")
    supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

    print("--- Testing 'gol' (Strict) ---")
    
    variations = [
        r"\ygol\y",       # Original attempt
        r"[[:<:]]gol[[:>:]]", # POSIX
        r"(^|\s)gol(\s|$)", # Manual
        r"\mgol\M",
        r"\bgol\b" # Python/PCRE standard
    ]
    
    for pattern in variations:
        print(f"\nTesting Pattern: {pattern}")
        try:
            # We use match() instead of imatch() for some patterns if imatch fails?
            # But we want case insensitive.
            # strict regex for "gol" against "Volkswagen Gol Trend"
            
            # Note: PostgREST `imatch` uses POSIX regular expressions (tilde `~*`).
            # Postgres POSIX regex support `\y` for word boundaries.
            
            res = supabase.table("vehicle").select("model").imatch("model", pattern).limit(5).execute()
            print(f"Result Count: {len(res.data)}")
            if res.data:
                print(f"Sample: {res.data[0]}")
        except Exception as e:
            print(f"Error: {e}")

    print("\n\n--- Testing 'I' (Strict) ---")
    # Kangoo I
    for pattern in variations:
        p = pattern.replace('gol', 'I')
        print(f"\nTesting Pattern: {p}")
        try:
            res = supabase.table("vehicle").select("model").imatch("model", p).limit(5).execute()
            print(f"Result Count: {len(res.data)}")
            if res.data:
                print(f"Sample: {res.data[0]}")
        except Exception as e:
            print(f"Error: {e}")

if __name__ == "__main__":
    asyncio.run(test_search())
