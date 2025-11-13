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

# ----------------------------
# 설정 및 데이터 로드
# ----------------------------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

REGIONS = {
    '부산광역시':      os.path.join(BASE_DIR, 'data', 'geojson', 'hangjeongdong_부산광역시.geojson'),
    '울산광역시':      os.path.join(BASE_DIR, 'data', 'geojson', 'hangjeongdong_울산광역시.geojson'),
    '경상북도':        os.path.join(BASE_DIR, 'data', 'geojson', 'hangjeongdong_경상북도.geojson'),
    '전라남도':        os.path.join(BASE_DIR, 'data', 'geojson', 'hangjeongdong_전라남도.geojson'),
    '전라북도':        os.path.join(BASE_DIR, 'data', 'geojson', 'hangjeongdong_전라북도.geojson'),
    '경상남도':        os.path.join(BASE_DIR, 'data', 'geojson', 'hangjeongdong_경상남도.geojson'),
    '대구광역시':      os.path.join(BASE_DIR, 'data', 'geojson', 'hangjeongdong_대구광역시.geojson'),
    '광주광역시':      os.path.join(BASE_DIR, 'data', 'geojson', 'hangjeongdong_광주광역시.geojson'),
    '강원특별자치도':  os.path.join(BASE_DIR, 'data', 'geojson', 'hangjeongdong_강원도.geojson'),
}
POP_PATH  = os.path.join(BASE_DIR, 'data', 'population2.xlsx')
SHEL_PATH = os.path.join(BASE_DIR, 'data', 'shelter.xlsx')

# ----------------------------
# 대기 안정도 매핑
# (Mongo에 들어오는 한글 표현이 예전/새 버전 섞여 있어도 대응)
# ----------------------------
korean_to_category = {
    '매우 불안정': 'A',
    '심한 불안정': 'A',
    '불안정':     'B',
    '약간 불안정': 'C',
    '중립':       'D',
    '약간 안정':   'E',
    '안정':       'F',
    '심한 안정':   'G',
}

stab_map = {
    'A': 0.2,
    'B': 0.4,
    'C': 0.6,
    'D': 0.8,
    'E': 1.0,
    'F': 1.2,
    'G': 1.5,
}

# ----------------------------
# 발전소 좌표 및 MongoDB 설정
# ----------------------------
power_plants = {
    '고리': (35.321499, 129.291612),
    '월성': (35.713058, 129.475347),
    '한빛': (35.415534, 126.416692),
    '한울': (37.085932, 129.390857),
}

mapping_codes = {
    '고리': 'KR',
    '월성': 'WS',
    '한빛': 'YK',
    '한울': 'UJ',
}

# 로깅 설정
if not logging.root.handlers:
    logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# Railway / 로컬 공통: 환경변수 MONGO_URI 사용
mongo_uri = os.getenv("MONGO_URI")
if not mongo_uri:
    raise ValueError("MONGO_URI environment variable not set in Railway! Check your service variables.")

mongo_uri = mongo_uri.strip().lstrip('=').strip()
logging.info(f"DEBUG: Retrieved MONGO_URI (after clean): '{mongo_uri}' (Length: {len(mongo_uri)})")

client = MongoClient(mongo_uri)
db = client['Data']
col = db['NPP_weather']

# ----------------------------
# GeoDataFrame 로드 및 전처리
# ----------------------------
def _load_geodata():
    gdfs = []
    for region_name, path in REGIONS.items():
        try:
            gdf_region = gpd.read_file(path)
            gdfs.append(gdf_region)
        except Exception as e:
            logging.error(f"Error loading geodata for {region_name} from {path}: {e}")
            # 한 지역이 실패해도 나머지는 계속 진행
            pass

    if not gdfs:
        raise ValueError("No GeoDataFrames could be loaded. Check file paths and existence.")

    gdf = pd.concat(gdfs, ignore_index=True)

    # 인구 병합
    pop_df = pd.read_excel(POP_PATH)
    pop_df['sido_full'] = pop_df['광역지자체'].map({k: k for k in REGIONS})
    pop_df['adm_nm_full'] = pop_df['sido_full'] + ' ' + pop_df['행정구역'] + ' ' + pop_df['adm_cd']
    gdf = gdf.merge(
        pop_df[['adm_nm_full', 'population']],
        left_on='adm_nm',
        right_on='adm_nm_full',
        how='left'
    )
    gdf.drop(columns=['adm_nm_full'], inplace=True)

    # 구호소 병합
    shel_df = pd.read_excel(SHEL_PATH)
    sg = gpd.GeoDataFrame(
        shel_df,
        geometry=gpd.points_from_xy(shel_df.longitude, shel_df.latitude),
        crs='EPSG:4326'
    )
    sg = gpd.sjoin(sg, gdf[['adm_nm', 'geometry']], how='left', predicate='within')
    cap_sum = (
        sg.groupby('adm_nm')['capacity']
        .sum()
        .reset_index()
        .rename(columns={'capacity': 'capacity_sum'})
    )
    gdf = gdf.merge(cap_sum, on='adm_nm', how='left').fillna({'capacity_sum': 0})

    # 중심점(위경도)
    proj = gdf.to_crs('EPSG:5179')
    proj['centroid'] = proj.geometry.centroid
    gdf['centroid_lat'] = proj['centroid'].to_crs('EPSG:4326').y
    gdf['centroid_lon'] = proj['centroid'].to_crs('EPSG:4326').x

    return gdf

_GDF = _load_geodata()

# ----------------------------
# 기상 데이터 조회
# ----------------------------
def fetch_weather(plant):
    code = mapping_codes.get(plant)
    if not code:
        raise KeyError(f"Unknown plant '{plant}'")

    doc = col.find_one({'genName': code}, sort=[('time', -1)])
    if not doc:
        raise ValueError(f"No data for '{plant}' (genName={code})")

    wd = doc.get('winddirection') or doc.get('wind_direction')
    ws = doc.get('windspeed') or doc.get('wind_speed')
    stability_str = doc.get('stability', '') or ''

    if wd is None or ws is None:
        raise ValueError(f"Incomplete weather doc: {doc}")

    cat = korean_to_category.get(stability_str, 'D')
    sw = stab_map.get(cat, stab_map['D'])

    logging.info(
        f"Weather for {plant}: wind={wd}°/{ws}m/s, stability='{stability_str}' → {cat}({sw})"
    )
    return float(wd), float(ws), float(sw)

# ----------------------------
# 거리/방위/풍위험 유틸
# ----------------------------
def _distance(lat1, lon1, lat2, lon2):
    R = 6371.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dl / 2) ** 2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))

def _bearing(lat1, lon1, lat2, lon2):
    lat1r, lat2r = math.radians(lat1), math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    x = math.sin(dl) * math.cos(lat2r)
    y = math.cos(lat1r) * math.sin(lat2r) - math.sin(lat1r) * math.cos(lat2r) * math.cos(dl)
    return (math.degrees(math.atan2(x, y)) + 360) % 360

def _adjust_wind_direction(wd):
    # 풍향 → 플룸 진행 방향(역풍)으로 180도 회전
    return (wd + 180) % 360

def _wind_risk(wd, ws, bearing, stability_weight, dist_km, alpha=0.025):
    """
    ws: 풍속 (m/s) – 클수록 희석, 하지만 여기서는 그대로 사용
    stability_weight: stab_map에서 온 값 (안정할수록 큰 값)
    dist_km: 발전소~구역 거리 (km)
    alpha: 거리 감쇠 계수
    """
    adj = _adjust_wind_direction(wd)
    rel = abs(adj - bearing)
    if rel > 180:
        rel = 360 - rel
    rel_rad = math.radians(rel)

    # 각도 성분 (역풍은 0으로 클리핑)
    ang = math.cos(rel_rad)
    if ang < 0:
        ang = 0.0

    # 거리 감쇠
    dist_w = 1.0 / (1.0 + alpha * dist_km)

    # 안정도 역비례
    dil = 1.0 / stability_weight if stability_weight > 0 else 0.0

    risk = ws * ang * dist_w * dil
    return max(risk, 0.0)

# 삼각형 거리 점수
OPT_KM = 60.0
MAX_KM = OPT_KM * 2.0

def _triangular_distance_score(d, opt=OPT_KM):
    return d if d <= opt else max(0.0, 2 * opt - d)

def generate_sector(lat, lon, bearing, width, radius_km=100, points=50):
    """부채꼴 좌표 생성(풍향 섹터)"""
    start = bearing - width / 2
    angs = [start + i * (width / points) for i in range(points + 1)]
    coords = [(lat, lon)]
    for ang in angs:
        dest = geopy.distance.distance(kilometers=radius_km).destination((lat, lon), ang)
        coords.append((dest.latitude, dest.longitude))
    return coords

def get_angle_width(stability_weight):
    """안정도 가중치 → 부채꼴 폭(°) 환산 (안정할수록 조금 좁게)"""
    min_w, max_w = 30, 60
    # stab_map 범위(0.2~1.5)에 맞춘 선형 스케일
    w = max_w - (stability_weight - 0.2) * (max_w - min_w) / (1.5 - 0.2)
    return max(min_w, min(w, max_w))

# ----------------------------
# TOPSIS 맵 HTML 생성
# ----------------------------
def generate_topsis_map_html(plant):
    """
    plant: '고리','월성','한빛','한울'
    반환: folium.Map._repr_html_() 형식의 HTML 문자열
    """
    if plant not in power_plants:
        raise KeyError(f"Unsupported plant '{plant}'")

    lat, lon = power_plants[plant]
    wd, ws, sw = fetch_weather(plant)

    # 지도 베이스
    m = folium.Map(location=[36.0, 127.5], zoom_start=8, tiles='cartodbpositron')
    MarkerCluster(name='구호소').add_to(m)

    # 발전소 방향 화살표
    bearing = _adjust_wind_direction(wd)
    angle_css = bearing  # CSS 회전 각도

    arrow_html = f"""
      <div style="
        display: inline-block;
        transform-origin: center center;
        transform: rotate({angle_css}deg) translate(-50%, -50%);
        font-size: 36px;
        color: blue;
        text-shadow: 1px 1px 2px rgba(0,0,0,0.5);
      ">
        <i class="fa fa-arrow-up"></i>
      </div>
    """

    folium.Marker(
        [lat, lon],
        icon=DivIcon(
            html=arrow_html,
            icon_size=(50, 50),
            icon_anchor=(25, 25)
        ),
        popup=f"{plant} 발전소 풍향: {wd}°",
        z_index_offset=1000
    ).add_to(m)

    # Plume 섹터
    width = get_angle_width(sw)
    coords = generate_sector(lat, lon, bearing, width, radius_km=OPT_KM)
    folium.Polygon(
        locations=coords,
        color='red',
        weight=2,
        fill=True,
        fill_color='red',
        fill_opacity=0.4,
        popup=f"풍향: {wd}° / 안정도 가중치: {sw}"
    ).add_to(m)

    # ---------- TOPSIS 계산 ----------
    df = _GDF.copy()

    # 1) 거리
    df['distance_to_nearest_plant'] = df.apply(
        lambda r: _distance(lat, lon, r['centroid_lat'], r['centroid_lon']),
        axis=1
    )

    # 2) 최대 거리 필터 (2*OPT_KM)
    df = df[df['distance_to_nearest_plant'] <= MAX_KM].reset_index(drop=True)

    # 3) 거리 점수(삼각형)
    df['distance_score'] = df['distance_to_nearest_plant'].apply(_triangular_distance_score)

    # 4) 인구 대비 수용률 및 절대 수용인원
    df['SC_percent'] = df.apply(
        lambda r: (r['capacity_sum'] / r['population'] * 100.0) if r.get('population', 0) > 0 else 0.0,
        axis=1
    )
    df['abs_capacity'] = df['capacity_sum']

    # 5) 풍위험
    df['bearing'] = df.apply(
        lambda r: _bearing(lat, lon, r['centroid_lat'], r['centroid_lon']),
        axis=1
    )
    df['wind_risk'] = df.apply(
        lambda r: _wind_risk(
            wd, ws, r['bearing'], sw, r['distance_to_nearest_plant'], alpha=0.025
        ),
        axis=1
    )

    # 6) PCA로 통합 수용능력(cap_pc1)
    cap_df = df[['SC_percent', 'abs_capacity']].fillna(0)
    cap_std = StandardScaler().fit_transform(cap_df)
    df['cap_pc1'] = PCA(n_components=1).fit_transform(cap_std).flatten()

    # 7) TOPSIS 기준: distance_score, cap_pc1, wind_risk
    criteria = df[['distance_score', 'cap_pc1', 'wind_risk']].fillna(0)
    norm_vals = MinMaxScaler().fit_transform(criteria)
    norm_df = pd.DataFrame(norm_vals, columns=criteria.columns, index=criteria.index)

    # 가중치
    weights = pd.Series({
        'distance_score': 0.34,
        'cap_pc1':        0.33,
        'wind_risk':      0.33,
    })
    weighted_df = norm_df.mul(weights, axis=1)

    ideal_best = {
        'distance_score': weighted_df['distance_score'].max(),
        'cap_pc1':        weighted_df['cap_pc1'].max(),
        'wind_risk':      weighted_df['wind_risk'].min(),  # 최소화
    }
    ideal_worst = {
        'distance_score': weighted_df['distance_score'].min(),
        'cap_pc1':        weighted_df['cap_pc1'].min(),
        'wind_risk':      weighted_df['wind_risk'].max(),
    }

    def _compute_topsis(idx):
        wrow = weighted_df.loc[idx]
        d_best = math.sqrt(sum((wrow[c] - ideal_best[c]) ** 2 for c in weighted_df.columns))
        d_worst = math.sqrt(sum((wrow[c] - ideal_worst[c]) ** 2 for c in weighted_df.columns))
        return d_worst / (d_best + d_worst) if (d_best + d_worst) else 0.0

    df['topsis'] = [_compute_topsis(i) for i in weighted_df.index]

    # ---------- 지도 시각화 ----------
    cm_top = cm.LinearColormap(['#313695', '#ffffff', '#A50026'],
                               index=[0, 0.5, 1],
                               vmin=0, vmax=1,
                               caption='TOPSIS Score')
    folium.GeoJson(
        df,
        style_function=lambda feat: {
            'fillColor': cm_top(feat['properties']['topsis']),
            'color': 'black',
            'weight': 1,
            'fillOpacity': 0.9,
        },
        tooltip=GeoJsonTooltip(
            fields=['adm_nm', 'population', 'distance_score', 'cap_pc1', 'wind_risk', 'topsis'],
            aliases=['행정동', '인구', '거리 점수', '통합 수용능력(PC1)', '풍위험', 'TOPSIS'],
            localize=True,
        )
    ).add_to(m)
    cm_top.add_to(m)

    # TOP5 마커
    for _, row in df.nlargest(5, 'topsis').iterrows():
        folium.Marker(
            [row['centroid_lat'], row['centroid_lon']],
            popup=f"{row['adm_nm']} ({row['topsis']:.3f})",
            icon=folium.Icon(color='darkred', icon='hospital')
        ).add_to(m)

    return m._repr_html_()

# ----------------------------
# TOP5 구호소 계산 함수
# ----------------------------
def compute_top5_for(plant):
    """
    plant: '고리','월성','한빛','한울'
    반환: 최상위 5개 행정동 정보 리스트
    """
    if plant not in power_plants:
        raise KeyError(f"Unsupported plant '{plant}'")

    lat, lon = power_plants[plant]
    wd, ws, sw = fetch_weather(plant)

    df = _GDF.copy()
    df['distance_to_nearest_plant'] = df.apply(
        lambda r: _distance(lat, lon, r['centroid_lat'], r['centroid_lon']),
        axis=1
    )
    df = df[df['distance_to_nearest_plant'] <= MAX_KM].reset_index(drop=True)
    df['distance_score'] = df['distance_to_nearest_plant'].apply(_triangular_distance_score)

    df['SC_percent'] = df.apply(
        lambda r: (r['capacity_sum'] / r['population'] * 100.0) if r.get('population', 0) > 0 else 0.0,
        axis=1
    )
    df['abs_capacity'] = df['capacity_sum']

    df['bearing'] = df.apply(
        lambda r: _bearing(lat, lon, r['centroid_lat'], r['centroid_lon']),
        axis=1
    )
    df['wind_risk'] = df.apply(
        lambda r: _wind_risk(
            wd, ws, r['bearing'], sw, r['distance_to_nearest_plant'], alpha=0.025
        ),
        axis=1
    )

    cap_df = df[['SC_percent', 'abs_capacity']].fillna(0)
    cap_std = StandardScaler().fit_transform(cap_df)
    df['cap_pc1'] = PCA(n_components=1).fit_transform(cap_std).flatten()

    criteria = df[['distance_score', 'cap_pc1', 'wind_risk']].fillna(0)
    norm_vals = MinMaxScaler().fit_transform(criteria)
    norm_df = pd.DataFrame(norm_vals, columns=criteria.columns, index=criteria.index)

    weights = pd.Series({
        'distance_score': 0.34,
        'cap_pc1':        0.33,
        'wind_risk':      0.33,
    })
    weighted_df = norm_df.mul(weights, axis=1)

    ideal_best = {
        'distance_score': weighted_df['distance_score'].max(),
        'cap_pc1':        weighted_df['cap_pc1'].max(),
        'wind_risk':      weighted_df['wind_risk'].min(),
    }
    ideal_worst = {
        'distance_score': weighted_df['distance_score'].min(),
        'cap_pc1':        weighted_df['cap_pc1'].min(),
        'wind_risk':      weighted_df['wind_risk'].max(),
    }

    def _compute_topsis(idx):
        wrow = weighted_df.loc[idx]
        d_best = math.sqrt(sum((wrow[c] - ideal_best[c]) ** 2 for c in weighted_df.columns))
        d_worst = math.sqrt(sum((wrow[c] - ideal_worst[c]) ** 2 for c in weighted_df.columns))
        return d_worst / (d_best + d_worst) if (d_best + d_worst) else 0.0

    df['topsis_score'] = [_compute_topsis(i) for i in weighted_df.index]

    top5 = df.nlargest(5, 'topsis_score')
    return [
        {
            'name': row['adm_nm'],
            'address': row['adm_nm'],
            'capacity': int(row['capacity_sum']),
            'topsis_score': round(float(row['topsis_score']), 3),
            'lat': float(row['centroid_lat']),
            'lon': float(row['centroid_lon']),
        }
        for _, row in top5.iterrows()
    ]
