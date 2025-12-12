import streamlit as st
import pandas as pd
from supabase import create_client, Client
import time

# --- Configuración de la página ---
st.set_page_config(page_title="Filtra - Buscador de Filtros", page_icon="🚗", layout="wide")

# --- CSS Personalizado ---
st.markdown("""
    <style>
    .main {
        padding-top: 2rem;
    }
    .stSelectbox label {
        font-weight: bold;
        color: #333;
    }
    </style>
    """, unsafe_allow_html=True)

# --- Inicialización de Supabase ---
# Usamos st.cache_resource para crear el cliente solo una vez
@st.cache_resource
def init_supabase() -> Client:
    try:
        url = st.secrets["SUPABASE_URL"]
        key = st.secrets["SUPABASE_KEY"]
        return create_client(url, key)
    except Exception as e:
        st.error(f"Error al conectar con la base de datos: {e}")
        st.stop()

supabase = init_supabase()

# --- Carga de Datos (Optimización: Fetch All & Cache) ---
@st.cache_data(ttl=24*3600)  # Cache por 24 horas
def get_all_vehicles():
    """Descarga la tabla 'vehicle' completa y la prepara para filtrar."""
    try:
        # Supabase API tiene un límite de filas por defecto, aseguramos traer todas si es posible.
        # Para tablas muy grandes se necesitaría paginación, pero para MVP 'todo de una' es mejor UX.
        # Asumiendo < 5000 vehículos por ahora. Si es más, se debe ajustar el rango.
        response = supabase.table("vehicle").select("*").execute()
        
        df = pd.DataFrame(response.data)
        
        if df.empty:
            return df
        
        df = df.fillna('')
        
        def format_version(x):
            try:
                # Formatear años eliminando decimales si existen
                y_from = str(int(float(x['year_from']))) if x['year_from'] else '?'
                y_to = str(int(float(x['year_to']))) if x['year_to'] else 'Presente'
            except:
                y_from = str(x['year_from'])
                y_to = str(x['year_to']) or 'Presente'

            # Construir partes opcionales
            suffix = f" {x['series_suffix']}" if x['series_suffix'] else ""
            disp = f" {x['engine_disp_l']}L" if x['engine_disp_l'] else ""
            code = f" {x['engine_code']}" if x['engine_code'] else ""
            try:
                hp_val = int(float(x['power_hp']))
                power = f" ({hp_val}HP)"
            except:
                power = f" ({x['power_hp']}HP)" if x['power_hp'] else ""
            
            return f"{x['model']}{suffix} ({y_from}-{y_to}){disp}{code}{power}"

        df['version_str'] = df.apply(format_version, axis=1)
        return df
    except Exception as e:
        st.error(f"Error al cargar vehículos: {e}")
        return pd.DataFrame()

@st.cache_data(ttl=600) # Cache corto para partes
def get_parts_for_vehicle(vehicle_id):
    """Obtiene las partes para un vehículo específico."""
    try:
        # join vehicle_part -> part
        # Added source_catalog
        response = supabase.table("vehicle_part")\
            .select("role, notes, source_catalog, part(brand_filter, part_code, part_type, media_type, notes)")\
            .eq("vehicle_id", vehicle_id)\
            .execute()
        
        data = []
        
        # Mapping for translation
        type_map = {
            'oil': '🛢️ Aceite',
            'air': '💨 Aire',
            'fuel': '⛽ Combustible',
            'cabin': '❄️ Habitáculo'
        }
        
        for item in response.data:
            part_info = item['part']
            if part_info: # Si hay datos de la parte
                p_type_raw = part_info.get('part_type', '').lower()
                
                entry = {
                    "raw_type": p_type_raw, # For sorting/grouping
                    "Tipo": type_map.get(p_type_raw, p_type_raw.capitalize()),
                    "Marca": part_info.get('brand_filter'),
                    "Código": part_info.get('part_code'),
                    "Catálogo": item.get('source_catalog'),
                    # Hidden but kept if needed for debug
                    "Notes": item.get('notes')
                }
                data.append(entry)
        
        return pd.DataFrame(data)
    except Exception as e:
        st.error(f"Error al buscar partes: {e}")
        return pd.DataFrame()

# --- Interfaz de Usuario ---
st.title("🚗 Filtra")
st.caption("Buscador de filtros para el mercado automotor argentino.")

# Checkbox de Debug (oculto por defecto para usuarios normales, visible si se quiere)
debug_mode = st.sidebar.checkbox("Modo Debug 🛠️", value=False)

# Cargar datos
with st.spinner("Cargando catálogo de vehículos..."):
    df_vehicles = get_all_vehicles()
    
if debug_mode:
    st.sidebar.divider()
    st.sidebar.warning("🚧 Debug Info")
    st.sidebar.write(f"Vehículos cargados: {len(df_vehicles)}")
    if df_vehicles.empty:
        st.sidebar.error("La tabla 'vehicle' devolvió 0 filas.")
        st.sidebar.info("Posibles causas:\n1. La tabla está vacía.\n2. RLS (Row Level Security) está activo y no permite lecturas anónimas.\n3. Error de conexión silencioso.")

if df_vehicles.empty:
    st.warning("No se encontraron vehículos en la base de datos.")
    if not debug_mode:
        st.info("Active el 'Modo Debug 🛠️' en la barra lateral para ver más detalles.")
else:
    # --- Cascada de Filtros ---
    col1, col2, col3 = st.columns(3)

    # 1. Marca
    brands = sorted(df_vehicles['brand_car'].unique())
    with col1:
        selected_brand = st.selectbox("1. Marca", ["Seleccione..."] + brands)

    if selected_brand != "Seleccione...":
        # Filtrar por marca
        df_brand = df_vehicles[df_vehicles['brand_car'] == selected_brand]
        
        # 2. Modelo
        models = sorted(df_brand['model'].unique())
        with col2:
            selected_model = st.selectbox("2. Modelo", ["Seleccione..."] + models)
        
        if selected_model != "Seleccione...":
            # Filtrar por modelo
            df_model = df_brand[df_brand['model'] == selected_model]
            
            # 3. Versión
            # Usamos el 'version_str' que creamos
            versions_map = dict(zip(df_model['version_str'], df_model['vehicle_id']))
            versions_list = sorted(versions_map.keys())
            
            with col3:
                selected_version_str = st.selectbox("3. Versión / Motor", ["Seleccione..."] + versions_list)
            
            if selected_version_str != "Seleccione...":
                selected_vehicle_id = versions_map[selected_version_str]
                
                st.divider()
                st.subheader(f"Resultados para: {selected_brand} {selected_version_str}")
                
                # Buscar partes
                with st.spinner("Buscando filtros compatibles..."):
                    df_parts = get_parts_for_vehicle(selected_vehicle_id)
                
                if not df_parts.empty:
                    # Grouping order
                    group_order = ['oil', 'air', 'cabin', 'fuel']
                    
                    # Display by groups
                    found_any_group = False
                    for p_type in group_order:
                        # Filter by raw_type
                        subset = df_parts[df_parts['raw_type'] == p_type]
                        if not subset.empty:
                            found_any_group = True
                            # Get pretty name from the first row's 'Tipo' col
                            header_name = subset.iloc[0]['Tipo'].upper()
                            st.subheader(header_name)
                            
                            # Display as list: Brand: Code
                            for _, row in subset.iterrows():
                                # Main info
                                line = f"**{row['Marca']}**: `{row['Código']}`"
                                st.markdown(line)
                                
                                # Conditional notes
                                notes = row.get('Notes')
                                if notes:
                                    st.caption(f"📝 {notes}")
                    
                    # Handle 'other' types if any
                    others = df_parts[~df_parts['raw_type'].isin(group_order)]
                    if not others.empty:
                        st.subheader("OTROS")
                        for _, row in others.iterrows():
                            line = f"**{row['Marca']}**: `{row['Código']}` ({row['Tipo']})"
                            st.markdown(line)
                            if row.get('Notes'):
                                st.caption(f"📝 {row.get('Notes')}")

                else:
                    st.info("No se encontraron filtros registrados para este vehículo.")

                # --- Debug Section (Specific to selection) ---
                if debug_mode:
                    st.divider()
                    st.write(f"**Vehicle ID**: `{selected_vehicle_id}`")
                    st.write("**Raw Vehicle Data**:")
                    st.json(df_model[df_model['vehicle_id'] == selected_vehicle_id].to_dict(orient='records'))
                    
                    # Test parts query directly and show result
                    try:
                        st.write("**Debugging Parts Query...**")
                        response_debug = supabase.table("vehicle_part")\
                            .select("role, notes, source_catalog, part(brand_filter, part_code, part_type, media_type, notes)")\
                            .eq("vehicle_id", selected_vehicle_id)\
                            .execute()
                        st.write(f"Raw Response Count: {len(response_debug.data)}")
                        st.json(response_debug.data)
                    except Exception as e:
                        st.error(f"Query Error: {e}")

                    if not df_parts.empty:
                        st.write("**Processed Parts Data**:")
                        st.dataframe(df_parts)
