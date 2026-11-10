import streamlit as st
import xmlrpc.client
from datetime import datetime, timedelta
import pandas as pd
from zoneinfo import ZoneInfo

st.set_page_config(page_title="Termómetro Cuervo", layout="wide")

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

# ==========================================
# 2. CARGA DE HISTÓRICO
# ==========================================
@st.cache_data
def cargar_historico():
    return pd.read_csv("historico_base.csv")

# ==========================================
# 3. EXTRACCIÓN DE DATOS EN VIVO
# ==========================================
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
    
    ordenes = models.execute_kw(db, uid, password, 'pos.order', 'search_read',
        [[['state', 'in', ['paid', 'done', 'invoiced']], ['date_order', '>=', search_start]]],
        {'fields': ['id', 'config_id', 'date_order']})
        
    if not ordenes:
        return pd.DataFrame()
        
    df_ordenes = pd.DataFrame(ordenes)
    df_ordenes['config_id'] = df_ordenes['config_id'].apply(lambda x: x[0] if isinstance(x, list) else x)
    df_ordenes['Local'] = df_ordenes['config_id'].map(mapa_locales)
    
    df_ordenes['date_order'] = pd.to_datetime(df_ordenes['date_order']) - pd.Timedelta(hours=3)
    df_ordenes['Fecha'] = df_ordenes['date_order'].dt.date
    df_ordenes['Hora'] = df_ordenes['date_order'].dt.hour
    
    hoy_arg = datetime.now(zona_ba).date()
    df_ordenes = df_ordenes[df_ordenes['Fecha'] == hoy_arg]
    
    if df_ordenes.empty:
        return pd.DataFrame()
        
    ordenes_ids = df_ordenes['id'].tolist()
    
    lineas = models.execute_kw(db, uid, password, 'pos.order.line', 'search_read',
        [[['order_id', 'in', ordenes_ids], ['product_id', '!=', False]]], 
        {'fields': ['order_id', 'product_id', 'full_product_name', 'qty']})
        
    df_lineas = pd.DataFrame(lineas)
    df_lineas['order_id'] = df_lineas['order_id'].apply(lambda x: x[0])
    df_lineas['product_id'] = df_lineas['product_id'].apply(lambda x: x[0])
    
    def es_tk(row):
        pid = row['product_id']
        name = str(row['full_product_name']).upper()
        if pid in tk_prod_ids: return True
        if any(x in name for x in ["(TK)", " TK", "TK ", "TAKE AWAY"]): return True
        return False

    df_lineas['Es_TK'] = df_lineas.apply(es_tk, axis=1)
    df_lineas['Qty_TK'] = df_lineas.apply(lambda x: x['qty'] if x['Es_TK'] else 0, axis=1)
    df_lineas['Qty_Salon'] = df_lineas.apply(lambda x: x['qty'] if not x['Es_TK'] else 0, axis=1)
    
    tickets_agrupados = df_lineas.groupby('order_id').agg(
        Total_TK=('Qty_TK', 'sum'),
        Total_Salon=('Qty_Salon', 'sum')
    ).reset_index()
    
    df_final = pd.merge(df_ordenes, tickets_agrupados, left_on='id', right_on='order_id', how='left').fillna(0)
    return df_final

# ==========================================
# 4. INTERFAZ GRÁFICA
# ==========================================
st.title("🔥 Termómetro Cuervo - Ocupación en Vivo")
st.markdown("Comparativa de **tendencia diaria** y ocupación en los últimos 60 minutos.")

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
    dia_elegido_nombre = st.selectbox(
        "Comparar contra histórico del día:",
        options=list(dias_nombres.values()),
        index=idx_default
    )

with col_metric:
    metrica_grafico = st.radio(
        "Métrica para los gráficos:",
        options=["Tickets", "Bebidas (Total)"],
        horizontal=True
    )

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
        Vivo_Tickets=('id', 'count'),
        Vivo_TK=('Total_TK', 'sum'),
        Vivo_Salon=('Total_Salon', 'sum')
    ).reset_index()
    ventas_hoy['Vivo_Bebidas'] = ventas_hoy['Vivo_TK'] + ventas_hoy['Vivo_Salon']
    
    hist_comparacion = historico[historico['Dia_Semana'] == dia_comparacion].copy()
    locales_activos = sorted(df_vivo['Local'].unique())
    
# --- 1. PRE-CÁLCULO Y RANKING ---
    datos_locales = {}
    for local in locales_activos:
        df_local = df_vivo[df_vivo['Local'] == local]
        df_ultima_hora = df_local[(df_local['date_order'] >= hace_una_hora_naive) & (df_local['date_order'] <= ahora_naive)]
        
        tickets_hoy = df_ultima_hora['id'].nunique()
        tk_hoy = df_ultima_hora['Total_TK'].sum()
        salon_hoy = df_ultima_hora['Total_Salon'].sum()
        bebidas_hoy = tk_hoy + salon_hoy
        
        kpi_hist_actual = hist_comparacion[(hist_comparacion['Local'] == local) & (hist_comparacion['Hora'] == hora_actual)]
        kpi_hist_anterior = hist_comparacion[(hist_comparacion['Local'] == local) & (hist_comparacion['Hora'] == (hora_actual - 1))]
        
        t_hist_act = kpi_hist_actual['Promedio_Tickets'].sum() if not kpi_hist_actual.empty else 0
        t_hist_ant = kpi_hist_anterior['Promedio_Tickets'].sum() if not kpi_hist_anterior.empty else 0
        tk_hist_act = kpi_hist_actual['Promedio_TK'].sum() if not kpi_hist_actual.empty else 0
        tk_hist_ant = kpi_hist_anterior['Promedio_TK'].sum() if not kpi_hist_anterior.empty else 0
        salon_hist_act = kpi_hist_actual['Promedio_Salon'].sum() if not kpi_hist_actual.empty else 0
        salon_hist_ant = kpi_hist_anterior['Promedio_Salon'].sum() if not kpi_hist_anterior.empty else 0
        
        fraccion_act = minuto_actual / 60.0
        fraccion_ant = (60 - minuto_actual) / 60.0
        
        tickets_hist_prop = (t_hist_ant * fraccion_ant) + (t_hist_act * fraccion_act)
        tk_hist_prop = (tk_hist_ant * fraccion_ant) + (tk_hist_act * fraccion_act)
        salon_hist_prop = (salon_hist_ant * fraccion_ant) + (salon_hist_act * fraccion_act)
        
        # NUEVA LÓGICA DE SATURACIÓN BASADA EN CAPACIDAD FÍSICA
        # 1. Buscamos el pico máximo histórico de bebidas de salón para este día exacto
        pico_salon_dia = hist_comparacion[hist_comparacion['Local'] == local]['Promedio_Salon'].max()
        
        # 2. Medimos el volumen de salón de la última hora contra ese pico máximo
        saturacion = (salon_hoy / pico_salon_dia * 100) if pico_salon_dia > 0 else 0
        
        ratio_tk = (tk_hoy / bebidas_hoy * 100) if bebidas_hoy > 0 else 0
        
        datos_locales[local] = {
            'tickets_hoy': tickets_hoy, 'tk_hoy': tk_hoy, 'salon_hoy': salon_hoy,
            'tickets_hist_prop': tickets_hist_prop, 'tk_hist_prop': tk_hist_prop, 'salon_hist_prop': salon_hist_prop,
            'saturacion': saturacion, 'ratio_tk': ratio_tk, 'pico_salon': pico_salon_dia
        }
        
    locales_ordenados = sorted(locales_activos, key=lambda x: datos_locales[x]['saturacion'], reverse=True)
    
    # --- 2. RENDERIZADO VISUAL ---
    for local in locales_ordenados:
        datos = datos_locales[local]
        
        # Nuevos umbrales calibrados para el formato de pico diario
        if datos['saturacion'] >= 90:
            estado = "🔴 Explotado"
        elif datos['saturacion'] >= 60:
            estado = "🟡 Movimiento Normal"
        else:
            estado = "🟢 Hay Lugar"
            
        st.subheader(f"📍 {local} — {estado}")
        
        col1, col2, col3, col4 = st.columns(4)
        def delta_color(vivo, hist_prop):
            return "normal" if (vivo - hist_prop) <= 0 else "inverse"
            
        hora_desde = hace_una_hora_naive.strftime("%H:%M")
        hora_hasta = ahora_naive.strftime("%H:%M")
        
        with col1:
            st.metric(label=f"Tickets ({hora_desde} a {hora_hasta})", value=int(datos['tickets_hoy']), 
                      delta=f"{datos['tickets_hoy'] - datos['tickets_hist_prop']:.1f} vs normal",
                      delta_color=delta_color(datos['tickets_hoy'], datos['tickets_hist_prop']))
        with col2:
            st.metric(label="Bebidas Take Away", value=int(datos['tk_hoy']), 
                      delta=f"{datos['tk_hoy'] - datos['tk_hist_prop']:.1f} vs normal",
                      delta_color=delta_color(datos['tk_hoy'], datos['tk_hist_prop']))
        with col3:
            st.metric(label="Bebidas Salón", value=int(datos['salon_hoy']), 
                      delta=f"{datos['salon_hoy'] - datos['salon_hist_prop']:.1f} vs normal",
                      delta_color=delta_color(datos['salon_hoy'], datos['salon_hist_prop']))
        with col4:
            ratio_int = int(datos['ratio_tk'])
            st.metric(label="% Take Away (Últ. Hora)", value=f"{ratio_int}%")
            st.progress(ratio_int / 100.0)
                      
        # --- GRÁFICO TENDENCIA ---
        col_hist = 'Promedio_Tickets' if metrica_grafico == "Tickets" else 'Promedio_Bebidas'
        col_vivo = 'Vivo_Tickets' if metrica_grafico == "Tickets" else 'Vivo_Bebidas'
        
        chart_hist = hist_comparacion[hist_comparacion['Local'] == local][['Hora', col_hist]].set_index('Hora')
        chart_hoy = ventas_hoy[ventas_hoy['Local'] == local][['Hora', col_vivo]].set_index('Hora')
        
        df_chart = pd.merge(chart_hist, chart_hoy, left_index=True, right_index=True, how='outer').fillna(0).sort_index()
        
        # FORZAMOS el rango estricto de 7 a 19 para anclar las puntas (al sumar 1 quedará 8 a 20 exactos)
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
            
            if (hora_actual - 1) in df_chart.index:
                df_chart.at[hora_actual - 1, 'Hoy (Proyectado)'] = df_chart.at[hora_actual - 1, 'Hoy (Completado)']
            
            df_chart.at[hora_actual, 'Hoy (Proyectado)'] = proyectado
            df_chart.at[hora_actual, 'Hoy (Completado)'] = float('nan')

        df_chart = df_chart.drop(columns=['Hoy (En Vivo)'])
        df_chart.index = df_chart.index + 1
        df_chart.index = [f"{int(h):02d}:00" for h in df_chart.index]
        
        st.line_chart(df_chart, color=['#6e6e6e', '#ff4b4b', '#ffaa00'], height=250)
        
        # --- PREDICTOR DE PICO ---
        if not chart_hist.empty:
            hora_pico = chart_hist.idxmax().iloc[0]
            val_pico = int(chart_hist.max().iloc[0])
            st.caption(f"⚡ **Dato Operativo:** El pico histórico de los {dia_elegido_nombre} acá es a las **{int(hora_pico)}:00 hs** (aprox {val_pico} {metrica_grafico.lower()}).")
            
        st.divider()
