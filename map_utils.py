#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
NPP TOPSIS Map (Refactored + Interactive + Batch)
- 비대화식(CLI) + 대화식(프롬프트) + 배치 시나리오 파일
- 대화식 입력: 발전소 / 풍향 / 풍속 / 기상 안정도 / 평일·주말 / 방학 여부 / 시간대
- 배치: --batch-config로 YAML/JSON 파일을 받아 여러 케이스를 일괄 실행
- 선택: --tag-in-filename 으로 시나리오 이름 등을 결과 파일명에 반영
- 선택: season(+slot)에 따라 풍향/풍속/안정도 자동 채움 (wind 미지정 시)
- 성능개선: 민감도 분석에서 정규화 스케일 재사용 + MC/OAT 병렬화(joblib)
"""

from __future__ import annotations
import os
import math
import time
import json
import argparse
import logging
from io import BytesIO
from dataclasses import dataclass
from typing import Dict, Tuple, List, Optional

import numpy as np
import geopandas as gpd
import pandas as pd
import folium
from folium.features import GeoJsonTooltip, DivIcon
import branca.colormap as cm
from sklearn.preprocessing import MinMaxScaler, StandardScaler
from sklearn.decomposition import PCA
from datetime import datetime
try:
    from zoneinfo import ZoneInfo  # Python 3.9+
except Exception:
    ZoneInfo = None

from PIL import Image
import geopy.distance
from pymongo import MongoClient
from scipy.stats import spearmanr, kendalltau

# Optional selenium (이미지 캡처시에만 필요)
try:
    from selenium import webdriver
    from selenium.webdriver.chrome.service import Service
    from webdriver_manager.chrome import ChromeDriverManager
    _SELENIUM_OK = True
except Exception:
    _SELENIUM_OK = False

# Optional joblib (민감도 병렬화)
try:
    from joblib import Parallel, delayed
    _JOBLIB_OK = True
except Exception:
    _JOBLIB_OK = False

# ------------------------------------
# 기본 설정
# ------------------------------------
KST = ZoneInfo("Asia/Seoul") if ZoneInfo else None

POWER_PLANTS: Dict[str, Tuple[float, float]] = {
    '고리': (35.321499, 129.291612),
    '월성': (35.713058, 129.475347),
    '한빛': (35.415534, 126.416692),
    '한울': (37.085932, 129.390857),
}
MAPPING_CODES = {'고리': 'KR', '월성': 'WS', '한빛': 'YK', '한울': 'UJ'}

# 대기 안정도 매핑
# 대기 안정도 매핑(그대로)
KOREAN_TO_CATEGORY = {
    '심한 불안정': 'A', '불안정': 'B', '약간 불안정': 'C', '중립': 'D',
    '약간 안정': 'E', '안정': 'F', '심한 안정': 'G'
}
# 안정할수록 위험 가중 ↑ (r_stab)
STAB_MAP = {'A': 0.8, 'B': 0.9, 'C': 1.0, 'D': 1.1, 'E': 1.2, 'F': 1.35, 'G': 1.5}


# 시간대 가중치 (평일 / 주말 / 방학)
TIME_WEIGHTS_WEEKDAY = {
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
TIME_WEIGHTS_WEEKEND = {
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
TIME_WEIGHTS_VACATION = {
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
DEFAULT_POI_WEIGHT = 0.4

# (선택) 계절/시간대 기반 풍향 규칙표
WIND_RULES = {
    'summer': {'day': {'direction': 200, 'speed': 3.5, 'stability': 'D'},
               'night': {'direction': 220, 'speed': 2.8, 'stability': 'E'}},
    'winter': {'day': {'direction': 330, 'speed': 4.2, 'stability': 'E'},
               'night': {'direction': 350, 'speed': 3.2, 'stability': 'F'}},
    'spring': {'day': {'direction': 240, 'speed': 3.0, 'stability': 'D'},
               'night': {'direction': 260, 'speed': 2.5, 'stability': 'E'}},
    'autumn': {'day': {'direction': 290, 'speed': 3.2, 'stability': 'D'},
               'night': {'direction': 310, 'speed': 2.7, 'stability': 'E'}},
}

# ------------------------------------
# 유틸 함수
# ------------------------------------
def safe_mkdirs(*paths: str) -> None:
    for p in paths:
        os.makedirs(p, exist_ok=True)

def _parse_weights(s: Optional[str]) -> Optional[Dict[str, float]]:
    if not s:
        return None
    out: Dict[str, float] = {}
    for tok in s.split(','):
        k, v = tok.split('=')
        out[k.strip()] = float(v)
    return out

def now_kst() -> datetime:
    if KST:
        return datetime.now(KST)
    return datetime.now()

def get_time_context_auto_full() -> Tuple[str, bool, bool]:
    now = now_kst()
    hour, weekday = now.hour, now.weekday()  # 0=월, 6=일
    if 6 <= hour < 12:
        slot = '오전'
    elif 12 <= hour < 18:
        slot = '오후'
    elif 18 <= hour < 24:
        slot = '야간'
    else:
        slot = '심야'
    is_weekend = (weekday >= 5)
    is_summer_vac = (now.month == 7 and now.day >= 20) or (now.month == 8 and now.day <= 20)
    is_winter_vac = (now.month == 12 and now.day >= 24) or (now.month == 1 and now.day <= 31)
    is_vacation = is_summer_vac or is_winter_vac
    return slot, is_weekend, is_vacation

def pick_slot_weights(slot: str, is_weekend: bool, is_vacation: bool) -> Dict[str, float]:
    if is_vacation:
        return TIME_WEIGHTS_VACATION.get(slot, {})
    elif is_weekend:
        return TIME_WEIGHTS_WEEKEND.get(slot, {})
    else:
        return TIME_WEIGHTS_WEEKDAY.get(slot, {})

def calculate_distance(lat1, lon1, lat2, lon2) -> float:
    R = 6371.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    return R * c

def calculate_bearing(lat1, lon1, lat2, lon2) -> float:
    lat1r, lat2r = math.radians(lat1), math.radians(lat2)
    dlr = math.radians(lon2 - lon1)
    x = math.sin(dlr) * math.cos(lat2r)
    y = math.cos(lat1r) * math.sin(lat2r) - math.sin(lat1r) * math.cos(lat2r) * math.cos(dlr)
    bearing = math.degrees(math.atan2(x, y))
    return (bearing + 360) % 360

def adjust_wind_direction(wd: float) -> float:
    return (wd + 180) % 360

def calculate_wind_risk(wd: float, ws: float, bearing: float,
                        stability_weight: float, dist_km: float, alpha: float = 0.05) -> float:
    adj = adjust_wind_direction(wd)
    rel = abs(adj - bearing)
    if rel > 180:
        rel = 360 - rel
    rel_rad = math.radians(rel)

    # 각도 항: 역풍은 0으로 클리핑
    ang = math.cos(rel_rad)
    if ang < 0:
        ang = 0.0

    # 거리 감쇠
    dist_w = 1.0 / (1.0 + alpha * dist_km)

    # ★ 풍속 역비례(희석), 저풍속 폭주 방지용 eps
    eps = 0.2  # m/s
    speed_factor = 1.0 / max(ws, eps)

    # ★ 안정도 정비례(안정할수록 위험↑)
    r_stab = stability_weight

    risk = speed_factor * ang * dist_w * r_stab
    return risk


def generate_sector(lat: float, lon: float, bearing: float, width: float,
                    radius_km: float = 100, points: int = 30) -> List[Tuple[float, float]]:
    start = bearing - width / 2
    angs = [start + i * (width / points) for i in range(points + 1)]
    coords = [(lat, lon)]
    for ang in angs:
        dest = geopy.distance.distance(kilometers=radius_km).destination((lat, lon), ang)
        coords.append((dest.latitude, dest.longitude))
    return coords

def get_angle_width(stability_weight: float) -> float:
    min_a, max_a = 30, 60
    min_w, max_w = min(STAB_MAP.values()), max(STAB_MAP.values())
    if max_w == min_w:
        return (min_a + max_a) / 2
    ratio = (stability_weight - min_w) / (max_w - min_w)  # 안정↑ → ratio↑
    return max(min_a, min(max_a - ratio * (max_a - min_a), max_a))


def get_sector_style(_feat=None) -> Dict:
    return {'fillColor': 'orange', 'color': 'orange', 'weight': 2, 'fillOpacity': 0.4}

# ------------------------------------
# 데이터 로드/전처리
# ------------------------------------
def read_geojson(path: str) -> gpd.GeoDataFrame:
    try:
        g = gpd.read_file(path)
        logging.info(f"Loaded {os.path.basename(path)}")
        return g
    except Exception as e:
        raise RuntimeError(f"GeoJSON 읽기 실패: {path} :: {e}")

def read_excel(path: str, desc: str) -> pd.DataFrame:
    try:
        df = pd.read_excel(path)
        logging.info(f"Loaded {desc}")
        return df
    except Exception as e:
        raise RuntimeError(f"엑셀 읽기 실패({desc}): {path} :: {e}")

def read_csv_guess_encoding(path: str, desc: str) -> pd.DataFrame:
    try:
        df = pd.read_csv(path, encoding='cp949')
        logging.info(f"Loaded {desc} (cp949)")
        return df
    except Exception:
        try:
            df = pd.read_csv(path, encoding='utf-8-sig')
            logging.info(f"Loaded {desc} (utf-8-sig)")
            return df
        except Exception as e:
            raise RuntimeError(f"CSV 읽기 실패({desc}): {path} :: {e}")

# ------------------------------------
# 날씨(Mongo/API/Manual)
# ------------------------------------
def fetch_weather_mongo(plant: str, mongo_uri: str, db_name: str, col_name: str) -> Dict:
    code = MAPPING_CODES.get(plant)
    if code is None:
        raise KeyError(f"MAPPING_CODES에 '{plant}' 키가 없습니다.")
    client = MongoClient(mongo_uri)
    col = client[db_name][col_name]
    doc = col.find_one({'genName': code}, sort=[('time', -1)])
    if not doc:
        avail = list(col.distinct('genName'))
        raise ValueError(f"genName='{code}' 문서를 찾을 수 없습니다. 현재 genName: {avail}")
    wd = doc.get('winddirection') or doc.get('wind_direction')
    ws = doc.get('windspeed') or doc.get('wind_speed')
    stability_str = doc.get('stability', '')
    if wd is None or ws is None:
        raise ValueError(f"불완전 기상 데이터: {doc}")
    category = KOREAN_TO_CATEGORY.get(stability_str, 'D')
    weight = STAB_MAP[category]
    logging.info(f"Weather {plant}: wind={wd}°/{ws}m/s, stability='{stability_str}' → {category}({weight})")
    return {
        'wind_direction': float(wd),
        'wind_speed': float(ws),
        'stability_category': category,
        'stability_weight': float(weight)
    }

def fetch_weather_api(plant: str, api_base: str) -> Dict:
    import requests
    code = MAPPING_CODES.get(plant)
    if code is None:
        raise KeyError(f"MAPPING_CODES에 '{plant}' 키가 없습니다.")
    url = api_base.rstrip('/') + f"/api/radiation_status/{code}"
    r = requests.get(url, timeout=10)
    r.raise_for_status()
    payload = r.json()
    wd = payload.get('wind_direction')
    ws = payload.get('wind_speed')
    stability_str = payload.get('stability', '')
    if wd is None or ws is None:
        raise ValueError(f"불완전 기상 데이터(API): {payload}")
    category = KOREAN_TO_CATEGORY.get(stability_str, 'D')
    weight = STAB_MAP[category]
    logging.info(f"Weather(API) {plant}: wind={wd}°/{ws}m/s, stability='{stability_str}' → {category}({weight})")
    return {
        'wind_direction': float(wd),
        'wind_speed': float(ws),
        'stability_category': category,
        'stability_weight': float(weight)
    }

def fetch_weather_manual(plant: str, wd: float, ws: float, stability_input: str) -> Dict:
    s = stability_input.strip().upper()
    if len(s) == 1 and s in STAB_MAP:
        category = s
    else:
        category = KOREAN_TO_CATEGORY.get(stability_input.strip(), 'D')
    weight = STAB_MAP.get(category, STAB_MAP['D'])
    return {
        'wind_direction': float(wd),
        'wind_speed': float(ws),
        'stability_category': category,
        'stability_weight': float(weight)
    }

# ------------------------------------
# 핵심 파이프라인
# ------------------------------------
@dataclass
class Paths:
    base_dir: str
    out_map: str
    out_cap: str
    out_score: str
    pop_path: str
    shel_path: str
    poi_path: str
    regions: Dict[str, str]

def build_paths(base_dir: str) -> Paths:
    out_map = os.path.join(base_dir, 'map')
    out_cap = os.path.join(out_map, '캡쳐')
    out_score = os.path.join(base_dir, '점수')
    regions = {
        '부산광역시':      os.path.join(base_dir, 'hangjeongdong_부산광역시.geojson'),
        '울산광역시':      os.path.join(base_dir, 'hangjeongdong_울산광역시.geojson'),
        '경상북도':        os.path.join(base_dir, 'hangjeongdong_경상북도.geojson'),
        '전라남도':        os.path.join(base_dir, 'hangjeongdong_전라남도.geojson'),
        '전라북도':        os.path.join(base_dir, 'hangjeongdong_전라북도.geojson'),
        '경상남도':        os.path.join(base_dir, 'hangjeongdong_경상남도.geojson'),
        '대구광역시':      os.path.join(base_dir, 'hangjeongdong_대구광역시.geojson'),
        '광주광역시':      os.path.join(base_dir, 'hangjeongdong_광주광역시.geojson'),
        '강원특별자치도':  os.path.join(base_dir, 'hangjeongdong_강원도.geojson'),
    }
    pop_path = os.path.join(base_dir, 'population2.xlsx')
    shel_path = os.path.join(base_dir, 'shelter.xlsx')
    poi_path = os.path.join(base_dir, 'poi_data.csv')
    return Paths(base_dir, out_map, out_cap, out_score, pop_path, shel_path, poi_path, regions)

def load_and_merge_geodata(paths: Paths) -> gpd.GeoDataFrame:
    gdfs = [read_geojson(p) for p in paths.regions.values()]
    gdf = pd.concat(gdfs, ignore_index=True)
    logging.info("Merged regions.")

    pop_df = read_excel(paths.pop_path, 'population')
    sido_map = {
        '부산광역시': '부산광역시', '울산광역시': '울산광역시', '대구광역시': '대구광역시', '광주광역시': '광주광역시',
        '전라남도': '전라남도', '전라북도': '전라북도', '경상남도': '경상남도', '경상북도': '경상북도', '강원특별자치도': '강원도'
    }
    pop_df['sido_full'] = pop_df['광역지자체'].map(sido_map)
    pop_df['adm_nm_full'] = pop_df['sido_full'] + ' ' + pop_df['행정구역'] + ' ' + pop_df['adm_cd']

    if 'adm_nm' not in gdf.columns:
        raise KeyError("GeoJSON에 'adm_nm' 컬럼이 필요합니다.")

    gdf = gdf.merge(pop_df[['adm_nm_full','population']], left_on='adm_nm', right_on='adm_nm_full', how='left')\
             .drop(columns=['adm_nm_full'])

    missing = gdf[gdf['population'].isna()]
    if not missing.empty:
        logging.warning("인구 병합 실패 예시(최대 10개): %s", missing['adm_nm'].unique()[:10])
    else:
        logging.info("모든 행정동에 population 병합 완료")

    proj = gdf.to_crs('EPSG:5179')
    proj['centroid'] = proj.geometry.centroid
    gdf['centroid_lat'] = proj['centroid'].to_crs('EPSG:4326').y
    gdf['centroid_lon'] = proj['centroid'].to_crs('EPSG:4326').x

    shel_df = read_excel(paths.shel_path, 'shelter')
    sg = gpd.GeoDataFrame(shel_df, geometry=gpd.points_from_xy(shel_df.longitude, shel_df.latitude), crs='EPSG:4326')
    if gdf.crs is None:
        gdf.set_crs('EPSG:4326', inplace=True)
    if gdf.crs.to_string() != 'EPSG:4326':
        gdf = gdf.to_crs('EPSG:4326')
    sg = gpd.sjoin(sg, gdf[['adm_nm', 'geometry']], how='left', predicate='within')
    cap_sum = sg.groupby('adm_nm', dropna=False)['capacity'].sum().reset_index().rename(columns={'capacity': 'capacity_sum'})
    gdf = gdf.merge(cap_sum, on='adm_nm', how='left').fillna({'capacity_sum': 0})

    gdf[['시도명','시군구명','행정동명']] = gdf['adm_nm'].str.split(' ', n=2, expand=True)
    return gdf

def compute_dynamic_population(gdf: gpd.GeoDataFrame, poi_df: pd.DataFrame,
                               slot_weights: Dict[str, float], alpha: float) -> gpd.GeoDataFrame:
    poi_df = poi_df.copy()
    poi_df['time_weight'] = poi_df['상권업종중분류명'].map(slot_weights).fillna(DEFAULT_POI_WEIGHT)
    poi_grouped = (poi_df.groupby(['시도명','시군구명','행정동명'])['time_weight']
                   .sum().reset_index(name='poi_weighted'))
    gdf = gdf.merge(poi_grouped, on=['시도명','시군구명','행정동명'], how='left').fillna({'poi_weighted': 0})
    gdf['commercial_index'] = MinMaxScaler().fit_transform(gdf[['poi_weighted']])
    gdf['dynamic_population'] = gdf['population'] * (1 + alpha * gdf['commercial_index'])
    return gdf

def compute_scores(gdf: gpd.GeoDataFrame, plant_lat: float, plant_lon: float,
                   wind_params: Dict, opt_km: float, wind_alpha: float,
                   weights: Optional[Dict[str, float]] = None) -> gpd.GeoDataFrame:
    gdf['distance_to_nearest_plant'] = gdf.apply(
        lambda r: calculate_distance(plant_lat, plant_lon, r['centroid_lat'], r['centroid_lon']), axis=1
    )

    # ★ 하드 배제: 발전소 10km 이내 구역은 점수가 높아도 제외
    EXCLUDE_MIN_KM = 10.0
    gdf = gdf[gdf['distance_to_nearest_plant'] >= EXCLUDE_MIN_KM].reset_index(drop=True)

    def triangular_distance_score(d, opt=opt_km):
        return d if d <= opt else max(0, 2 * opt - d)

    gdf['distance_score'] = gdf['distance_to_nearest_plant'].apply(triangular_distance_score)
    gdf = gdf[gdf['distance_to_nearest_plant'] <= 2 * opt_km].reset_index(drop=True)

    gdf['SC_dynamic%'] = gdf.apply(
        lambda r: (r['capacity_sum'] / r['dynamic_population'] * 100) if r['dynamic_population'] > 0 else 0, axis=1
    )
    gdf['abs_capacity'] = gdf['capacity_sum']

    gdf['bearing'] = gdf.apply(
        lambda r: calculate_bearing(plant_lat, plant_lon, r['centroid_lat'], r['centroid_lon']), axis=1
    )
    gdf['wind_risk'] = gdf.apply(
        lambda r: calculate_wind_risk(
            wind_params['wind_direction'], wind_params['wind_speed'], r['bearing'],
            wind_params['stability_weight'], r['distance_to_nearest_plant'], alpha=wind_alpha
        ),
        axis=1
    )

    cap_df = gdf[['SC_dynamic%', 'abs_capacity']].fillna(0)
    cap_std = StandardScaler().fit_transform(cap_df)
    gdf['cap_pc1'] = PCA(n_components=1).fit_transform(cap_std).flatten()

    criteria = gdf[['distance_score', 'cap_pc1', 'wind_risk', 'commercial_index']].fillna(0)
    if weights is None:
        weights = {'distance_score': 0.25, 'cap_pc1': 0.25, 'wind_risk': 0.25, 'commercial_index': 0.25}
    w = pd.Series(weights).reindex(criteria.columns).fillna(0.0)
    w = w / w.sum() if w.sum() > 0 else pd.Series([0.25, 0.25, 0.25, 0.25], index=criteria.columns)

    norm_vals = MinMaxScaler().fit_transform(criteria)
    norm_df = pd.DataFrame(norm_vals, columns=criteria.columns, index=criteria.index)
    weighted_df = norm_df.mul(w, axis=1)

    ideal_best = {
        'distance_score': weighted_df['distance_score'].max(),
        'cap_pc1':        weighted_df['cap_pc1'].max(),
        'wind_risk':      weighted_df['wind_risk'].min(),
        'commercial_index': weighted_df['commercial_index'].min(),
    }
    ideal_worst = {
        'distance_score': weighted_df['distance_score'].min(),
        'cap_pc1':        weighted_df['cap_pc1'].min(),
        'wind_risk':      weighted_df['wind_risk'].max(),
        'commercial_index': weighted_df['commercial_index'].max(),
    }

    def compute_topsis_row(idx: int) -> float:
        wrow = weighted_df.loc[idx]
        d_best  = math.sqrt(sum((wrow[c] - ideal_best[c])**2 for c in weighted_df.columns))
        d_worst = math.sqrt(sum((wrow[c] - ideal_worst[c])**2 for c in weighted_df.columns))
        return d_worst / (d_best + d_worst) if (d_best + d_worst) else 0.0

    gdf['topsis_score'] = [compute_topsis_row(i) for i in weighted_df.index]
    return gdf

def build_map(gdf: gpd.GeoDataFrame, selected: str, plant_lat: float, plant_lon: float,
              wind_params: Dict, sector_radius_km: float = 100) -> folium.Map:
    m = folium.Map(location=[36.0, 127.5], zoom_start=8, tiles=None)
    folium.TileLayer(
        tiles='https://{s}.basemaps.cartocdn.com/light_all/{z}/{x}/{y}{r}.png',
        name='CartoDB Positron',
        attr='Map tiles by CartoDB, CC BY 3.0 — Map data © OpenStreetMap contributors'
    ).add_to(m)

    power_layer = folium.FeatureGroup(name='발전소').add_to(m)
    angle = (adjust_wind_direction(wind_params['wind_direction']) - 90) % 360
    arrow_html = f"""<div style="transform:rotate({angle}deg);font-size:36px;color:blue">
                       <i class="fa fa-arrow-circle-right"></i></div>"""
    folium.Marker(
        location=[plant_lat, plant_lon],
        icon=DivIcon(icon_size=(50, 50), icon_anchor=(25, 25), html=arrow_html),
        popup=f"Wind: {wind_params['wind_direction']}°"
    ).add_to(power_layer)

    width = get_angle_width(wind_params['stability_weight'])
    bearing = adjust_wind_direction(wind_params['wind_direction'])
    coords = generate_sector(plant_lat, plant_lon, bearing, width, radius_km=sector_radius_km)
    folium.Polygon(locations=coords, **get_sector_style(None), popup=f"{selected} 발전소 풍향 섹터").add_to(m)

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
                fields=['adm_nm','population','distance_score','cap_pc1','wind_risk','commercial_index','topsis_score'],
                aliases=['행정동:','인구수:','거리 점수:','통합 수용능력(PC1):','풍위험:','유동인구지수:','TOPSIS 점수:'],
                localize=True
            ),
            name='TOPSIS'
        ).add_to(m)
        topsis_cm.add_to(m)

    for r in [10000, 30000, 60000, 100000]:
        folium.Circle(
            location=[plant_lat, plant_lon],
            radius=r, color='black', fill=False, dash_array='5', weight=2,
            popup=f"{r // 1000}km 반경"
        ).add_to(m)

    folium.LayerControl(position='topleft').add_to(m)
    return m

def add_topN_markers(m: folium.Map, gdf: gpd.GeoDataFrame, topN: int) -> pd.DataFrame:
    if gdf.empty:
        folium.map.Popup("후보지가 없습니다(거리 필터에 모두 제외됨). opt-km를 키워보세요.").add_to(m)
        return gdf.iloc[0:0]
    topN = min(max(1, topN), len(gdf))
    topN_sites = gdf.nlargest(topN, 'topsis_score').copy()
    for _, row in topN_sites.iterrows():
        popup_html = f"""
        <b>{row['adm_nm']}</b><br>
        인구수: {int(row['population']) if pd.notna(row['population']) else 0:,}<br>
        수용인원: {int(row['capacity_sum']) if pd.notna(row['capacity_sum']) else 0:,}<br>
        동적인구: {int(row['dynamic_population']) if pd.notna(row['dynamic_population']) else 0:,}<br>
        TOPSIS 점수: {row['topsis_score']:.3f}<br>
        <a href="https://www.google.com/maps?q={row['centroid_lat']},{row['centroid_lon']}" target="_blank">구글맵에서 보기</a>
        """
        folium.Marker(
            location=[row['centroid_lat'], row['centroid_lon']],
            popup=folium.Popup(popup_html, max_width=350),
            icon=folium.Icon(color='lightblue', icon='fa-light fa-person-shelter', prefix='fa')
        ).add_to(m)
    return topN_sites

def save_outputs(gdf: gpd.GeoDataFrame, m: folium.Map, paths: Paths, topN_df: pd.DataFrame,
                 save_images: bool, image_scale: int) -> Dict[str, str]:
    safe_mkdirs(paths.out_map, paths.out_cap, paths.out_score)
    xlsx_path = os.path.join(paths.out_score, 'topsis_result.xlsx')
    gdf.to_excel(xlsx_path, index=False)
    top_csv = os.path.join(paths.out_score, 'topN_sites.csv')
    topN_df.to_csv(top_csv, index=False, encoding='utf-8-sig')
    html_path = os.path.join(paths.out_map, 'NPP_topsis_map.html')
    m.save(html_path)
    saved = {"excel": xlsx_path, "topN_csv": top_csv, "html": html_path}
    if save_images:
        if not _SELENIUM_OK:
            logging.warning("selenium/webdriver-manager 미설치로 이미지 저장을 건너뜁니다.")
        else:
            png_path = os.path.join(paths.out_cap, 'NPP_topsis_map.png')
            tiff_path = os.path.join(paths.out_cap, 'NPP_topsis_map.tiff')
            save_map_as_image(html_path, png_path, image_format='png', scale=image_scale)
            try:
                Image.open(png_path).save(tiff_path, format='TIFF')
                saved.update({"png": png_path, "tiff": tiff_path})
            except Exception as e:
                logging.warning(f"PNG→TIFF 변환 실패: {e}")
    return saved

def save_map_as_image(html_path: str, output_image_path: str, image_format: str = 'png', scale: int = 3) -> None:
    if not _SELENIUM_OK:
        raise RuntimeError("이미지 저장에는 selenium, webdriver_manager가 필요합니다.")
    chrome_options = webdriver.ChromeOptions()
    for arg in ["--headless", "--hide-scrollbars", "--no-sandbox", "--disable-dev-shm-usage", "--window-size=1440,1200"]:
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
        logging.info(f"Saved map image: {output_image_path}")
    finally:
        driver.quit()

def _stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")

def save_outputs_with_tag(gdf: gpd.GeoDataFrame, m: folium.Map, paths: Paths, topN_df: pd.DataFrame,
                          save_images: bool, image_scale: int, tag: Optional[str]) -> Dict[str, str]:
    safe_mkdirs(paths.out_map, paths.out_cap, paths.out_score)
    stamp = _stamp()
    safe_tag = f"_{tag}" if tag else ""
    xlsx_path = os.path.join(paths.out_score, f'topsis_result{safe_tag}_{stamp}.xlsx')
    top_csv  = os.path.join(paths.out_score, f'topN_sites{safe_tag}_{stamp}.csv')
    html_path= os.path.join(paths.out_map,   f'NPP_topsis_map{safe_tag}_{stamp}.html')
    gdf.to_excel(xlsx_path, index=False)
    topN_df.to_csv(top_csv, index=False, encoding='utf-8-sig')
    m.save(html_path)
    saved = {"excel": xlsx_path, "topN_csv": top_csv, "html": html_path}
    if save_images and _SELENIUM_OK:
        png_path  = os.path.join(paths.out_cap, f'NPP_topsis_map{safe_tag}_{stamp}.png')
        tiff_path = os.path.join(paths.out_cap, f'NPP_topsis_map{safe_tag}_{stamp}.tiff')
        save_map_as_image(html_path, png_path, image_format='png', scale=image_scale)
        try:
            Image.open(png_path).save(tiff_path, format='TIFF')
            saved.update({"png": png_path, "tiff": tiff_path})
        except Exception as e:
            logging.warning(f"PNG→TIFF 변환 실패: {e}")
    elif save_images:
        logging.warning("selenium/webdriver-manager 미설치로 이미지 저장을 건너뜁니다.")
    return saved

def scenario_tag(s: dict) -> str:
    if 'name' in s and s['name']:
        return str(s['name'])
    base = f"{s.get('plant','')}_{s.get('slot','')}"
    if s.get('is_weekend') is not None:
        base += "_주말" if s['is_weekend'] else "_평일"
    if s.get('is_vacation') is not None:
        base += "_방학" if s['is_vacation'] else "_학기중"
    if s.get('season'):
        base += f"_{s['season']}"
    return base

def load_scenarios(path: str) -> List[dict]:
    ext = os.path.splitext(path)[1].lower()
    if ext in ('.yaml', '.yml'):
        try:
            import yaml
        except Exception:
            raise RuntimeError("PyYAML이 필요합니다. pip install pyyaml")
        with open(path, 'r', encoding='utf-8') as f:
            data = yaml.safe_load(f)
    elif ext == '.json':
        with open(path, 'r', encoding='utf-8') as f:
            data = json.load(f)
    else:
        raise ValueError("시나리오 파일 확장자를 확인하세요(.yaml/.yml/.json)")
    if not isinstance(data, list):
        raise ValueError("시나리오 파일은 리스트 형태여야 합니다. 예: - { ... } - { ... }")
    return data

def infer_wind_from_rules(season: Optional[str], slot: str) -> Optional[dict]:
    if not season:
        return None
    is_night = (slot in ('야간', '심야'))
    key = 'night' if is_night else 'day'
    rule = WIND_RULES.get(season, {}).get(key)
    if not rule:
        return None
    s = str(rule['stability'])
    cat = s if len(s) == 1 and s in STAB_MAP else KOREAN_TO_CATEGORY.get(s, 'D')
    return {
        'wind_direction': float(rule['direction']),
        'wind_speed': float(rule['speed']),
        'stability_category': cat,
        'stability_weight': float(STAB_MAP.get(cat, STAB_MAP['D']))
    }

def _normalize_weights(base: Dict[str, float]) -> Dict[str, float]:
    s = sum(base.values())
    if s <= 0:
        n = len(base)
        return {k: 1.0/n for k in base}
    return {k: v/s for k, v in base.items()}

def topsis_score_with_weights(gdf: gpd.GeoDataFrame, weights: Dict[str,float]) -> pd.Series:
    cols = ['distance_score','cap_pc1','wind_risk','commercial_index']
    criteria = gdf[cols].fillna(0)
    w = pd.Series(_normalize_weights({c: weights.get(c,0.0) for c in cols}))
    norm_vals = MinMaxScaler().fit_transform(criteria)
    weighted_df = pd.DataFrame(norm_vals, columns=cols, index=criteria.index).mul(w, axis=1)

    ideal_best = {
        'distance_score': weighted_df['distance_score'].max(),
        'cap_pc1':        weighted_df['cap_pc1'].max(),
        'wind_risk':      weighted_df['wind_risk'].min(),
        'commercial_index': weighted_df['commercial_index'].min(),
    }
    ideal_worst = {
        'distance_score': weighted_df['distance_score'].min(),
        'cap_pc1':        weighted_df['cap_pc1'].min(),
        'wind_risk':      weighted_df['wind_risk'].max(),
        'commercial_index': weighted_df['commercial_index'].max(),
    }

    def row_score(row):
        d_best  = math.sqrt(sum((row[c]-ideal_best[c])**2 for c in weighted_df.columns))
        d_worst = math.sqrt(sum((row[c]-ideal_worst[c])**2 for c in weighted_df.columns))
        return d_worst/(d_best+d_worst) if (d_best+d_worst)>0 else 0.0

    return weighted_df.apply(row_score, axis=1)

# ===== 성능개선: 민감도용 정규화 재사용 + 병렬화 버전 =====
def _prepare_topsis_norm_matrix(gdf: gpd.GeoDataFrame):
    cols = ['distance_score','cap_pc1','wind_risk','commercial_index']
    X = gdf[cols].fillna(0).to_numpy()
    scaler = MinMaxScaler().fit(X)
    Xn = scaler.transform(X)
    return Xn, cols

def _topsis_from_norm(Xn: np.ndarray, weights: Dict[str, float], cols: List[str]) -> np.ndarray:
    w = np.array([weights.get(c, 0.0) for c in cols], dtype=float)
    s = w.sum()
    if s <= 0:
        w[:] = 1.0 / len(cols)
    else:
        w = w / s
    W = Xn * w
    # maximize(+1): distance_score, cap_pc1 / minimize(-1): wind_risk, commercial_index
    maximize = np.array([1, 1, -1, -1], dtype=int)
    best  = np.where(maximize == 1, W.max(axis=0), W.min(axis=0))
    worst = np.where(maximize == 1, W.min(axis=0), W.max(axis=0))
    d_best  = np.linalg.norm(W - best, axis=1)
    d_worst = np.linalg.norm(W - worst, axis=1)
    denom = d_best + d_worst
    score = np.divide(d_worst, denom, out=np.zeros_like(d_best), where=denom!=0)
    return score

def sensitivity_analysis_weights(
        gdf: gpd.GeoDataFrame,
        base_weights: Dict[str,float],
        topN: int = 10,
        mode: str = 'oat',     # 'oat' or 'mc'
        delta: float = 0.10,   # OAT에서 각 가중치 ±delta 범위
        steps: int = 5,        # OAT에서 분할 단계수
        mc_runs: int = 200,    # Monte Carlo 시행 수
        random_state: int = 42
    ) -> pd.DataFrame:

    rng = np.random.default_rng(random_state)
    Xn, cols = _prepare_topsis_norm_matrix(gdf)
    base = _normalize_weights({c: base_weights.get(c,0.0) for c in cols})

    base_scores = _topsis_from_norm(Xn, base, cols)
    base_rank = pd.Series(base_scores).rank(ascending=False, method='min')
    base_top = set(pd.Series(base_scores).nlargest(topN).index)

    def summarize(W: Dict[str, float]) -> dict:
        scores = _topsis_from_norm(Xn, W, cols)
        rank = pd.Series(scores).rank(ascending=False, method='min')
        rho, _ = spearmanr(base_rank, rank)
        ktau, _ = kendalltau(base_rank, rank)
        avg_abs_rank_shift = float((rank - base_rank).abs().mean())
        new_top = set(pd.Series(scores).nlargest(topN).index)
        jacc = len(base_top & new_top) / max(1, len(base_top | new_top))
        return {
            **{f'w_{k}': W[k] for k in cols},
            'spearman_rho': rho,
            'kendall_tau': ktau,
            'avg_abs_rank_shift': avg_abs_rank_shift,
            'topN_jaccard': jacc
        }

    records: List[dict] = []

    if mode == 'oat':
        Ws: List[Dict[str, float]] = []
        for c in cols:
            base_w = base.copy()
            lo = max(0.0, base_w[c]*(1-delta))
            hi = min(1.0, base_w[c]*(1+delta))
            grid = np.linspace(lo, hi, steps)
            for val in grid:
                W = base_w.copy()
                remainder = max(0.0, 1.0 - val)
                others = [k for k in cols if k != c]
                other_sum = sum(base_w[k] for k in others)
                if other_sum == 0:
                    for k in others: W[k] = remainder/len(others)
                else:
                    for k in others: W[k] = remainder * (base_w[k]/other_sum)
                W[c] = float(val)
                Ws.append(W)
        if _JOBLIB_OK:
            records = Parallel(n_jobs=-1, backend="loky")(delayed(summarize)(W) for W in Ws)
        else:
            records = [summarize(W) for W in Ws]

    elif mode == 'mc':
        alpha = np.array([base[k] for k in cols]) * 50 + 1e-6
        draws = rng.dirichlet(alpha, size=mc_runs)
        Ws = [{k: float(v) for k, v in zip(cols, d)} for d in draws]
        if _JOBLIB_OK:
            records = Parallel(n_jobs=-1, backend="loky")(delayed(summarize)(W) for W in Ws)
        else:
            records = [summarize(W) for W in Ws]
    else:
        raise ValueError("mode는 'oat' 또는 'mc'만 허용")

    df = pd.DataFrame.from_records(records)
    return df

def run_one_scenario(selected: str, slot: str, is_weekend: bool, is_vacation: bool,
                     wind_params: dict, topN: int, args, paths: Paths, tag: Optional[str] = None) -> dict:
    slot_weights = pick_slot_weights(slot, is_weekend, is_vacation)

    gdf = load_and_merge_geodata(paths)
    poi_df = read_csv_guess_encoding(paths.poi_path, 'poi_data')
    gdf = compute_dynamic_population(gdf, poi_df, slot_weights, alpha=args.dynamic_alpha)

    plant_lat, plant_lon = POWER_PLANTS[selected]
    gdf = compute_scores(
        gdf, plant_lat, plant_lon, wind_params,
        opt_km=args.opt_km, wind_alpha=args.wind_alpha,
        weights=args.weights_parsed
    )

    sensi_paths = {}
    if args.sensitivity != 'none':
        base_default_weights = {'distance_score': 0.25, 'cap_pc1': 0.25, 'wind_risk': 0.25, 'commercial_index': 0.25}
        base_w = args.weights_parsed or base_default_weights
        os.makedirs(paths.out_score, exist_ok=True)
        sensi_tag = "interactive"

        def _run_sensi(mode_label: str):
            df = sensitivity_analysis_weights(
                gdf, base_w,
                topN=args.sensi_topN,
                mode=mode_label,
                delta=args.sensi_delta,
                steps=args.sensi_steps,
                mc_runs=args.sensi_mc_runs
            )
            outp = os.path.join(paths.out_score, f'weight_sensitivity_{sensi_tag}_{mode_label}.csv')
            df.to_csv(outp, index=False, encoding='utf-8-sig')
            sensi_paths[mode_label] = outp
            logging.info(f"가중치 민감도 결과({mode_label}) 저장: {outp}")

        if args.sensitivity in ('oat', 'both'):
            _run_sensi('oat')
        if args.sensitivity in ('mc', 'both'):
            _run_sensi('mc')

    m = build_map(gdf, selected, plant_lat, plant_lon, wind_params,
                  sector_radius_km=args.sector_radius_km)
    topN_df = add_topN_markers(m, gdf, topN=max(1, min(topN, len(gdf))))

    saved = save_outputs_with_tag(gdf, m, paths, topN_df, args.save_images, args.image_scale, tag)
    if sensi_paths:
        saved['sensitivity'] = sensi_paths
    return saved

def run_batch(args, paths: Paths):
    scenarios = load_scenarios(args.batch_config)
    all_saved = []
    fixed_plant = args.plant
    for i, sc in enumerate(scenarios, 1):
        selected = sc.get('plant') or fixed_plant
        if not selected:
            raise ValueError(f"[시나리오 {i}] 발전소(plant)가 지정되지 않았습니다. (--plant 또는 scenario.plant)")
        slot       = sc['slot']
        is_weekend = bool(sc['is_weekend'])
        is_vacation= bool(sc['is_vacation'])
        topN       = int(sc.get('topN', 5))
        if 'wind' in sc and sc['wind'] is not None:
            w = sc['wind']
            wind_params = fetch_weather_manual(selected, float(w['direction']), float(w['speed']), str(w['stability']))
        else:
            inferred = infer_wind_from_rules(sc.get('season'), slot)
            if not inferred:
                raise ValueError(f"[시나리오 {i}] wind 미지정 & season 규칙도 없음 → 풍향정보 필요")
            wind_params = inferred
        tag = scenario_tag(sc) if args.tag_in_filename else None
        logging.info(f"[{i}/{len(scenarios)}] 실행: {tag or '(no-tag)'}")
        saved = run_one_scenario(selected, slot, is_weekend, is_vacation, wind_params, topN, args, paths, tag)
        all_saved.append({"scenario": tag or f"scenario_{i}", "files": saved})

    try:
        import zipfile
        bundle = os.path.join(paths.base_dir, f"batch_bundle_{_stamp()}.zip")
        with zipfile.ZipFile(bundle, 'w', zipfile.ZIP_DEFLATED) as z:
            for item in all_saved:
                for _, p in item["files"].items():
                    if isinstance(p, str) and os.path.exists(p):
                        z.write(p, arcname=os.path.basename(p))
        logging.info(f"배치 결과 ZIP: {bundle}")
        all_saved.append({"bundle": bundle})
    except Exception as e:
        logging.warning(f"ZIP 번들 생성 실패: {e}")
    print(json.dumps(all_saved, ensure_ascii=False, indent=2))

def _input_with_default(prompt: str, default: Optional[str] = None) -> str:
    s = input(f"{prompt}{' ['+default+']' if default is not None else ''}: ").strip()
    return s if s else (default if default is not None else '')

def prompt_user_interactive() -> Dict:
    print("""\n=== 대화식 입력 모드 ===""")
    plant_names = list(POWER_PLANTS.keys())
    print("사고 발전소를 선택하세요:")
    for i, name in enumerate(plant_names, 1):
        print(f"  {i}. {name}")
    while True:
        sel = _input_with_default(f"번호(1-{len(plant_names)}) 또는 이름", None)
        try:
            if sel.isdigit():
                idx = int(sel)
                if 1 <= idx <= len(plant_names):
                    selected = plant_names[idx-1]
                    break
            if sel in plant_names:
                selected = sel
                break
        except Exception:
            pass
        print("→ 올바른 번호/이름을 입력하세요.")

    while True:
        try:
            wd = float(_input_with_default("풍향(0~360°)", "180"))
            if 0 <= wd <= 360:
                break
        except Exception:
            pass
        print("→ 0~360 사이 숫자를 입력하세요.")
    while True:
        try:
            ws = float(_input_with_default("풍속(m/s)", "3.0"))
            if ws >= 0:
                break
        except Exception:
            pass
        print("→ 0 이상 숫자를 입력하세요.")
    stab_help = "안정도(A~G) 또는 한글(중립/불안정/안정 등)"
    stability_input = _input_with_default(f"기상 안정도({stab_help})", "D")

    while True:
        wk = _input_with_default("오늘이 주말입니까? (y/n)", "n").lower()
        if wk in ("y","n"):
            is_weekend = (wk == 'y')
            break
        print("→ y 또는 n")

    while True:
        vac = _input_with_default("지금 방학입니까? (y/n)", "n").lower()
        if vac in ("y","n"):
            is_vacation = (vac == 'y')
            break
        print("→ y 또는 n")

    slot_options = ['오전','오후','야간','심야']
    print("시간대를 선택하세요: ")
    for i, s in enumerate(slot_options, 1):
        print(f"  {i}. {s}")
    while True:
        ssel = _input_with_default(f"번호(1-4) 또는 시간대", None)
        if ssel.isdigit() and 1 <= int(ssel) <= 4:
            slot = slot_options[int(ssel)-1]
            break
        if ssel in slot_options:
            slot = ssel
            break
        print("→ 올바른 번호/시간대를 입력하세요.")

    while True:
        try:
            topN = int(_input_with_default("지도에 표시할 TOP N", "5"))
            if topN >= 1:
                break
        except Exception:
            pass
        print("→ 1 이상의 정수를 입력하세요.")

    wind_params = fetch_weather_manual(selected, wd, ws, stability_input)
    return {
        'selected': selected,
        'slot': slot,
        'is_weekend': is_weekend,
        'is_vacation': is_vacation,
        'topN': topN,
        'wind_params': wind_params,
    }

# ------------------------------------
# 메인
# ------------------------------------
def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="NPP TOPSIS Map (Refactored + Interactive + Batch)")
    parser.add_argument('--interactive', action='store_true', help='대화식 입력 사용')
    parser.add_argument('--batch-config', type=str, help='여러 시나리오(YAML/JSON) 일괄 실행')
    parser.add_argument('--tag-in-filename', action='store_true', help='파일명에 시나리오 name/tag 반영')

    # 비대화식 옵션
    parser.add_argument('--plant', choices=list(POWER_PLANTS.keys()), help='발전소 선택(비대화식/배치 고정)')
    parser.add_argument('--topN', type=int, default=5, help='지도에 표시할 TOP N 구역(비대화식)')
    parser.add_argument('--base-dir', type=str, default=r"E:/논문/On going/auto", help='입출력 기본 디렉터리')
    parser.add_argument('--sector-radius-km', type=float, default=100.0, help='풍향 섹터 반경(km)')
    parser.add_argument('--opt-km', type=float, default=60.0, help='거리 최적값(삼각 점수) km')
    parser.add_argument('--dynamic-alpha', type=float, default=0.5, help='동적인구 가중치 α')
    parser.add_argument('--wind-alpha', type=float, default=0.025, help='풍위험 거리 감쇠 α')

    # 시간 문맥(비대화식 전용; interactive에서는 무시)
    parser.add_argument('--slot', choices=['오전','오후','야간','심야'], default=None, help='시간대 강제 설정(비대화식)')
    parser.add_argument('--weekend', type=str, default=None, choices=['true','false'], help='주말 여부 강제(비대화식)')
    parser.add_argument('--vacation', type=str, default=None, choices=['true','false'], help='방학 여부 강제(비대화식)')

    # 날씨 소스(비대화식 전용)
    parser.add_argument('--weather-source', choices=['mongo','api'], default='mongo')
    parser.add_argument('--mongo-uri', type=str, default='mongodb://localhost:27017')
    parser.add_argument('--mongo-db', type=str, default='Data')
    parser.add_argument('--mongo-col', type=str, default='NPP_weather')
    parser.add_argument('--api-base', type=str, default='http://localhost:8000', help='weather-source=api 일 때 기본 URL')

    # 출력 제어
    parser.add_argument('--save-images', action='store_true', help='지도 이미지를 PNG/TIFF로 저장')
    parser.add_argument('--image-scale', type=int, default=3, help='이미지 확대 배율')

    # 로깅
    parser.add_argument('--log-level', type=str, default='INFO', choices=['DEBUG','INFO','WARNING','ERROR'])

    # TOPSIS 가중치/민감도
    parser.add_argument('--weights', type=str, default=None,
                        help='예: distance_score=0.25,cap_pc1=0.25,wind_risk=0.25,commercial_index=0.25')
    parser.add_argument('--sensitivity', choices=['none', 'oat', 'mc', 'both'], default='none',
                        help="가중치 민감도 테스트 모드 (oat / mc / both)")
    parser.add_argument('--sensi-delta', type=float, default=0.10, help='OAT에서 기준 가중치 대비 ±변화 폭(비율)')
    parser.add_argument('--sensi-steps', type=int, default=5, help='OAT에서 분할 단계 수')
    parser.add_argument('--sensi-mc-runs', type=int, default=200, help='Monte Carlo 시행 수')
    parser.add_argument('--sensi-topN', type=int, default=10, help='TopN 교집합 평가 기준')
    return parser.parse_args()

def main():
    args = parse_args()
    args.weights_parsed = _parse_weights(args.weights)
    logging.basicConfig(level=getattr(logging, args.log_level), format='[%(levelname)s] %(message)s')

    paths = build_paths(args.base_dir)
    safe_mkdirs(paths.out_map, paths.out_cap, paths.out_score)

    if args.batch_config:
        run_batch(args, paths)
        return

    if not args.interactive and not args.plant:
        logging.info("플래그가 없어 대화식으로 전환합니다. (PyCharm 콘솔 실행용)")
        args.interactive = True

    if args.interactive:
        args.base_dir = _input_with_default("입출력 기본 경로를 지정하세요", args.base_dir)
        paths = build_paths(args.base_dir)
        safe_mkdirs(paths.out_map, paths.out_cap, paths.out_score)

    if args.interactive:
        while True:
            sensi_path = None
            ui = prompt_user_interactive()
            selected = ui['selected']
            slot = ui['slot']
            is_weekend = ui['is_weekend']
            is_vacation = ui['is_vacation']
            topN = ui['topN']
            wind_params = ui['wind_params']

            logging.info(
                f"입력 요약: 발전소={selected}, 풍향={wind_params['wind_direction']}°, 풍속={wind_params['wind_speed']}m/s, "
                f"안정도={wind_params['stability_category']}({wind_params['stability_weight']}), "
                f"주말={is_weekend}, 방학={is_vacation}, 시간대={slot}, TOPN={topN}"
            )

            slot_weights = pick_slot_weights(slot, is_weekend, is_vacation)

            gdf = load_and_merge_geodata(paths)
            poi_df = read_csv_guess_encoding(paths.poi_path, 'poi_data')
            gdf = compute_dynamic_population(gdf, poi_df, slot_weights, alpha=args.dynamic_alpha)

            plant_lat, plant_lon = POWER_PLANTS[selected]
            gdf = compute_scores(
                gdf, plant_lat, plant_lon, wind_params,
                opt_km=args.opt_km, wind_alpha=args.wind_alpha,
                weights=args.weights_parsed
            )

            if args.sensitivity != 'none':
                base_default_weights = {'distance_score': 0.25, 'cap_pc1': 0.25, 'wind_risk': 0.25, 'commercial_index': 0.25}
                base_w = args.weights_parsed or base_default_weights
                sensi_df = sensitivity_analysis_weights(
                    gdf, base_w,
                    topN=args.sensi_topN,
                    mode=args.sensitivity,
                    delta=args.sensi_delta,
                    steps=args.sensi_steps,
                    mc_runs=args.sensi_mc_runs
                )
                os.makedirs(paths.out_score, exist_ok=True)
                sensi_path = os.path.join(paths.out_score, f'weight_sensitivity_interactive_{args.sensitivity}.csv')
                sensi_df.to_csv(sensi_path, index=False, encoding='utf-8-sig')
                logging.info(f"가중치 민감도 결과 저장: {sensi_path}")

            m = build_map(gdf, selected, plant_lat, plant_lon, wind_params,
                          sector_radius_km=args.sector_radius_km)
            topN_df = add_topN_markers(m, gdf, topN=max(1, min(topN, len(gdf))))
            saved = save_outputs(gdf, m, paths, topN_df,
                                 save_images=args.save_images, image_scale=args.image_scale)
            if sensi_path:
                saved['sensitivity_csv'] = sensi_path

            print(json.dumps({
                'mode': 'interactive',
                'selected_plant': selected,
                'slot': slot,
                'is_weekend': is_weekend,
                'is_vacation': is_vacation,
                'wind': {
                    'direction_deg': wind_params['wind_direction'],
                    'speed_ms': wind_params['wind_speed'],
                    'stability': wind_params['stability_category'],
                    'stability_weight': wind_params['stability_weight']
                },
                'outputs': saved
            }, ensure_ascii=False, indent=2))

            again = _input_with_default("같은 세션에서 다시 실행할까요? (y/n)", "n").lower()
            if again != 'y':
                break
        return

    # === 비대화식(CLI) 단일 실행 ===
    auto_slot, auto_weekend, auto_vac = get_time_context_auto_full()
    slot = args.slot or auto_slot
    is_weekend = (args.weekend.lower() == 'true') if args.weekend is not None else auto_weekend
    is_vacation = (args.vacation.lower() == 'true') if args.vacation is not None else auto_vac

    if not args.plant:
        raise SystemExit("--plant 는 비대화식에서 필수입니다. 또는 --interactive 사용")
    selected = args.plant

    if args.weather_source == 'mongo':
        wind_params = fetch_weather_mongo(selected, args.mongo_uri, args.mongo_db, args.mongo_col)
    else:
        wind_params = fetch_weather_api(selected, args.api_base)
    topN = max(1, int(args.topN))

    logging.info(f"입력 요약(비대화식): 발전소={selected}, 주말={is_weekend}, 방학={is_vacation}, 시간대={slot}, TOPN={topN}")

    slot_weights = pick_slot_weights(slot, is_weekend, is_vacation)

    gdf = load_and_merge_geodata(paths)
    poi_df = read_csv_guess_encoding(paths.poi_path, 'poi_data')
    gdf = compute_dynamic_population(gdf, poi_df, slot_weights, alpha=args.dynamic_alpha)

    plant_lat, plant_lon = POWER_PLANTS[selected]
    gdf = compute_scores(
        gdf, plant_lat, plant_lon, wind_params,
        opt_km=args.opt_km, wind_alpha=args.wind_alpha,
        weights=args.weights_parsed
    )

    sensi_path = None
    if args.sensitivity != 'none':
        base_default_weights = {'distance_score': 0.25, 'cap_pc1': 0.25, 'wind_risk': 0.25, 'commercial_index': 0.25}
        base_w = args.weights_parsed or base_default_weights
        sensi_df = sensitivity_analysis_weights(
            gdf, base_w,
            topN=args.sensi_topN,
            mode=args.sensitivity,
            delta=args.sensi_delta,
            steps=args.sensi_steps,
            mc_runs=args.sensi_mc_runs
        )
        os.makedirs(paths.out_score, exist_ok=True)
        sensi_tag = scenario_tag({'plant': selected, 'slot': slot, 'is_weekend': is_weekend, 'is_vacation': is_vacation})
        sensi_path = os.path.join(paths.out_score, f'weight_sensitivity_{sensi_tag}_{args.sensitivity}.csv')
        sensi_df.to_csv(sensi_path, index=False, encoding='utf-8-sig')
        logging.info(f"가중치 민감도 결과 저장: {sensi_path}")

    m = build_map(gdf, selected, plant_lat, plant_lon, wind_params,
                  sector_radius_km=args.sector_radius_km)
    topN_df = add_topN_markers(m, gdf, topN=max(1, min(topN, len(gdf))))

    saved = save_outputs(gdf, m, paths, topN_df,
                         save_images=args.save_images, image_scale=args.image_scale)
    if sensi_path:
        saved['sensitivity_csv'] = sensi_path

    print(json.dumps({
        'mode': 'cli',
        'selected_plant': selected,
        'slot': slot,
        'is_weekend': is_weekend,
        'is_vacation': is_vacation,
        'wind': {
            'direction_deg': wind_params['wind_direction'],
            'speed_ms': wind_params['wind_speed'],
            'stability': wind_params['stability_category'],
            'stability_weight': wind_params['stability_weight']
        },
        'outputs': saved
    }, ensure_ascii=False, indent=2))

if __name__ == '__main__':
    main()
