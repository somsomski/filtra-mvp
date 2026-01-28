import json

def format_version_app_logic(x):
    # Mocking the function from app.py
    try:
        y_from = str(int(float(x['year_from']))) if x['year_from'] else '?'
        y_to = str(int(float(x['year_to']))) if x['year_to'] else 'Presente'
    except:
        y_from = str(x['year_from'])
        y_to = str(x['year_to']) or 'Presente'

    # Metadata Extraction
    meta = x.get('metadata') or {}
    if isinstance(meta, str):
        try:
            meta = json.loads(meta)
        except:
            meta = {}
    
    # Prefer metadata, fallback to columns
    eng_code = meta.get('engine_code') or x.get('engine_code')
    eng_series = meta.get('engine_series')
    
    # Construir partes opcionales
    suffix = f" {x['series_suffix']}" if x['series_suffix'] else ""
    disp = f" {x['engine_disp_l']}L" if x['engine_disp_l'] else ""
    
    # Tech Badge
    tech_parts = []
    if eng_code: tech_parts.append(eng_code)
    if eng_series: tech_parts.append(eng_series)
    
    tech_str = f" [{' | '.join(tech_parts)}]" if tech_parts else ""
    
    try:
        hp_val = int(float(x['power_hp']))
        power = f" ({hp_val}HP)"
    except:
        power = f" ({x['power_hp']}HP)" if x['power_hp'] else ""
    
    return f"{x['model']}{suffix} ({y_from}-{y_to}){disp}{power}{tech_str}"

def bot_logic_extraction(vehicle):
    # Mocking extraction from bot.py
    msg_body = ""
    
    # Metadata Extraction
    meta = vehicle.get('metadata') or {}
    if isinstance(meta, str):
        try:
            meta = json.loads(meta)
        except:
            meta = {}
            
    eng_code = meta.get('engine_code') or vehicle.get('engine_code')
    eng_series = meta.get('engine_series')
    
    if eng_code or eng_series:
        tech_info = []
        if eng_series: tech_info.append(f"Serie: {eng_series}")
        if eng_code: tech_info.append(f"Motor: {eng_code}")
        
        msg_body += f"\n🔧 {' | '.join(tech_info)}"
        
    return msg_body

# Test Cases
test_cases = [
    # Case 1: Standard Metadata
    {
        "model": "Gol",
        "year_from": 2010,
        "year_to": 2015,
        "series_suffix": "Trend",
        "engine_disp_l": "1.6",
        "power_hp": 101,
        "engine_code": "LegacyCode", # Should be overridden if metadata exists? No, my logic says 'meta.get OR x.get'. If meta doesn't have it, use legacy.
                                     # Wait, precedence: `meta.get('engine_code') or x.get('engine_code')`. 
                                     # If meta has it, use it. If not, use legacy.
        "metadata": {"engine_code": "CFZ", "engine_series": "EA111"}
    },
    # Case 2: Metadata as String
    {
        "model": "Hilux",
        "year_from": 2016,
        "year_to": None,
        "series_suffix": "DX",
        "engine_disp_l": "2.4",
        "power_hp": 150,
        "engine_code": None,
        "metadata": '{"engine_code": "2GD-FTV", "engine_series": "GD"}'
    },
    # Case 3: No Metadata, Legacy Columns
    {
        "model": "Corsa",
        "year_from": 2000,
        "year_to": 2010,
        "series_suffix": None,
        "engine_disp_l": "1.6",
        "power_hp": 92,
        "engine_code": "C16NE",
        "metadata": None
    },
    # Case 4: Broken JSON
    {
        "model": "Clio",
        "year_from": 2005,
        "year_to": 2012,
        "series_suffix": None,
        "engine_disp_l": "1.2",
        "power_hp": 75,
        "engine_code": "D4F",
        "metadata": "{bad_json: 123"
    }
]

print("--- Testing App Logic ---")
for t in test_cases:
    print(f"Result: {format_version_app_logic(t)}")

print("\n--- Testing Bot Logic ---")
for t in test_cases:
    print(f"Result: {bot_logic_extraction(t).strip()}")
