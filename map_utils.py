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

# ----------------------------
# 설정 및 데이터 로드
# ----------------------------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# 발전소 좌표 및 MongoDB 설정
power_plants = {
    '고리': (35.321499, 129.291612),
    '월성': (35.713058, 129.475347),
    '한빛': (35.415534, 126.416692),
    '한울': (37.085932, 129.390857)
}
mapping_codes = {
    '고리': 'KR',
    '월성': 'WS',
    '한빛': 'YK',
    '한울': 'UJ'
}
client = MongoClient('mongodb://localhost:27017')
db = client['Data']
col = db['NPP_weather']

# 데이터 파일 경로
REGIONS = {
    '부산광역시':  os.path.join(BASE_DIR, 'data', 'geojson', 'hangjeongdong_부산광역시.geojson'),
    '울산광역시':  os.path.join(BASE_DIR, 'data', 'geojson', 'hangjeongdong_울산광역시.geojson'),
    '경상북도':    os.path.join(BASE_DIR, 'data', 'geojson', 'hangjeongdong_경상북도.geojson'),
    '전라남도':    os.path.join(BASE_DIR, 'data', 'geojson', 'hangjeongdong_전라남도.geojson'),
    '전라북도':    os.path.join(BASE_DIR, 'data', 'geojson', 'hangjeongdong_전라북도.geojson'),
    '경상남도':    os.path.join(BASE_DIR, 'data', 'geojson', 'hangjeongdong_경상남도.geojson'),
    '대구광역시':  os.path.join(BASE_DIR, 'data', 'geojson', 'hangjeongdong_대구광역시.geojson'),
    '광주광역시':  os.path.join(BASE_DIR, 'data', 'geojson', 'hangjeongdong_광주광역시.geojson'),
    '강원특별자치도': os.path.join(BASE_DIR, 'data', 'geojson', 'hangjeongdong_강원도.geojson'),
}
POP_PATH  = os.path.join(BASE_DIR, 'data', 'population2.xlsx')
SHEL_PATH = os.path.join(BASE_DIR, 'data', 'shelter.xlsx')

# 안정도 카테고리 및 가중치
korean_to_category = {
    '심한 불안정':'A','불안정':'B','약간 불안정':'C','중립':'D',
    '약간 안정':'E','안정':'F','심한 안정':'G'
}
stab_map = {'A':0.2,'B':0.4,'C':0.6,'D':0.8,'E':1.0,'F':1.2,'G':1.5}

# ----------------------------
# GeoDataFrame 로드 및 전처리
# ----------------------------
def _load_geodata():
    gdfs = [gpd.read_file(p) for p in REGIONS.values()]
    gdf = pd.concat(gdfs, ignore_index=True)

    pop_df = pd.read_excel(POP_PATH)
    pop_df['sido_full'] = pop_df['광역지자체'].map({k:k for k in REGIONS})
    pop_df['adm_nm_full'] = pop_df['sido_full'] + ' ' + pop_df['행정구역'] + ' ' + pop_df['adm_cd']
    gdf = gdf.merge(pop_df[['adm_nm_full','population']],
                    left_on='adm_nm', right_on='adm_nm_full', how='left')
    gdf.drop(columns=['adm_nm_full'], inplace=True)

    shel_df = pd.read_excel(SHEL_PATH)
    sg = gpd.GeoDataFrame(shel_df,
        geometry=gpd.points_from_xy(shel_df.longitude, shel_df.latitude),
        crs='EPSG:4326')
    sg = gpd.sjoin(sg, gdf[['adm_nm','geometry']],
                   how='left', predicate='within')
    cap_sum = sg.groupby('adm_nm')['capacity'].sum().reset_index().rename(columns={'capacity':'capacity_sum'})
    gdf = gdf.merge(cap_sum, on='adm_nm', how='left').fillna({'capacity_sum':0})

    proj = gdf.to_crs('EPSG:5179')
    proj['centroid'] = proj.geometry.centroid
    gdf['centroid_lat'] = proj['centroid'].to_crs('EPSG:4326').y
    gdf['centroid_lon'] = proj['centroid'].to_crs('EPSG:4326').x

    return gdf

_GDF = _load_geodata()

# ----------------------------
# 기상 데이터 조회 및 유틸
# ----------------------------
def fetch_weather(plant):
    code = mapping_codes.get(plant)
    if not code: raise KeyError(f"Unknown plant '{plant}'")
    doc = col.find_one({'genName':code}, sort=[('time',-1)])
    if not doc: raise ValueError(f"No data for '{plant}'")
    wd = doc.get('winddirection') or doc.get('wind_direction')
    ws = doc.get('windspeed') or doc.get('wind_speed')
    sw = stab_map[korean_to_category.get(doc.get('stability',''), 'D')]
    return wd, ws, sw

def _distance(lat1,lon1,lat2,lon2):
    R=6371.0;phi1,phi2=math.radians(lat1),math.radians(lat2)
    dphi,dl=math.radians(lat2-lat1),math.radians(lon2-lon1)
    a=math.sin(dphi/2)**2+math.cos(phi1)*math.cos(phi2)*math.sin(dl/2)**2
    return R*2*math.atan2(math.sqrt(a),math.sqrt(1-a))

def _bearing(lat1,lon1,lat2,lon2):
    lat1r,lat2r=math.radians(lat1),math.radians(lat2)
    dl=math.radians(lon2-lon1)
    x,y=math.sin(dl)*math.cos(lat2r), math.cos(lat1r)*math.sin(lat2r)-math.sin(lat1r)*math.cos(lat2r)*math.cos(dl)
    return (math.degrees(math.atan2(x,y))+360)%360

def _wind_risk(wd,ws,bearing,sw,dist):
    adj=(wd+180)%360;rel=abs(adj-bearing)
    if rel>180: rel=360-rel
    return max(ws*math.cos(math.radians(rel))/(1+0.05*dist)*(1/sw),0)

def generate_sector(lat, lon, bearing, width, radius_km=100, points=50):
    """부채꼴 좌표 생성(풍향 섹터)"""
    start = bearing - width/2
    angs = [start + i*(width/points) for i in range(points+1)]
    coords = [(lat, lon)]
    for ang in angs:
        dest = geopy.distance.distance(kilometers=radius_km).destination((lat, lon), ang)
        coords.append((dest.latitude, dest.longitude))
    return coords

def get_angle_width(stability_weight):
    """안정도 가중치→부채꼴 폭(°) 환산"""
    min_w, max_w = 30, 60
    return max(min_w, min(max_w, max_w - (stability_weight-0.2)*(max_w-min_w)/(1.5-0.2)))

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
    # MongoDB에서 최신 풍향/풍속/안정도 가중치 가져오기
    wd, ws, sw = fetch_weather(plant)

    # 1) 지도 초기화 (CartoDB Positron 스타일)
    m = folium.Map(location=[36.0, 127.5], zoom_start=8, tiles='cartodbpositron')
    MarkerCluster(name='구호소').add_to(m)

    # 2) 발전소 아이콘 대신 풍향 화살표 추가
    bearing = (wd + 180) % 360
    angle_css = bearing

    arrow_html = f"""
      <div style="
        display: inline-block;
        transform-origin: center center;
        /* ① 회전 먼저 → ② 중앙 이동 */
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
        z_index_offset=1000  # 폴리곤 위에 표시
    ).add_to(m)

    # 3) sector 그리기
    width = get_angle_width(sw)
    coords = generate_sector(lat, lon, bearing, width)
    folium.Polygon(
        locations=coords,
        color='red',
        weight=2,
        fill=True,
        fill_color='red',
        fill_opacity=0.4,
        popup=f"풍향: {wd}° / 안정도 가중치: {sw}"
    ).add_to(m)


    # 4) TOPSIS 계산
    df = _GDF.copy()
    df['dist']       = df.apply(lambda r: _distance(lat, lon, r['centroid_lat'], r['centroid_lon']), axis=1)
    df = df[df['dist']<=120]
    df['dist_score'] = df['dist'].apply(lambda d: d if d<=60 else max(0,120-2*d))
    df['bearing']    = df.apply(lambda r: _bearing(lat, lon, r['centroid_lat'], r['centroid_lon']), axis=1)
    df['wind_risk']  = df.apply(lambda r: _wind_risk(wd, ws, r['bearing'], sw, r['dist']), axis=1)

    cap = df[['capacity_sum','population']].fillna(0)
    df['cap_pc1'] = PCA(n_components=1).fit_transform(StandardScaler().fit_transform(cap)).flatten()

    crit   = df[['dist_score','cap_pc1','wind_risk']].values
    norm   = MinMaxScaler().fit_transform(crit)
    w_norm = norm * [0.34,0.33,0.33]
    best   = [w_norm[:,0].max(), w_norm[:,1].max(), w_norm[:,2].min()]
    worst  = [w_norm[:,0].min(), w_norm[:,1].min(), w_norm[:,2].max()]
    df['topsis'] = [
        (math.sqrt(sum((row[i]-worst[i])**2 for i in range(3))) /
         (math.sqrt(sum((row[i]-best[i])**2 for i in range(3))) +
          math.sqrt(sum((row[i]-worst[i])**2 for i in range(3)))))
        if (sum((row[i]-best[i])**2 for i in range(3)) + sum((row[i]-worst[i])**2 for i in range(3))) > 0
        else 0
        for row in w_norm
    ]

    # 5) 컬러맵 & GeoJson
    cm_top = cm.LinearColormap(['#313695', 'white', '#a50026'], index=[0, 0.5, 1],
                               vmin=0, vmax=1, caption='TOPSIS Score')
    folium.GeoJson(
        df,
        style_function=lambda feat: {
            'fillColor': cm_top(feat['properties']['topsis']),
            'color': 'black', 'weight': 1, 'fillOpacity': 0.7
        },
        tooltip=GeoJsonTooltip(
            fields=['adm_nm','population','topsis'],
            aliases=['행정동','인구','TOPSIS'],
            localize=True
        )
    ).add_to(m)
    cm_top.add_to(m)

    # 6) TOP5 마커
    for _, row in df.nlargest(5,'topsis').iterrows():
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
    lat, lon = power_plants[plant]
    wd, ws, sw = fetch_weather(plant)
    df = _GDF.copy()
    df['dist']       = df.apply(lambda r: _distance(lat, lon, r['centroid_lat'], r['centroid_lon']), axis=1)
    df = df[df['dist']<=120]
    df['dist_score'] = df['dist'].apply(lambda d: d if d<=60 else max(0,120-2*d))
    df['bearing']    = df.apply(lambda r: _bearing(lat, lon, r['centroid_lat'], r['centroid_lon']), axis=1)
    df['wind_risk']  = df.apply(lambda r: _wind_risk(wd, ws, r['bearing'], sw, r['dist']), axis=1)
    cap = df[['capacity_sum','population']].fillna(0)
    df['cap_pc1'] = PCA(n_components=1).fit_transform(StandardScaler().fit_transform(cap)).flatten()

    crit   = df[['dist_score','cap_pc1','wind_risk']].values
    norm   = MinMaxScaler().fit_transform(crit)
    w_norm = norm * [0.34,0.33,0.33]
    best   = [w_norm[:,0].max(), w_norm[:,1].max(), w_norm[:,2].min()]
    worst  = [w_norm[:,0].min(), w_norm[:,1].min(), w_norm[:,2].max()]
    df['topsis_score'] = [
        (math.sqrt(sum((row[i]-worst[i])**2 for i in range(3))) /
         (math.sqrt(sum((row[i]-best[i])**2 for i in range(3))) +
          math.sqrt(sum((row[i]-worst[i])**2 for i in range(3)))))
        if (sum((row[i]-best[i])**2 for i in range(3))+sum((row[i]-worst[i])**2 for i in range(3)))>0
        else 0
        for row in w_norm
    ]

    top5 = df.nlargest(5, 'topsis_score')
    return [
        {
            'name': row['adm_nm'],
            'address': row['adm_nm'],  # 필요시 실제 주소 컬럼으로 수정
            'capacity': int(row['capacity_sum']),
            'topsis_score': round(float(row['topsis_score']), 3),
            'lat': float(row['centroid_lat']),
            'lon': float(row['centroid_lon'])
        }
        for _, row in top5.iterrows()
    ]
