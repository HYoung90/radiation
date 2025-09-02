import os
import math
import time
from io import BytesIO
import geopandas as gpd
import pandas as pd
import folium
from folium.plugins import MarkerCluster
from folium.features import GeoJsonTooltip, DivIcon
import branca.colormap as cm
from sklearn.preprocessing import MinMaxScaler, StandardScaler
from sklearn.decomposition import PCA
from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from webdriver_manager.chrome import ChromeDriverManager
from datetime import datetime
from PIL import Image
import geopy.distance
from pymongo import MongoClient

# ==========================================
# 시간대/요일/방학 자동 판별 함수
# ==========================================
def get_time_context():
    now = datetime.now()
    hour, weekday = now.hour, now.weekday()  # 0=월, 6=일

    # 시간대 분류
    if 6 <= hour < 12:
        slot = '오전'
    elif 12 <= hour < 18:
        slot = '오후'
    elif 18 <= hour < 24:
        slot = '야간'
    else:
        slot = '심야'

    # 주말 판별
    is_weekend = (weekday >= 5)

    # 방학(여름/겨울) 예시
    is_summer_vac = (now.month == 7 and now.day >= 20) or (now.month == 8 and now.day <= 20)
    is_winter_vac = (now.month == 12 and now.day >= 24) or (now.month == 1 and now.day <= 31)
    is_vacation = is_summer_vac or is_winter_vac

    return slot, is_weekend, is_vacation

slot, is_weekend, is_vacation = get_time_context()

# ==========================================
# 업종별 시간대별 가중치 세트 (평일/주말/방학)
# ==========================================
# --- 평일
time_weights_weekday = {
    '오전': {
        '한식': 0.8, '식료품 소매': 0.6, '의원': 0.5, '이용·미용': 0.4, '주점': 0.2, '일반 숙박': 0.2,
        '초등학교': 0.8, '중학교': 0.7, '고등학교': 0.9,
    },
    '오후': {
        '한식': 0.6, '식료품 소매': 0.5, '입시·교과학원': 0.7, '카페': 0.6, '주점': 0.3, '일반 숙박': 0.2,
        '초등학교': 0.5, '중학교': 0.8, '고등학교': 1.0,
    },
    '야간': {
        '주점': 0.9, '한식': 0.5, '일반 숙박': 0.6, '모텔/여관': 0.7,
        '초등학교': 0.1, '중학교': 0.2, '고등학교': 0.6,
    },
    '심야': {
        '주점': 0.7, '일반 숙박': 0.9,
        '초등학교': 0.05, '중학교': 0.05, '고등학교': 0.1,
    }
}
# --- 주말(음식/카페/숙박↑, 학교↓)
time_weights_weekend = {
    '오전': {
        '한식': 1.0, '식료품 소매': 0.8, '의원': 0.3, '이용·미용': 0.6, '주점': 0.3, '일반 숙박': 0.4,
        '초등학교': 0.001, '중학교': 0.001, '고등학교': 0.1,
    },
    '오후': {
        '한식': 1.1, '식료품 소매': 0.7, '입시·교과학원': 0.4, '카페': 0.9, '주점': 0.6, '일반 숙박': 0.5,
        '초등학교': 0.001, '중학교': 0.001, '고등학교': 0.1,
    },
    '야간': {
        '주점': 1.2, '한식': 0.6, '일반 숙박': 0.8, '모텔/여관': 1.0,
        '초등학교': 0.001, '중학교': 0.001, '고등학교': 0.1,
    },
    '심야': {
        '주점': 1.0, '일반 숙박': 1.1,
        '초등학교': 0.01, '중학교': 0.01, '고등학교': 0.05,
    }
}
# --- 방학(학교 대폭↓, 학원도 조정)
time_weights_vacation = {
    '오전': {
        '한식': 0.9, '식료품 소매': 0.7, '의원': 0.4, '이용·미용': 0.5, '주점': 0.2, '일반 숙박': 0.2,
        '초등학교': 0.2, '중학교': 0.15, '고등학교': 0.25,
    },
    '오후': {
        '한식': 0.7, '식료품 소매': 0.6, '입시·교과학원': 0.6, '카페': 0.7, '주점': 0.4, '일반 숙박': 0.3,
        '초등학교': 0.1, '중학교': 0.1, '고등학교': 0.2,
    },
    '야간': {
        '주점': 1.0, '한식': 0.6, '일반 숙박': 0.7, '모텔/여관': 0.8,
        '초등학교': 0.01, '중학교': 0.01, '고등학교': 0.05,
    },
    '심야': {
        '주점': 0.9, '일반 숙박': 1.0,
        '초등학교': 0.01, '중학교': 0.01, '고등학교': 0.01,
    }
}
default_weight = 0.4

# 가중치 세트 선택
if is_vacation:
    slot_weights = time_weights_vacation.get(slot, {})
elif is_weekend:
    slot_weights = time_weights_weekend.get(slot, {})
else:
    slot_weights = time_weights_weekday.get(slot, {})

# ----------------------------
# 발전소 좌표 및 MongoDB 설정
# ----------------------------
power_plants = {
    '고리': (35.321499, 129.291612),
    '월성': (35.713058, 129.475347),
    '한빛': (35.415534, 126.416692),
    '한울': (37.085932, 129.390857)
}
mapping_codes = {'고리': 'KR', '월성': 'WS', '한빛': 'YK', '한울': 'UJ'}
client = MongoClient('mongodb://localhost:27017')
db = client['Data']
col = db['NPP_weather']

# ----------------------------
# 대기 안정도 맵핑
# ----------------------------
korean_to_category = {
    '심한 불안정': 'A', '불안정': 'B', '약간 불안정': 'C', '중립': 'D',
    '약간 안정': 'E', '안정': 'F', '심한 안정': 'G'
}
stab_map = {'A': 0.2, 'B': 0.4, 'C': 0.6, 'D': 0.8, 'E': 1.0, 'F': 1.2, 'G': 1.5}

# ----------------------------
# 함수 정의
# ----------------------------
def calculate_distance(lat1, lon1, lat2, lon2):
    R = 6371.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    return R * c

def calculate_bearing(lat1, lon1, lat2, lon2):
    lat1r, lat2r = math.radians(lat1), math.radians(lat2)
    dlr = math.radians(lon2 - lon1)
    x = math.sin(dlr) * math.cos(lat2r)
    y = math.cos(lat1r) * math.sin(lat2r) - math.sin(lat1r) * math.cos(lat2r) * math.cos(dlr)
    bearing = math.degrees(math.atan2(x, y))
    return (bearing + 360) % 360

def adjust_wind_direction(wd):
    return (wd + 180) % 360

def calculate_wind_risk(wd, ws, bearing, stability_weight, dist_km, alpha=0.05):
    adj = adjust_wind_direction(wd)
    rel = abs(adj - bearing)
    if rel > 180:
        rel = 360 - rel
    rel_rad = math.radians(rel)
    dist_w = 1 / (1 + alpha * dist_km)
    dil = 1 / stability_weight
    risk = ws * math.cos(rel_rad) * dist_w * dil
    return max(risk, 0)

def generate_sector(lat, lon, bearing, width, radius_km=100, points=30):
    start = bearing - width / 2
    angs = [start + i * (width / points) for i in range(points + 1)]
    coords = [(lat, lon)]
    for ang in angs:
        dest = geopy.distance.distance(kilometers=radius_km).destination((lat, lon), ang)
        coords.append((dest.latitude, dest.longitude))
    return coords

def get_angle_width(stability_weight):
    min_a, max_a = 30, 60
    w = max_a - (stability_weight - 0.2) * (max_a - min_a) / (1.5 - 0.2)
    return max(min_a, min(w, max_a))

def get_sector_style(_):
    return {'fillColor': 'orange', 'color': 'orange', 'weight': 2, 'fillOpacity': 0.4}

def fetch_weather(plant):
    code = mapping_codes.get(plant)
    if code is None:
        raise KeyError(f"mapping_codes에 '{plant}' 키가 없습니다.")
    doc = col.find_one({'genName': code}, sort=[('time', -1)])
    if not doc:
        raise ValueError(f"genName='{code}'인 문서를 찾을 수 없습니다. (현재 컬렉션 genName: {col.distinct('genName')})")
    wd = doc.get('winddirection') or doc.get('wind_direction')
    ws = doc.get('windspeed') or doc.get('wind_speed')
    stability_str = doc.get('stability', '')
    if wd is None or ws is None:
        raise ValueError(f"불완전한 기상 데이터: {doc}")
    category = korean_to_category.get(stability_str, 'D')
    weight = stab_map[category]
    print(f"Fetched weather for {plant}: wind={wd}°/{ws}m/s, stability='{stability_str}' → {category}({weight})")
    return {'wind_direction': wd, 'wind_speed': ws, 'stability_category': category, 'stability_weight': weight}

# ----------------------------
# 출력 디렉토리 생성
# ----------------------------
out_map = r'E:\논문\On going\auto\map'
out_cap = os.path.join(out_map, '캡쳐')
out_score = r'E:\논문\On going\auto\점수'
for d in [out_map, out_cap, out_score]:
    os.makedirs(d, exist_ok=True)

# ----------------------------
# 파일 경로 설정
# ----------------------------
regions = {
    '부산광역시':      r'E:\논문\On going\auto\hangjeongdong_부산광역시.geojson',
    '울산광역시':      r'E:\논문\On going\auto\hangjeongdong_울산광역시.geojson',
    '경상북도':      r'E:\논문\On going\auto\hangjeongdong_경상북도.geojson',
    '전라남도':      r'E:\논문\On going\auto\hangjeongdong_전라남도.geojson',
    '전라북도':   r'E:\논문\On going\auto\hangjeongdong_전라북도.geojson',
    '경상남도':   r'E:\논문\On going\auto\hangjeongdong_경상남도.geojson',
    '대구광역시': r'E:\논문\On going\auto\hangjeongdong_대구광역시.geojson',
    '광주광역시': r'E:\논문\On going\auto\hangjeongdong_광주광역시.geojson',
    '강원특별자치도': r'E:\논문\On going\auto\hangjeongdong_강원도.geojson',
}
pop_path = r'E:\논문\On going\auto\population2.xlsx'
shel_path = r'E:\논문\On going\auto\shelter.xlsx'
poi_path = r'E:\논문\On going\auto\poi_data.csv'

# ----------------------------
# 데이터 로드 및 병합
# ----------------------------
def read_geojson(p):
    try:
        g = gpd.read_file(p)
        print(f"Loaded {os.path.basename(p)}")
        return g
    except Exception as e:
        print(f"Error reading {p}: {e}")
        exit(1)
gdfs = [read_geojson(path) for path in regions.values()]
gdf = pd.concat(gdfs, ignore_index=True)
print("Merged regions.")

def read_xl(path, desc):
    try:
        df = pd.read_excel(path)
        print(f"Loaded {desc}")
        return df
    except Exception as e:
        print(f"Error reading {desc}: {e}")
        exit(1)
pop_df = read_xl(pop_path, 'population')
shel_df = read_xl(shel_path, 'shelter')

# 인구 병합
sido_map = {
    '부산광역시': '부산광역시', '울산광역시': '울산광역시', '대구광역시': '대구광역시', '광주광역시': '광주광역시',
    '전라남도': '전라남도', '전라북도': '전라북도', '경상남도': '경상남도', '경상북도': '경상북도', '강원특별자치도': '강원도'
}
pop_df['sido_full'] = pop_df['광역지자체'].map(sido_map)
pop_df['adm_nm_full'] = pop_df['sido_full'] + ' ' + pop_df['행정구역'] + ' ' + pop_df['adm_cd']
gdf = gdf.merge(pop_df[['adm_nm_full','population']], left_on='adm_nm', right_on='adm_nm_full', how='left').drop(columns=['adm_nm_full'])
missing = gdf[gdf['population'].isna()]
if not missing.empty:
    print("인구 병합 실패한 행정동 예시:", missing['adm_nm'].unique()[:10])
else:
    print("모든 행정동에 population이 정상 채워졌습니다!")

# Shelter(구호소) 병합
proj = gdf.to_crs('EPSG:5179')
proj['centroid'] = proj.geometry.centroid
gdf['centroid_lat'] = proj['centroid'].to_crs('EPSG:4326').y
gdf['centroid_lon'] = proj['centroid'].to_crs('EPSG:4326').x
sg = gpd.GeoDataFrame(shel_df, geometry=gpd.points_from_xy(shel_df.longitude, shel_df.latitude), crs='EPSG:4326')
sg = gpd.sjoin(sg, gdf[['adm_nm', 'geometry']], how='left', predicate='within')
cap_sum = sg.groupby('adm_nm')['capacity'].sum().reset_index().rename(columns={'capacity': 'capacity_sum'})
gdf = gdf.merge(cap_sum, on='adm_nm', how='left').fillna({'capacity_sum': 0})

# adm_nm 분리(시도/시군구/행정동)
gdf[['시도명','시군구명','행정동명']] = gdf['adm_nm'].str.split(' ', n=2, expand=True)

# ----------------------------
# POI(유동인구) 및 유동인구지수 계산
# ----------------------------
poi_df = pd.read_csv(poi_path, encoding='cp949')
poi_df['time_weight'] = poi_df['상권업종중분류명'].map(slot_weights).fillna(default_weight)
poi_grouped = (
    poi_df
    .groupby(['시도명','시군구명','행정동명'])['time_weight']
    .sum()
    .reset_index(name='poi_weighted')
)
gdf = (
    gdf
    .merge(poi_grouped,
           on=['시도명','시군구명','행정동명'],
           how='left')
    .fillna({'poi_weighted': 0})
)
gdf['commercial_index'] = MinMaxScaler().fit_transform(gdf[['poi_weighted']])
alpha = 0.5
gdf['dynamic_population'] = gdf['population'] * (1 + alpha * gdf['commercial_index'])

# ----------------------------
# 발전소/기상 선택 (사용자 입력)
# ----------------------------
plants = list(power_plants.keys())
print("사고 발전소를 선택하세요:")
for i, name in enumerate(plants, 1):
    print(f"{i}. {name}")
while True:
    try:
        idx = int(input(f"번호를 입력하세요 (1-{len(plants)}): ").strip())
        if 1 <= idx <= len(plants):
            selected = plants[idx - 1]
            break
        else:
            print(f"1부터 {len(plants)} 사이의 번호를 입력하세요.")
    except ValueError:
        print("유효한 숫자를 입력하세요.")
plant_lat, plant_lon = power_plants[selected]
wind_parameters = fetch_weather(selected)

# ----------------------------
# TOPSIS 분석 (유동인구지수 포함)
# ----------------------------
gdf['distance_to_nearest_plant'] = gdf.apply(
    lambda r: calculate_distance(plant_lat, plant_lon, r['centroid_lat'], r['centroid_lon']),
    axis=1
)
opt_km = 60
def triangular_distance_score(d, opt=opt_km):
    return d if d <= opt else max(0, 2 * opt - d)
gdf['distance_score'] = gdf['distance_to_nearest_plant'].apply(triangular_distance_score)
gdf = gdf[gdf['distance_to_nearest_plant'] <= 2 * opt_km].reset_index(drop=True)

gdf['SC_dynamic%'] = gdf.apply(
    lambda r: (r['capacity_sum'] / r['dynamic_population'] * 100)
              if r['dynamic_population'] > 0 else 0,
    axis=1
)
gdf['abs_capacity'] = gdf['capacity_sum']
gdf['bearing']   = gdf.apply(
    lambda r: calculate_bearing(plant_lat, plant_lon, r['centroid_lat'], r['centroid_lon']), axis=1
)
gdf['wind_risk'] = gdf.apply(
    lambda r: calculate_wind_risk(
        wind_parameters['wind_direction'],
        wind_parameters['wind_speed'],
        r['bearing'],
        wind_parameters['stability_weight'],
        r['distance_to_nearest_plant'],
        alpha=0.025
    ),
    axis=1
)
cap_df = gdf[['SC_dynamic%', 'abs_capacity']].rename(columns={'abs_capacity': 'abs_capacity'}).fillna(0)
cap_std = StandardScaler().fit_transform(cap_df)
gdf['cap_pc1'] = PCA(n_components=1).fit_transform(cap_std).flatten()

# === TOPSIS: 유동인구지수 포함 ===
criteria = gdf[['distance_score', 'cap_pc1', 'wind_risk', 'commercial_index']].fillna(0)
weights = pd.Series({
    'distance_score': 0.30,
    'cap_pc1':        0.20,
    'wind_risk':      0.30,
    'commercial_index': 0.20
})
norm_vals = MinMaxScaler().fit_transform(criteria)
norm_df   = pd.DataFrame(norm_vals, columns=criteria.columns, index=criteria.index)
weighted_df = norm_df.mul(weights, axis=1)
ideal_best = {
    'distance_score': weighted_df['distance_score'].max(),
    'cap_pc1':        weighted_df['cap_pc1'].max(),
    'wind_risk':      weighted_df['wind_risk'].min(),
    'commercial_index': weighted_df['commercial_index'].min()
}
ideal_worst = {
    'distance_score': weighted_df['distance_score'].min(),
    'cap_pc1':        weighted_df['cap_pc1'].min(),
    'wind_risk':      weighted_df['wind_risk'].max(),
    'commercial_index': weighted_df['commercial_index'].max()
}
def compute_topsis(idx):
    w = weighted_df.loc[idx]
    d_best  = math.sqrt(sum((w[c] - ideal_best[c])**2 for c in weighted_df.columns))
    d_worst = math.sqrt(sum((w[c] - ideal_worst[c])**2 for c in weighted_df.columns))
    return d_worst / (d_best + d_worst) if (d_best + d_worst) else 0
gdf['topsis_score'] = [compute_topsis(i) for i in norm_df.index]

# 결과 저장
gdf.to_excel(os.path.join(out_score, 'topsis_result.xlsx'), index=False)
print(f"TOPSIS 결과 저장됨: {os.path.join(out_score, 'topsis_result.xlsx')}")

# ----------------------------
# 지도 시각화 및 저장
# ----------------------------
m = folium.Map(location=[36.0, 127.5], zoom_start=8, tiles=None)
folium.TileLayer(
    tiles='https://{s}.basemaps.cartocdn.com/light_all/{z}/{x}/{y}{r}.png',
    name='CartoDB Positron',
    attr='Map tiles by CartoDB, CC BY 3.0 — Map data © OpenStreetMap contributors'
).add_to(m)
MarkerCluster(name='구호소').add_to(m)
power_layer = folium.FeatureGroup(name='발전소').add_to(m)
angle = (adjust_wind_direction(wind_parameters['wind_direction']) - 90) % 360
arrow_html = f"""<div style="transform:rotate({angle}deg);font-size:36px;color:blue"><i class="fa fa-arrow-circle-right"></i></div>"""
folium.Marker(location=[plant_lat, plant_lon], icon=DivIcon(icon_size=(50, 50), icon_anchor=(25, 25), html=arrow_html), popup=f"Wind: {wind_parameters['wind_direction']}°").add_to(power_layer)
folium.Marker(location=[plant_lat, plant_lon], icon=folium.Icon(color='red', icon='industry'), popup=f"{selected} 발전소").add_to(power_layer)
width = get_angle_width(wind_parameters['stability_weight'])
bearing = adjust_wind_direction(wind_parameters['wind_direction'])
coords = generate_sector(plant_lat, plant_lon, bearing, width, radius_km=100)
folium.Polygon(locations=coords, **get_sector_style(None), popup=f"{selected} 발전소 풍향 섹터").add_to(m)

# GeoJSON (TOPSIS)
valid = gdf['topsis_score'].dropna()
if not valid.empty:
    topsis_cm = cm.LinearColormap(['blue', 'white', 'red'], vmin=0, vmax=1)
    topsis_cm.caption = 'TOPSIS Score'
    folium.GeoJson(
        gdf,
        style_function=lambda feat: {
            'fillColor': topsis_cm(feat['properties']['topsis_score']),
            'color': 'black', 'weight': 1, 'fillOpacity': 0.7
        },
        tooltip=GeoJsonTooltip(
            fields=[
                'adm_nm',
                'population',
                'distance_score',
                'cap_pc1',
                'wind_risk',
                'commercial_index',
                'topsis_score'
            ],
            aliases=[
                '행정동:',
                '인구수:',
                '거리 점수:',
                '통합 수용능력(PC1):',
                '풍위험:',
                '유동인구지수:',
                'TOPSIS Score:'
            ],
            localize=True
        ),
        name='TOPSIS'
    ).add_to(m)
    topsis_cm.add_to(m)

# Top 5 마커 표시
topN = 5
topN_sites = gdf.nlargest(topN, 'topsis_score')

# 2) 상세 정보 팝업
for idx, row in topN_sites.iterrows():
    popup_html = f"""
    <b>{row['adm_nm']}</b><br>
    인구수: {int(row['population']):,}<br>
    수용인원: {int(row['capacity_sum']):,}<br>
    동적인구: {int(row['dynamic_population']):,}<br>
    TOPSIS 점수: {row['topsis_score']:.3f}<br>
    <a href="https://www.google.com/maps?q={row['centroid_lat']},{row['centroid_lon']}" target="_blank">구글맵에서 보기</a>
    """
    folium.Marker(
        location=[row['centroid_lat'], row['centroid_lon']],
        popup=folium.Popup(popup_html, max_width=350),
        icon=folium.Icon(color='darkred', icon='hospital')
    ).add_to(m)
for r in [10000, 30000, 60000, 100000]:
    folium.Circle(
        location=[plant_lat, plant_lon],
        radius=r,
        color='black', fill=False, dash_array='5', weight=2,
        popup=f"{r // 1000}km 반경"
    ).add_to(m)
folium.LayerControl().add_to(m)
html_path = os.path.join(out_map, 'NPP_topsis_map.html')
m.save(html_path)
print(f"Saved map HTML: {html_path}")

# 이미지 저장 (필요시)
def save_map_as_image(html_path, output_image_path, image_format='png', scale=2):
    chrome_options = webdriver.ChromeOptions()
    for arg in ["--headless", "--hide-scrollbars", "--no-sandbox", "--disable-dev-shm-usage",
                "--window-size=1440,1200"]:
        chrome_options.add_argument(arg)
    service = Service(ChromeDriverManager().install())
    driver = webdriver.Chrome(service=service, options=chrome_options)
    try:
        driver.get(f"file:///{html_path}")
        time.sleep(5)
        screenshot = driver.get_screenshot_as_png()
        img = Image.open(BytesIO(screenshot))
        w, h = img.size
        resampling = Image.Resampling.LANCZOS if hasattr(Image, 'Resampling') else Image.LANCZOS
        img = img.resize((w * scale, h * scale), resampling)
        img.save(output_image_path, format=image_format.upper())
        print(f"Saved map image: {output_image_path}")
    except Exception as e:
        print(f"Error saving map image: {e}")
    finally:
        driver.quit()

png_path = os.path.join(out_cap, 'NPP_topsis_map.png')
tiff_path = os.path.join(out_cap, 'NPP_topsis_map.tiff')
save_map_as_image(html_path, png_path, image_format='png', scale=3)
try:
    Image.open(png_path).save(tiff_path, format='TIFF')
    print(f"Converted PNG to TIFF: {tiff_path}")
except Exception as e:
    print(f"Error converting to TIFF: {e}")