import streamlit as st
import xmlrpc.client
from datetime import datetime, timedelta
import pandas as pd
from zoneinfo import ZoneInfo
import time

st.set_page_config(page_title="Termómetro Cuervo", layout="wide")

st_autorefresh = st.empty()

# ==========================================
# 1. CONEXIÓN A ODOO
# ==========================================
@st.cache_resource
def conectar_odoo():
    url = st.secrets["ODOO_URL"]
    db = st.secrets["ODOO_DB"]
    user = st.secrets["ODOO_USER"]
    password = st.secrets["ODOO_PASSWORD"]
    
    common = xmlrpc.client.ServerProxy(f'{url}/xmlrpc/2/common')
    uid = common.authenticate(db, user, password, {})
    models = xmlrpc.client.ServerProxy(f'{url}/xmlrpc/2/object')
    return uid, models, db, password

@st.cache_data
def cargar_historico():
    return pd.read_csv("historico_base.csv")

def obtener_datos_hoy(uid, models, db, password):
    zona_ba = ZoneInfo("America/Argentina/Buenos_Aires")
    ayer = datetime.now(zona_ba) - timedelta(days=1)
    search_start = ayer.strftime('%Y-%m-%d 00:00:00')
    
    locales = models.execute_kw(db, uid, password, 'pos.config', 'search_read', [[]], {'fields': ['name', 'id']})
    mapa_locales = {c['id']: str(c['name']).strip().upper() for c in locales}
    
    tk_products = models.execute_kw(db, uid, password, 'product.product', 'search', [[
        ['product_template_attribute_value_ids.product_attribute_value_id', 'in', [31, 101]]
    ]])
    tk_prod_ids = set(tk_products)
    
    categorias = models.execute_kw(db, uid, password, 'product.category', 'search_read', [[
        ['name', 'ilike', 'Heladería']
    ]], {'fields': ['id']})
    cat_ids = [c['id'] for c in categorias] if categorias else []
    
    heladeria_prods = models.execute_kw(db, uid, password, 'product.product', 'search_read', [[
        ['categ_id', 'in', cat_ids]
    ]], {'fields': ['id', 'name']}) if cat_ids else []
    heladeria_ids = {p['id']: str(p['name']).upper() for p in heladeria_prods}
    
    ordenes = models.execute_kw(db, uid, password, 'pos.order', 'search_read',
        [[['state', 'in', ['paid', 'done', 'invoiced']], ['date_order', '>=', search_start]]],
        {'fields': ['id', 'config_id', 'date_order']})
        
    if not ordenes: return pd.DataFrame()
        
    df_ordenes = pd.DataFrame(ordenes)
    df_ordenes['config_id'] = df_ordenes['config_id'].apply(lambda x: x[0] if isinstance(x, list) else x)
    df_ordenes['Local'] = df_ordenes['config_id'].map(mapa_locales)
    
    df_ordenes['date_order'] = pd.to_datetime(df_ordenes['date_order']) - pd.Timedelta(hours=3)
    df_ordenes['Fecha'] = df_ordenes['date_order'].dt.date
    df_ordenes['Hora'] = df_ordenes['date_order'].dt.hour
    
    hoy_arg = datetime.now(zona_ba).date()
    df_ordenes = df_ordenes[df_ordenes['Fecha'] == hoy_arg]
    
    if df_ordenes.empty: return pd.DataFrame()
        
    ordenes_ids = df_ordenes['id'].tolist()
    
    lineas = models.execute_kw(db, uid, password, 'pos.order.line', 'search_read',
        [[['order_id', 'in', ordenes_ids], ['product_id', '!=', False]]], 
        {'fields': ['order_id', 'product_id', 'full_product_name', 'qty']})
        
    df_lineas = pd.DataFrame(lineas)
    df_lineas['order_id'] = df_lineas['order_id'].apply(lambda x: x[0])
    df_lineas['product_id'] = df_lineas['product_id'].apply(lambda x: x[0])
    
    def extract_data(row):
        pid = row['product_id']
        name = str(row['full_product_name']).upper()
        qty = row['qty']
        es_tk = 1 if pid in tk_prod_ids or any(x in name for x in ["(TK)", " TK", "TK ", "TAKE AWAY"]) else 0
        es_helado = 1 if pid in heladeria_ids else 0
        
        q_tk = qty if es_tk else 0
        q_sal = qty if not es_tk else 0
        q_hel = qty if es_helado else 0
        q_k = qty if (es_helado and 'KILO' in name) else 0
        q_m = qty if (es_helado and 'MEDIO' in name) else 0
        q_v = qty if (es_helado and 'VASO' in name) else 0
        
        return pd.Series([q_tk, q_sal, q_hel, q_k, q_m, q_v])

    df_lineas[['Qty_TK', 'Qty_Salon', 'Qty_Helado', 'Qty_Kilo', 'Qty_Medio', 'Qty_Vaso']] = df_lineas.apply(extract_data, axis=1)
    
    tickets_agrupados = df_lineas.groupby('order_id').agg(
        Total_TK=('Qty_TK', 'sum'), Total_Salon=('Qty_Salon', 'sum'),
        Total_Helados=('Qty_Helado', 'sum'), Total_Kilos=('Qty_Kilo', 'sum'),
        Total_Medios=('Qty_Medio', 'sum'), Total_Vasos=('Qty_Vaso', 'sum')
    ).reset_index()
    
    return pd.merge(df_ordenes, tickets_agrupados, left_on='id', right_on='order_id', how='left').fillna(0)

# ==========================================
# 4. INTERFAZ GRÁFICA
# ==========================================
st.title("🔥 Termómetro Cuervo - Ocupación en Vivo")
st.markdown("Comparativa de **tendencia diaria** y ocupación en los últimos 60 minutos.")

# Diccionario de sillas reales por local. 
# Si modificás las sillas o ponés mesas nuevas, ajustá el número acá directamente.
CAPACIDAD_SILLAS = {
    'COSTA RICA': 36,     # 10 blancas + 8 madera = 18 mesas x 2
    'FRENCH': 20,         # 6 cuadradas + 4 redondas = 10 mesas x 2
    'JURAMENTO': 40,      # 20 mesas x 2
    'EL SALVADOR': 44,    # 6 chicas (x2) + 8 grandes (x4) = 44
    'NEWBERY': 44,        # 14 mesas estandar (x2) + 2 mesones (x8 aprox) = 44
}

dias_nombres = {1: 'Martes', 2: 'Miércoles', 3: 'Jueves', 4: 'Viernes', 5: 'Sábado', 6: 'Domingo'}
ahora_arg = datetime.now(ZoneInfo("America/Argentina/Buenos_Aires"))
ahora_naive = ahora_arg.replace(tzinfo=None)
hace_una_hora_naive = ahora_naive - timedelta(hours=1)

dia_actual_num = ahora_arg.weekday()
hora_actual = ahora_arg.hour
minuto_actual = ahora_arg.minute

idx_default = list(dias_nombres.keys()).index(dia_actual_num) if dia_actual_num in dias_nombres else 0

col_btn, col_select, col_metric = st.columns([1, 2, 2])
with col_btn:
    st.write("") 
    if st.button("🔄 Actualizar Datos"):
        st.cache_data.clear()

with col_select:
    dia_elegido_nombre = st.selectbox("Comparar contra histórico del día:", options=list(dias_nombres.values()), index=idx_default)

with col_metric:
    metrica_grafico = st.radio("Métrica para los gráficos:", options=["Tickets", "Volumen (Bebidas/Helados)"], horizontal=True)

dia_comparacion = [k for k, v in dias_nombres.items() if v == dia_elegido_nombre][0]

uid, models, db, password = conectar_odoo()
historico = cargar_historico()
historico['Promedio_Bebidas'] = historico['Promedio_TK'] + historico['Promedio_Salon']

with st.spinner("Consultando estado de las cajas de hoy..."):
    df_vivo = obtener_datos_hoy(uid, models, db, password)

if df_vivo.empty:
    st.info("No hay tickets registrados en el día de hoy.")
else:
    ventas_hoy = df_vivo.groupby(['Local', 'Hora']).agg(
        Vivo_Tickets=('id', 'count'), Vivo_TK=('Total_TK', 'sum'), Vivo_Salon=('Total_Salon', 'sum'),
        Vivo_Helados=('Total_Helados', 'sum')
    ).reset_index()
    ventas_hoy['Vivo_Bebidas'] = ventas_hoy['Vivo_TK'] + ventas_hoy['Vivo_Salon']
    
    hist_comparacion = historico[historico['Dia_Semana'] == dia_comparacion].copy()
    locales_activos = sorted(df_vivo['Local'].unique())
    
    datos_locales = {}
    for local in locales_activos:
        df_local = df_vivo[df_vivo['Local'] == local]
        df_ultima_hora = df_local[(df_local['date_order'] >= hace_una_hora_naive) & (df_local['date_order'] <= ahora_naive)]
        
        tickets_hoy = df_ultima_hora['id'].nunique()
        
        # Extracción Café
        tk_hoy = df_ultima_hora['Total_TK'].sum()
        salon_hoy = df_ultima_hora['Total_Salon'].sum()
        bebidas_hoy = tk_hoy + salon_hoy
        
        # Extracción Heladería
        helados_hoy = df_ultima_hora['Total_Helados'].sum()
        kilos_hoy = df_ultima_hora['Total_Kilos'].sum()
        medios_hoy = df_ultima_hora['Total_Medios'].sum()
        vasos_hoy = df_ultima_hora['Total_Vasos'].sum()
        
        kpi_hist_actual = hist_comparacion[(hist_comparacion['Local'] == local) & (hist_comparacion['Hora'] == hora_actual)]
        kpi_hist_anterior = hist_comparacion[(hist_comparacion['Local'] == local) & (hist_comparacion['Hora'] == (hora_actual - 1))]
        
        def ponderar(col):
            act = kpi_hist_actual[col].sum() if not kpi_hist_actual.empty else 0
            ant = kpi_hist_anterior[col].sum() if not kpi_hist_anterior.empty else 0
            return (ant * ((60 - minuto_actual) / 60.0)) + (act * (minuto_actual / 60.0))
            
        tickets_hist_prop = ponderar('Promedio_Tickets')
        tk_hist_prop = ponderar('Promedio_TK')
        salon_hist_prop = ponderar('Promedio_Salon')
        helados_hist_prop = ponderar('Promedio_Helados')
        kilos_hist_prop = ponderar('Promedio_Kilos')
        medios_hist_prop = ponderar('Promedio_Medios')
        vasos_hist_prop = ponderar('Promedio_Vasos')
        
        if 'GAVIOTA' in local:
            pico_dia = hist_comparacion[hist_comparacion['Local'] == local]['Promedio_Helados'].max()
            saturacion = (helados_hoy / pico_dia * 100) if pico_dia > 0 else 0
        else:
            pico_dia = hist_comparacion[hist_comparacion['Local'] == local]['Promedio_Salon'].max()
            saturacion = (salon_hoy / pico_dia * 100) if pico_dia > 0 else 0
            
        ratio_tk = (tk_hoy / bebidas_hoy * 100) if bebidas_hoy > 0 else 0
        
        # --- CÁLCULO DE OCUPACIÓN FÍSICA ---
        es_patio = any(x in local for x in ['GAVIOTA', 'PASEO DEL SOL'])
        sillas_totales = CAPACIDAD_SILLAS.get(local, 30) # Default de seguridad
        ocupacion_fisica = (salon_hoy / sillas_totales * 100) if sillas_totales > 0 else 0
        
        datos_locales[local] = {
            'tickets_hoy': tickets_hoy, 'tickets_hist_prop': tickets_hist_prop, 'saturacion': saturacion,
            'tk_hoy': tk_hoy, 'salon_hoy': salon_hoy, 'tk_hist_prop': tk_hist_prop, 'salon_hist_prop': salon_hist_prop, 'ratio_tk': ratio_tk,
            'helados_hoy': helados_hoy, 'kilos_hoy': kilos_hoy, 'medios_hoy': medios_hoy, 'vasos_hoy': vasos_hoy,
            'helados_hist_prop': helados_hist_prop, 'kilos_hist_prop': kilos_hist_prop, 'medios_hist_prop': medios_hist_prop, 'vasos_hist_prop': vasos_hist_prop,
            'ocupacion_fisica': ocupacion_fisica, 'es_patio': es_patio
        }
        
    locales_ordenados = sorted(locales_activos, key=lambda x: datos_locales[x]['saturacion'], reverse=True)
    
    for local in locales_ordenados:
        datos = datos_locales[local]
        
        if datos['saturacion'] >= 90: estado = "🔴 Explotado"
        elif datos['saturacion'] >= 60: estado = "🟡 Movimiento Normal"
        else: estado = "🟢 Hay Lugar"
            
        if 'GAVIOTA' in local:
            st.subheader(f"📍 {local} — {estado} 🍦")
        else:
            st.subheader(f"📍 {local} — {estado}")
        
        def delta_color(vivo, hist_prop):
            return "normal" if (vivo - hist_prop) <= 0 else "inverse"
            
        hora_desde = hace_una_hora_naive.strftime("%H:%M")
        hora_hasta = ahora_naive.strftime("%H:%M")
        
        if 'GAVIOTA' in local:
            col1, col2, col3, col4, col5 = st.columns(5)
            with col1:
                st.metric(f"Tickets ({hora_desde} a {hora_hasta})", int(datos['tickets_hoy']), f"{datos['tickets_hoy'] - datos['tickets_hist_prop']:.1f} vs normal", delta_color=delta_color(datos['tickets_hoy'], datos['tickets_hist_prop']))
            with col2:
                st.metric("Helados (Total)", int(datos['helados_hoy']), f"{datos['helados_hoy'] - datos['helados_hist_prop']:.1f} vs normal", delta_color=delta_color(datos['helados_hoy'], datos['helados_hist_prop']))
            with col3:
                st.metric("Kilos", int(datos['kilos_hoy']), f"{datos['kilos_hoy'] - datos['kilos_hist_prop']:.1f} vs normal", delta_color=delta_color(datos['kilos_hoy'], datos['kilos_hist_prop']))
            with col4:
                st.metric("Medios", int(datos['medios_hoy']), f"{datos['medios_hoy'] - datos['medios_hist_prop']:.1f} vs normal", delta_color=delta_color(datos['medios_hoy'], datos['medios_hist_prop']))
            with col5:
                st.metric("Vasos", int(datos['vasos_hoy']), f"{datos['vasos_hoy'] - datos['vasos_hist_prop']:.1f} vs normal", delta_color=delta_color(datos['vasos_hoy'], datos['vasos_hist_prop']))
        else:
            col1, col2, col3, col4, col5 = st.columns(5)
            with col1:
                st.metric(f"Tickets ({hora_desde} a {hora_hasta})", int(datos['tickets_hoy']), f"{datos['tickets_hoy'] - datos['tickets_hist_prop']:.1f} vs normal", delta_color=delta_color(datos['tickets_hoy'], datos['tickets_hist_prop']))
            with col2:
                st.metric("Bebidas Take Away", int(datos['tk_hoy']), f"{datos['tk_hoy'] - datos['tk_hist_prop']:.1f} vs normal", delta_color=delta_color(datos['tk_hoy'], datos['tk_hist_prop']))
            with col3:
                st.metric("Bebidas Salón", int(datos['salon_hoy']), f"{datos['salon_hoy'] - datos['salon_hist_prop']:.1f} vs normal", delta_color=delta_color(datos['salon_hoy'], datos['salon_hist_prop']))
            with col4:
                ratio_int = int(datos['ratio_tk'])
                st.metric("% Take Away", f"{ratio_int}%")
                st.progress(ratio_int / 100.0)
            with col5:
                if datos['es_patio']:
                    st.metric("Ocupación", "Patio C.")
                else:
                    ocup_int = int(datos['ocupacion_fisica'])
                    st.metric("Ocupación Salón", f"{ocup_int}%")
                    st.progress(min(ocup_int / 100.0, 1.0)) # Topeado en 1.0 (100%) para que la barra no de error si hay gente de pie
                      
        # GRÁFICO TENDENCIA
        if metrica_grafico == "Tickets":
            col_hist, col_vivo = 'Promedio_Tickets', 'Vivo_Tickets'
        else:
            col_hist = 'Promedio_Helados' if 'GAVIOTA' in local else 'Promedio_Bebidas'
            col_vivo = 'Vivo_Helados' if 'GAVIOTA' in local else 'Vivo_Bebidas'
        
        chart_hist = hist_comparacion[hist_comparacion['Local'] == local][['Hora', col_hist]].set_index('Hora')
        chart_hoy = ventas_hoy[ventas_hoy['Local'] == local][['Hora', col_vivo]].set_index('Hora')
        
        df_chart = pd.merge(chart_hist, chart_hoy, left_index=True, right_index=True, how='outer').fillna(0).sort_index()
        df_chart = df_chart.reindex(range(7, 20)).fillna(0)
        df_chart.columns = ['Promedio Histórico', 'Hoy (En Vivo)']
        
        df_chart['Hoy (Proyectado)'] = float('nan')
        df_chart['Hoy (Completado)'] = df_chart['Hoy (En Vivo)']
        df_chart.loc[hora_actual+1:, 'Hoy (Completado)'] = float('nan')

        if hora_actual in df_chart.index:
            ventas_parciales = df_chart.at[hora_actual, 'Hoy (En Vivo)']
            hist_hasta_ahora = df_chart.loc[:hora_actual-1, 'Promedio Histórico'].sum()
            hoy_hasta_ahora = df_chart.loc[:hora_actual-1, 'Hoy (En Vivo)'].sum()
            factor_dia = (hoy_hasta_ahora / hist_hasta_ahora) if hist_hasta_ahora > 0 else 1
            
            hist_hora_actual = df_chart.at[hora_actual, 'Promedio Histórico']
            minutos_restantes = 60 - minuto_actual
            ventas_restantes_esperadas = hist_hora_actual * (minutos_restantes / 60.0) * factor_dia
            
            proyectado = ventas_parciales + ventas_restantes_esperadas
            if (hora_actual - 1) in df_chart.index: df_chart.at[hora_actual - 1, 'Hoy (Proyectado)'] = df_chart.at[hora_actual - 1, 'Hoy (Completado)']
            
            df_chart.at[hora_actual, 'Hoy (Proyectado)'] = proyectado
            df_chart.at[hora_actual, 'Hoy (Completado)'] = float('nan')

        df_chart = df_chart.drop(columns=['Hoy (En Vivo)'])
        df_chart.index = df_chart.index + 1
        df_chart.index = [f"{int(h):02d}:00" for h in df_chart.index]
        
        st.line_chart(df_chart, color=['#6e6e6e', '#ff4b4b', '#ffaa00'], height=250)
        
        if not chart_hist.empty:
            hora_pico = chart_hist.idxmax().iloc[0]
            val_pico = int(chart_hist.max().iloc[0])
            st.caption(f"⚡ **Dato Operativo:** El pico histórico de los {dia_elegido_nombre} acá es a las **{int(hora_pico)}:00 hs** (aprox {val_pico} unidades).")
            
        st.divider()

time.sleep(120)
st.rerun()
