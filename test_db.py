print("--- Starting test_db.py ---")
import sys
print("Imports starting...")
try:
    from supabase import create_client
    import toml
    print("Imports success.")
except ImportError as e:
    print(f"Import failed: {e}")
    sys.exit(1)

print("Loading secrets...")
try:
    secrets = toml.load(".streamlit/secrets.toml")
    # Handle potentially nested or flat secrets
    if "supabase" in secrets:
        url = secrets["supabase"]["url"]
        key = secrets["supabase"]["key"]
    else:
        url = secrets["SUPABASE_URL"]
        key = secrets["SUPABASE_KEY"]
    
    print(f"URL found: {url[:10]}...")
except Exception as e:
    print(f"Error loading secrets: {e}")
    sys.exit(1)

print("Creating client...")
try:
    supabase = create_client(url, key)
    print("Client created.")
except Exception as e:
    print(f"Client creation failed: {e}")
    sys.exit(1)

print("Executing query...")
try:
    # Set a timeout if possible? implementation specific.
    # We will just run the query.
    print("Sending request to Supabase...")
    response = supabase.table("vehicle").select("*").limit(5).execute()
    print("Request returned!")
    print(f"Count: {len(response.data)}")
    print("Data sample:", response.data)
except Exception as e:
    print(f"Query failed: {e}")
