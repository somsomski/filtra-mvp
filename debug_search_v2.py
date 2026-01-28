import os
import asyncio
from supabase import create_client
import time

# Try to get from env
SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")

print(f"Env Check - URL: {'Found' if SUPABASE_URL else 'Missing'}", flush=True)
print(f"Env Check - Key: {'Found' if SUPABASE_KEY else 'Missing'}", flush=True)

async def test_search():
    if not SUPABASE_URL or not SUPABASE_KEY:
        print("CRITICAL: SUPABASE_URL or SUPABASE_KEY not in environment variables.", flush=True)
        return

    print("Connecting to Supabase...", flush=True)
    try:
        supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
        print("Client created.", flush=True)
    except Exception as e:
        print(f"Client Init Error: {e}", flush=True)
        return

    print("Checking connection (simple select)...", flush=True)
    try:
        # Just select 1
        res = supabase.table("vehicle").select("vehicle_id").limit(1).execute()
        print(f"Connection OK. Data sample: {len(res.data)}", flush=True)
    except Exception as e:
        print(f"Connection Failed: {e}", flush=True)
        return

    print("--- Testing 'gol' (Strict) ---", flush=True)
    
    variations = [
        r"\ygol\y",       # Original attempt
        r"[[:<:]]gol[[:>:]]", # POSIX
        r"\mgol\M",
        r"\bgol\b",        # Regular regex boundary
        r"(^| )gol( |$)"   # Simple manual space check (limited)
    ]
    
    for pattern in variations:
        print(f"\nTesting Pattern: {pattern}", flush=True)
        try:
            res = supabase.table("vehicle").select("model").imatch("model", pattern).limit(5).execute()
            print(f"Result Count: {len(res.data)}", flush=True)
            if res.data:
                print(f"Sample: {res.data[0]}", flush=True)
        except Exception as e:
            print(f"Error: {e}", flush=True)

    print("\n\n--- Testing 'I' (Strict) for Kangoo I ---", flush=True)
    # Kangoo I
    for pattern in variations:
        p = pattern.replace('gol', 'I')
        print(f"\nTesting Pattern: {p}", flush=True)
        try:
            # We search for "Kangoo I" effectively by checking if 'I' matches
            # Ideally we'd search where model ILIKE %Kangoo% AND strict 'I'
            # But let's just see if strict 'I' matches anything (it should match Kangoo I, Logan I, etc)
            res = supabase.table("vehicle").select("model").imatch("model", p).limit(5).execute()
            print(f"Result Count: {len(res.data)}", flush=True)
            if res.data:
                print(f"Sample: {res.data[0]}", flush=True)
        except Exception as e:
            print(f"Error: {e}", flush=True)

if __name__ == "__main__":
    asyncio.run(test_search())
