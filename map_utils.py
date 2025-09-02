import os
import math
import geopandas as gpd
import pandas as pd
import folium
from folium.plugins import MarkerCluster
from folium.features import GeoJsonTooltip, DivIcon
import branca.colormap as cm
import geopy.distance
from pymongo import MongoClient
from sklearn.preprocessing import MinMaxScaler, StandardScaler
from sklearn.decomposition import PCA
import logging
from datetime import datetime

# ----------------------------
# 전역 변수 및 설정
# ----------------------------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
# 데이터 파일 경로 설정
REGIONS = {
    '부산광역시': os.path.join(BASE_DIR, 'data', 'geojson', 'hangjeongdong_부산광역시.geojson'),
    '울산광역시': os.path.join(BASE_DIR, 'data', 'geojson', 'hangjeongdong_울산광역시.geojson'),
    '경상북도': os.path.join(BASE_DIR, 'data', 'geojson', 'hangjeongdong_경상북도.geojson'),
    '전라남도': os.path.join(BASE_DIR, 'data', 'geojson', 'hangjeongdong_전라남도.geojson'),
    '전라북도': os.path.join(BASE_DIR, 'data', 'geojson', 'hangjeongdong_전라북도.geojson'),
    '경상남도': os.path.join(BASE_DIR, 'data', 'geojson', 'hangjeongdong_경상남도.geojson'),
    '대구광역시': os.path.join(BASE_DIR, 'data', 'geojson', 'hangjeongdong_대구광역시.geojson'),
    '광주광역시': os.path.join(BASE_DIR, 'data', 'geojson', 'hangjeongdong_광주광역시.geojson'),
    '강원특별자치도': os.path.join(BASE_DIR, 'data', 'geojson', 'hangjeongdong_강원도.geojson'),
}
POP_PATH = os.path.join(BASE_DIR, 'data', 'population2.xlsx')
SHEL_PATH = os.path.join(BASE_DIR, 'data', 'shelter.xlsx')
POI_PATH = os.path.join(BASE_DIR, 'data', 'poi_data.csv')

# 대기 안정도 매핑
stab_map = {'A': 0.2, 'B': 0.4, 'C': 0.6, 'D': 0.8, 'E': 1.0, 'F': 1.2, 'G': 1.5}
korean_to_category = {
    '심한 불안정': 'A', '불안정': 'B', '약간 불안정': 'C',
    '중립': 'D', '약간 안정': 'E', '안정': 'F', '심한 안정': 'G'
}

# 발전소 좌표 및 MongoDB 설정
power_plants = {
    '고리': (35.321499, 129.291612),
    '월성': (35.713058, 129.475347),
    '한빛': (35.415534, 126.416692),
    '한울': (37.085932, 129.390857)
}
mapping_codes = {'고리': 'KR', '월성': 'WS', '한빛': 'YK', '한울': 'UJ'}

# 로깅 설정
if not logging.root.handlers:
    logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
mongo_uri = os.getenv("MONGO_URI", "mongodb://localhost:27017/")
try:
    client = MongoClient(mongo_uri)
    db = client['Data']
    col = db['NPP_weather']
except Exception as e:
    logging.error(f"MongoDB 연결 실패: {e}")
    # 웹 앱에서는 오류를 반환하거나 대체 로직을 실행해야 합니다.
    col = None


# ----------------------------
# 유동인구 지수 관련 함수
# ----------------------------
def get_time_context():
    now = datetime.now()
    hour, weekday = now.hour, now.weekday()
    slot = '오전' if 6 <= hour < 12 else '오후' if 12 <= hour < 18 else '야간' if 18 <= hour < 24 else '심야'
    is_weekend = (weekday >= 5)
    is_vacation = (now.month == 7 or now.month == 8) or (now.month == 12 or now.month == 1)
    return slot, is_weekend, is_vacation


def get_poi_weights():
    slot, is_weekend, is_vacation = get_time_context()
    default_weight = 0.4
    time_weights_weekday = {'한식': 0.8, '식료품 소매': 0.6, '의원': 0.5, '초등학교': 0.8, '중학교': 0.7, '고등학교': 0.9}
    time_weights_weekend = {'한식': 1.0, '식료품 소매': 0.8, '의원': 0.3, '초등학교': 0.001, '중학교': 0.001, '고등학교': 0.1}
    time_weights_vacation = {'한식': 0.9, '식료품 소매': 0.7, '의원': 0.4, '초등학교': 0.2, '중학교': 0.15, '고등학교': 0.25}
    if is_vacation:
        slot_weights = time_weights_vacation.get(slot, {})
    elif is_weekend:
        slot_weights = time_weights_weekend.get(slot, {})
    else:
        slot_weights = time_weights_weekday.get(slot, {})
    return slot_weights, default_weight


# ----------------------------
# GeoDataFrame 로드 및 전처리 (초기 한 번만 실행)
# ----------------------------
def _load_and_prepare_data():
    try:
        gdfs = [gpd.read_file(path) for path in REGIONS.values()]
        gdf = pd.concat(gdfs, ignore_index=True)
        pop_df = pd.read_excel(POP_PATH)
        shel_df = pd.read_excel(SHEL_PATH)

        pop_df['sido_full'] = pop_df['광역지자체'].map({k: k for k in REGIONS})
        pop_df['adm_nm_full'] = pop_df['sido_full'] + ' ' + pop_df['행정구역'] + ' ' + pop_df['adm_cd']
        gdf = gdf.merge(pop_df[['adm_nm_full', 'population']], left_on='adm_nm', right_on='adm_nm_full', how='left')
        gdf.drop(columns=['adm_nm_full'], inplace=True)

        sg = gpd.GeoDataFrame(shel_df, geometry=gpd.points_from_xy(shel_df.longitude, shel_df.latitude),
                              crs='EPSG:4326')
        sg = gpd.sjoin(sg, gdf[['adm_nm', 'geometry']], how='left', predicate='within')
        cap_sum = sg.groupby('adm_nm')['capacity'].sum().reset_index().rename(columns={'capacity': 'capacity_sum'})
        gdf = gdf.merge(cap_sum, on='adm_nm', how='left').fillna({'capacity_sum': 0})

        proj = gdf.to_crs('EPSG:5179')
        proj['centroid'] = proj.geometry.centroid
        gdf['centroid_lat'] = proj['centroid'].to_crs('EPSG:4326').y
        gdf['centroid_lon'] = proj['centroid'].to_crs('EPSG:4326').x

        # POI 데이터 병합 및 유동인구 지수 계산
        poi_df = pd.read_csv(POI_PATH, encoding='cp949')
        slot_weights, default_weight = get_poi_weights()
        poi_df['time_weight'] = poi_df['상권업종중분류명'].map(slot_weights).fillna(default_weight)
        poi_grouped = poi_df.groupby(['시도명', '시군구명', '행정동명'])['time_weight'].sum().reset_index(name='poi_weighted')
        gdf[['시도명', '시군구명', '행정동명']] = gdf['adm_nm'].str.split(' ', n=2, expand=True)
        gdf = gdf.merge(poi_grouped, on=['시도명', '시군구명', '행정동명'], how='left').fillna({'poi_weighted': 0})
        gdf['commercial_index'] = MinMaxScaler().fit_transform(gdf[['poi_weighted']])
        gdf['dynamic_population'] = gdf['population'] * (1 + 0.5 * gdf['commercial_index'])

        return gdf
    except Exception as e:
        logging.error(f"데이터 로드 및 전처리 중 오류 발생: {e}")
        return None


_GDF = _load_and_prepare_data()


# ----------------------------
# TOPSIS 계산 및 유틸리티 함수
# ----------------------------
def _distance(lat1, lon1, lat2, lon2):
    R = 6371.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    return R * c


def _bearing(lat1, lon1, lat2, lon2):
    lat1r, lat2r = math.radians(lat1), math.radians(lat2)
    dlr = math.radians(lon2 - lon1)
    x = math.sin(dlr) * math.cos(lat2r)
    y = math.cos(lat1r) * math.sin(lat2r) - math.sin(lat1r) * math.cos(lat2r) * math.cos(dlr)
    return (math.degrees(math.atan2(x, y)) + 360) % 360


def _wind_risk(wd, ws, bearing, sw, dist):
    adj = (wd + 180) % 360
    rel = abs(adj - bearing)
    if rel > 180:
        rel = 360 - rel
    return max(ws * math.cos(math.radians(rel)) / (1 + 0.05 * dist) * (1 / sw), 0)


def fetch_weather(plant):
    if col is None:
        raise ConnectionError("MongoDB에 연결할 수 없습니다.")
    code = mapping_codes.get(plant)
    if not code: raise KeyError(f"Unknown plant '{plant}'")
    doc = col.find_one({'genName': code}, sort=[('time', -1)])
    if not doc: raise ValueError(f"No data for '{plant}'")
    wd = doc.get('winddirection') or doc.get('wind_direction')
    ws = doc.get('windspeed') or doc.get('wind_speed')
    sw_str = doc.get('stability', '')
    sw_cat = korean_to_category.get(sw_str, 'D')
    sw = stab_map[sw_cat]
    return wd, ws, sw


def get_angle_width(stability_weight):
    min_w, max_w = 30, 60
    return max(min_w, min(max_w, max_w - (stability_weight - 0.2) * (max_w - min_w) / (1.5 - 0.2)))


def generate_sector(lat, lon, bearing, width, radius_km=100, points=50):
    start = bearing - width / 2
    angs = [start + i * (width / points) for i in range(points + 1)]
    coords = [(lat, lon)]
    for ang in angs:
        dest = geopy.distance.distance(kilometers=radius_km).destination((lat, lon), ang)
        coords.append((dest.latitude, dest.longitude))
    return coords


# ----------------------------
# TOPSIS 맵 및 데이터 생성 메인 함수
# ----------------------------
def _run_topsis_analysis(plant):
    if _GDF is None:
        raise RuntimeError("데이터 로드 실패, TOPSIS 분석을 수행할 수 없습니다.")
    df = _GDF.copy()
    lat, lon = power_plants[plant]
    wd, ws, sw = fetch_weather(plant)

    df['dist'] = df.apply(lambda r: _distance(lat, lon, r['centroid_lat'], r['centroid_lon']), axis=1)
    df = df[df['dist'] <= 120]
    df['dist_score'] = df['dist'].apply(lambda d: d if d <= 60 else max(0, 120 - 2 * d))
    df['bearing'] = df.apply(lambda r: _bearing(lat, lon, r['centroid_lat'], r['centroid_lon']), axis=1)
    df['wind_risk'] = df.apply(lambda r: _wind_risk(wd, ws, r['bearing'], sw, r['dist']), axis=1)

    df['SC_dynamic%'] = df.apply(
        lambda r: (r['capacity_sum'] / r['dynamic_population'] * 100) if r['dynamic_population'] > 0 else 0, axis=1)
    cap_df = df[['SC_dynamic%', 'capacity_sum']].fillna(0)
    cap_std = StandardScaler().fit_transform(cap_df)
    df['cap_pc1'] = PCA(n_components=1).fit_transform(cap_std).flatten()

    crit = df[['dist_score', 'cap_pc1', 'wind_risk', 'commercial_index']].fillna(0)
    weights = pd.Series({'dist_score': 0.30, 'cap_pc1': 0.20, 'wind_risk': 0.30, 'commercial_index': 0.20})
    norm_vals = MinMaxScaler().fit_transform(crit)
    norm_df = pd.DataFrame(norm_vals, columns=crit.columns, index=crit.index)
    weighted_df = norm_df.mul(weights, axis=1)

    ideal_best = weighted_df.max()
    ideal_best['wind_risk'] = weighted_df['wind_risk'].min()
    ideal_best['commercial_index'] = weighted_df['commercial_index'].min()
    ideal_worst = weighted_df.min()
    ideal_worst['wind_risk'] = weighted_df['wind_risk'].max()
    ideal_worst['commercial_index'] = weighted_df['commercial_index'].max()

    def compute_topsis(idx):
        w = weighted_df.loc[idx]
        d_best = math.sqrt(sum((w[c] - ideal_best[c]) ** 2 for c in weighted_df.columns))
        d_worst = math.sqrt(sum((w[c] - ideal_worst[c]) ** 2 for c in weighted_df.columns))
        return d_worst / (d_best + d_worst) if (d_best + d_worst) else 0

    df['topsis_score'] = [compute_topsis(i) for i in norm_df.index]
    return df, {'wind_direction': wd, 'wind_speed': ws, 'stability_weight': sw}


def generate_topsis_map_html(plant):
    df, weather = _run_topsis_analysis(plant)
    lat, lon = power_plants[plant]
    wd, sw = weather['wind_direction'], weather['stability_weight']

    m = folium.Map(location=[36.0, 127.5], zoom_start=8, tiles='cartodbpositron')

    # 풍향 화살표 및 부채꼴
    angle = (wd + 180) % 360
    arrow_html = f"""<div style="transform:rotate({angle}deg);font-size:36px;color:blue"><i class="fa fa-arrow-circle-right"></i></div>"""
    folium.Marker(location=[lat, lon], icon=DivIcon(icon_size=(50, 50), icon_anchor=(25, 25), html=arrow_html),
                  popup=f"Wind: {wd}°").add_to(m)

    width = get_angle_width(sw)
    coords = generate_sector(lat, lon, (wd + 180) % 360, width)
    folium.Polygon(locations=coords, color='red', weight=2, fill=True, fill_color='red', fill_opacity=0.4,
                   popup=f"풍향: {wd}°").add_to(m)

    # TOPSIS 점수 시각화
    cm_top = cm.LinearColormap(['#313695', '#ffffff', '#A50026'], index=[0, 0.5, 1], vmin=0, vmax=1,
                               caption='TOPSIS Score')
    folium.GeoJson(
        df,
        style_function=lambda feat: {
            'fillColor': cm_top(feat['properties']['topsis_score']),
            'color': 'black', 'weight': 1, 'fillOpacity': 0.9
        },
        tooltip=GeoJsonTooltip(fields=['adm_nm', 'population', 'topsis_score'], aliases=['행정동', '인구', 'TOPSIS'],
                               localize=True)
    ).add_to(m)
    cm_top.add_to(m)

    # Top 5 마커
    top5_sites = df.nlargest(5, 'topsis_score')
    for _, row in top5_sites.iterrows():
        folium.Marker(
            [row['centroid_lat'], row['centroid_lon']],
            popup=f"{row['adm_nm']} ({row['topsis_score']:.3f})",
            icon=folium.Icon(color='darkred', icon='hospital')
        ).add_to(m)

    return m._repr_html_()


def compute_top5_for(plant):
    df, _ = _run_topsis_analysis(plant)
    top5 = df.nlargest(5, 'topsis_score')
    return [
        {
            'name': row['adm_nm'],
            'address': row['adm_nm'],
            'capacity': int(row['capacity_sum']),
            'topsis_score': round(float(row['topsis_score']), 3),
            'lat': float(row['centroid_lat']),
            'lon': float(row['centroid_lon'])
        }
        for _, row in top5.iterrows()
    ]